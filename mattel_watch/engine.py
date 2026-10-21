import logging
from datetime import datetime, timezone
from .catalog import CatalogError
from .telegram import DeliveryError, health_message, summary_message
from .config import launch_time
from .launch_inspector import LaunchInspector
from .discovery import discovery_hint

log = logging.getLogger(__name__)
REMIND_SECONDS = 600  # "Launching in 10 min" reminder lead time.
# Inspection reasons meaning the page could not be read at all, as opposed to a readable unclear page.
BLOCKED_REASONS = ('Verification or access challenge', 'Product document returned HTTP', 'No document response',
                   'Navigation timed out', 'Browser page inspection failed', 'Browser navigated outside')
UNVERIFIED = 'Stock shows available, but the page could not be checked, so it may still be on a timer. Open it to confirm.'


class Engine:
    def __init__(self, cfg, state, catalog, notifier, clock=None, inspector=None):
        self.cfg, self.state, self.catalog, self.notifier = cfg, state, catalog, notifier
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.inspector = inspector or LaunchInspector()
        self._audit_after = 0
        self._blocked = set()
        self.state.validate_scope(cfg.scope_key)
        self.state.configure_watches(cfg.launch_watches)

    def scan(self):
        products = self.catalog.snapshot()
        if not self.state.has_baseline():
            check = self.catalog.snapshot()
            identities = lambda rows: {(p.id, tuple(sorted(p.groups))) for p in rows}
            if identities(products) != identities(check):
                raise CatalogError('Catalogue changed during initial baseline; retry when two complete reads agree')
            products = check
        summary = self.state.observe(products, self.cfg.scope_key)
        current = {p.id for p in products}
        if summary['baseline']:
            log.info('Baseline saved: %s products. Historical items skipped; upcoming launches are inspected in the background.', summary['observed'])
        else:
            for row in self.state.active():
                if row['id'] not in current:
                    continue
                p = self.state.product(row['id'])
                if not p.eligible() or self.state.watched_handle(p.handle):
                    continue
                self._process(p)
        self.record_scan_success()
        self.scan_launches()
        self.scan_discovery()
        self.maybe_daily_summary(summary['observed'])
        summary['active_launch_watches']=sum(not self._watch_finished(w) for w in self.state.watches())
        summary['baseline_pages_unchecked']=self.state.audit_unchecked()
        return summary

    def close(self):
        self.inspector.close()

    def _inspect(self, p):
        before=(p.launch_check,p.launch_at,p.launch_label,p.ui_buyable)
        result=self.inspector.inspect(p)
        if result.status=='unknown' and result.reason.startswith(BLOCKED_REASONS):
            self._blocked.add(p.id)
        else:
            self._blocked.discard(p.id)
        p.launch_check=result.status
        p.ui_buyable=result.ui_buyable
        if result.label:
            p.launch_label=result.label
        if result.launch_at:
            p.launch_at=result.launch_at
        self.state.save_product(p)
        if before!=(p.launch_check,p.launch_at,p.launch_label,p.ui_buyable):
            self.state.bump_revision(p.id)
        now=self.clock().timestamp()
        self.state.note_inspection(p.id,result.status,now+(self.cfg.baseline_recheck_seconds if result.status!='unknown' else 300))
        return result

    def _process(self, p, detail_ok=False, inspected=False):
        pid = p.id
        row = self.state.row(pid)
        # An item announced earlier (e.g. before its timer ended) gets an audible "available now" reply.
        alerted = row['alert_state'] == 'sent'
        if not detail_ok and (row['alert_state'] == 'pending' or (row['purchase_state'] == 'pending' and p.available)):
            try:
                p = self.catalog.details(p)
                self.state.save_product(p)
                detail_ok = True
            except CatalogError:
                log.warning('Product %s detail/currency refresh failed; availability check deferred', pid)
        if not inspected and (p.launch_check=='unchecked' or (row['purchase_state']=='pending' and (p.available or p.launch_check=='unknown'))):
            self._inspect(p)
            if self.state.auto_watch(p,self.clock().timestamp()):
                watch=next(w for w in self.state.watches() if w['handle']==p.handle)
                self.state.update_watch(p.handle,self._next_watch_check(watch,self.clock().timestamp()))
        self._send_alert(p)
        row = self.state.row(pid)
        # Stock flags alone must never override an explicitly configured launch.
        launch_due = p.launch_at is None or self.clock() >= launch_time(p.launch_at)
        verified = p.launch_check in {'no_timer','scheduled'} and p.ui_buyable is True
        # A blocked page check (HTTP 429 / verification) must not hide a release: stock data alone
        # then earns a notice. A readable page with an unclear timer still waits.
        blocked = p.launch_check == 'unknown' and pid in self._blocked
        ready = (row['purchase_state'] == 'pending' and p.available and detail_ok and launch_due
                 and (verified or blocked))
        if ready:
            self.state.finish_purchase(pid, 'alerts_only', 'Available now: add it to your cart yourself' if verified else
                                       UNVERIFIED)
        self._edit_status(pid)
        if ready and alerted:
            self._ping(pid)

    def _ping(self, pid):
        # Telegram edits are silent, so "available now" is also sent as a reply to the earlier alert.
        row = self.state.row(pid)
        try:
            self.notifier.notify(row['alert_id'], self.state.product(pid), row['detail'])
        except DeliveryError:
            log.warning('Product %s availability notification failed; status edit still applies', pid)

    def _send_alert(self, p):
        row = self.state.row(p.id)
        if self.state.claim_alert(p.id):
            try:
                mid = self.notifier.send(p, row['purchase_state'], row['detail'])
            except DeliveryError as exc:
                self.state.finish_alert(p.id, 'uncertain' if exc.ambiguous else 'pending')
                log.warning('Product %s alert %s', p.id, 'uncertain; not resent' if exc.ambiguous else 'rejected; pending retry')
            else:
                self.state.finish_alert(p.id, 'sent', mid)

    def discover_product(self, handle):
        """Inspect a saved product now; register/notify a future launch, never buy."""
        if not self.state.has_baseline():
            raise ValueError('Run scan first to establish the saved catalogue.')
        self.state.validate_scope(self.cfg.scope_key)
        p = self.state.product_for_handle(handle)
        if p is None:
            raise ValueError('Product is not in the saved catalogue. Run scan to refresh it first.')
        if not p.eligible():
            raise ValueError('Product is outside the eligible RLC/Premium scope.')
        row = self.state.row(p.id)
        answer = {'read_only': False, 'no_purchase': True, 'found_in_catalogue': True,
                  'watch_registered': False, 'title': p.title, 'status': p.launch_check,
                  'launch_at': p.launch_at, 'alert_state': row['alert_state']}
        if self.state.watched_handle(handle):
            return {**answer, 'status': 'already_watched',
                    'reason': 'Existing watch and notification history preserved; use status to inspect it.'}
        if row['purchase_state'] not in {'baseline', 'pending'}:
            return {**answer, 'status': 'already_processed',
                    'reason': 'Existing purchase outcome preserved.'}
        p = self.catalog.details(p)
        self.state.save_product(p)
        result = self._inspect(p)
        registered = self.state.auto_watch(p, self.clock().timestamp())
        if registered:
            watch = next(w for w in self.state.watches() if w['handle'] == handle)
            self.state.update_watch(handle, self._next_watch_check(watch, self.clock().timestamp()), 'scheduled')
            # Do not call _process: a launch may become due while inspection or
            # Telegram is in flight, even when the user's config is live.
            self._send_alert(p)
            self._edit_status(p.id)
        return {**answer, 'watch_registered': registered, 'status': result.status,
                'launch_at': result.launch_at, 'alert_state': self.state.row(p.id)['alert_state'],
                'reason': result.reason if registered else 'No verified future launch was registered. ' + (result.reason or '')}

    def _watch_finished(self, watch):
        row = self.state.row(watch['product_id']) if watch['product_id'] else None
        if row is None:
            return False
        if row['alert_state'] == 'pending':
            return False
        return row['purchase_state'] in {'purchased','dry_run','in_cart','needs_attention','uncertain'} or (
            row['purchase_state'] == 'alerts_only' and watch['phase'] == 'available')

    def _next_watch_check(self, watch, now):
        at = launch_time(watch['launch_at']).timestamp()
        window = self.cfg.launch_window_seconds
        interval = self.cfg.launch_poll_seconds if at-window <= now <= at+window else self.cfg.interval_seconds
        after = now + interval
        if now < at-REMIND_SECONDS and watch['reminded_for'] != watch['launch_at']:
            after = min(after, at-REMIND_SECONDS)
        if now < at-window:
            after = min(after, at-window)
        elif now < at:
            after = min(after, at)
        return after

    def scan_launches(self):
        if not self.state.has_baseline():
            return
        now = self.clock().timestamp()
        for watch in self.state.watches():
            if self._watch_finished(watch) or now < watch['check_after']:
                continue
            after = self._next_watch_check(watch, now)
            try:
                if watch['product_id'] and hasattr(self.catalog,'refresh_watch'):
                    p=self.catalog.refresh_watch(self.state.product(watch['product_id']))
                else:
                    p = self.catalog.watch_product(watch['handle'])
                p.launch_at = watch['launch_at']
                self.state.bind_watch(watch, p)
                result=self._inspect(p)
                if result.launch_at:
                    if watch['source']=='auto':
                        self.state.update_auto_time(p.handle,result.launch_at)
                        watch=dict(watch);watch['launch_at']=result.launch_at
                        after=self._next_watch_check(watch,now)
                    else:
                        # A later site launch always takes precedence over a manual earlier time.
                        p.launch_at=max(launch_time(watch['launch_at']),launch_time(result.launch_at)).isoformat()
                        self.state.save_product(p)
            except (CatalogError, ValueError):
                # Back off blocked/invalid reads even during the release window.
                self.state.update_watch(watch['handle'], max(after, now+60))
                log.warning('Launch watch %s could not be refreshed; no purchase attempted', watch['handle'])
                continue
            phase = 'scheduled' if now < launch_time(p.launch_at).timestamp() else (
                'available' if p.available and p.ui_buyable is True and p.launch_check in {'scheduled','no_timer'} else 'waiting_for_availability')
            if phase != watch['phase']:
                self.state.bump_revision(p.id)
            self.state.update_watch(watch['handle'], after, phase)
            self._process(p, detail_ok=True, inspected=True)
            self._maybe_remind(watch, p, now)

    def _maybe_remind(self, watch, p, now):
        at = launch_time(p.launch_at)
        left = at.timestamp() - now
        row = self.state.row(p.id)
        if (not 0 < left <= REMIND_SECONDS or watch['reminded_for'] == p.launch_at
                or row['purchase_state'] != 'pending'):
            return
        try:
            self.notifier.remind(row['alert_id'] if row['alert_state'] == 'sent' else None, p, max(1, round(left / 60)))
        except DeliveryError:
            log.warning('Launch reminder for %s failed; it is retried on the next check', p.handle)
        else:
            self.state.mark_reminded(watch['handle'], p.launch_at)

    def _post(self, text):
        try:
            self.notifier.post(text)
            return True
        except DeliveryError:
            log.warning('Telegram status post failed; retried later')
            return False

    def record_scan_failure(self, reason):
        failures = int(self.state.get_setting('health_failures') or 0) + 1
        self.state.set_setting('health_failures', str(failures))
        if failures >= self.cfg.health_failures_before_alert and self.state.get_setting('health_alerted') != '1':
            if self._post(health_message(True, failures, reason)):
                self.state.set_setting('health_alerted', '1')

    def record_scan_success(self):
        if self.state.get_setting('health_alerted') == '1' and self._post(health_message(False)):
            self.state.set_setting('health_alerted', '0')
        if self.state.get_setting('health_failures') not in (None, '0'):
            self.state.set_setting('health_failures', '0')

    def maybe_daily_summary(self, observed):
        now = self.clock()
        today = now.date().isoformat()
        if now.hour < self.cfg.summary_hour_utc or self.state.get_setting('summary_date') == today:
            return
        launches = sorted((launch_time(w['launch_at']), self.state.product(w['product_id']).title)
                          for w in self.state.watches()
                          if w['product_id'] and not self._watch_finished(w) and launch_time(w['launch_at']) > now)
        if self._post(summary_message(observed, self.state.daily_stats(), launches)):
            self.state.set_setting('summary_date', today)

    def scan_discovery(self):
        """Audit old baseline pages in the background; new products inspect immediately."""
        now=self.clock().timestamp()
        if not self.state.has_baseline() or now<self._audit_after:
            return
        self._audit_after=now+self.cfg.discovery_poll_seconds
        if any(abs(launch_time(w['launch_at']).timestamp()-now)<=self.cfg.launch_window_seconds and not self._watch_finished(w) for w in self.state.watches()):
            return  # Prioritize due product checks over the historical page audit.
        p=self.state.next_audit_product(now)
        if p is None:return
        log.info('Launch discovery: checking %s (%s); %s baseline pages unchecked',
                 p.title, discovery_hint(p), self.state.audit_unchecked())
        try:
            p=self.catalog.details(p)
            self.state.save_product(p)
            result=self._inspect(p)
        except CatalogError:
            self.state.note_inspection(p.id,'unknown',now+300)
            log.warning('Launch discovery: %s could not be refreshed; retry queued', p.handle)
            return
        log.info('Launch discovery: %s -> %s; %s baseline pages unchecked',
                 p.handle, result.status, self.state.audit_unchecked())
        if self.state.auto_watch(p,self.clock().timestamp()):
            log.info('Automatically discovered future launch: %s at %s',p.handle,p.launch_at)
            watch=next(w for w in self.state.watches() if w['handle']==p.handle)
            self.state.update_watch(p.handle,self._next_watch_check(watch,now),'scheduled')
            self._process(p,detail_ok=True,inspected=True)

    def next_discovery_delay(self):
        if not self.state.has_baseline():return float('inf')
        return max(0.5,self._audit_after-self.clock().timestamp())

    def next_watch_delay(self):
        now = self.clock().timestamp()
        delays = [max(0.5, w['check_after']-now) for w in self.state.watches() if not self._watch_finished(w)]
        return min(delays) if delays else float('inf')

    def _edit_status(self, pid):
        row = self.state.row(pid)
        if row['alert_state'] != 'sent' or row['edited_revision'] >= row['revision']:
            return
        try:
            self.notifier.edit(row['alert_id'], self.state.product(pid), row['purchase_state'], row['detail'])
        except DeliveryError:
            log.warning('Product %s status edit failed; original alert retained', pid)
        else:
            self.state.edit_done(pid, row['revision'])
