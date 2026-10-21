import argparse
import json
import logging
import os
from pathlib import Path
import random
import tempfile
import time
from datetime import datetime, timezone
from .catalog import ShopifyCatalog, CatalogError
from .config import Config, load_env
from .config import launch_time
from dataclasses import asdict
from urllib.parse import urlsplit
import re
from .engine import Engine
from .http import JsonHTTP
from .lock import process_lock
from .models import Product, Variant
from .state import State
from .telegram import Telegram, ConsoleTelegram, DeliveryError
from .launch_inspector import LaunchInspector, Inspection


def product_handle(url):
    parsed = urlsplit(url or '')
    match = re.fullmatch(r'/(?:en-ca/)?products/([a-z0-9-]+)/?', parsed.path)
    if (parsed.scheme != 'https' or parsed.netloc != 'creations.mattel.com'
            or parsed.query or parsed.fragment or not match):
        raise ValueError('Provide --product-url as an exact HTTPS Mattel product page')
    return match.group(1)


def demo():
    """Fully offline lifecycle demo with illustrative, explicitly synthetic products."""
    class FixtureCatalog:
        def __init__(self):
            self.items = [Product('100', 'demo-existing', 'Hot Wheels RLC DEMO EXISTING',
                                 'https://creations.mattel.com/en-ca/products/demo-existing',
                                 ['rlc'], [Variant('1000', 'Default Title', True, 4600)], currency='CAD')]
        def snapshot(self):
            return [Product.from_json(p.to_json()) for p in self.items]
        def details(self, p):
            return p
    print('OFFLINE DEMO — synthetic product names and prices; no Mattel or Telegram calls.')
    with tempfile.TemporaryDirectory(prefix='mattel-watch-demo-') as folder:
        cfg = Config()
        state = State(Path(folder) / 'state.sqlite3')
        cat, notifier = FixtureCatalog(), ConsoleTelegram()
        class DemoInspector:
            def inspect(self,p):return Inspection('no_timer',ui_buyable=p.available,reason='Synthetic offline demo')
            def close(self):pass
        engine = Engine(cfg, state, cat, notifier, inspector=DemoInspector())
        print('1. Baseline:', engine.scan())
        cat.items.append(Product('101', 'demo-new', 'Hot Wheels Premium DEMO NEW',
                                 'https://creations.mattel.com/en-ca/products/demo-new',
                                 ['premium'], [Variant('1001', 'Default Title', True, 1600)], currency='CAD'))
        print('2. New listing:', engine.scan())
        print('3. Same listing again:', engine.scan())
        print(json.dumps({'alerts_emitted': notifier.count, 'state': state.rows_for_status()}, indent=2))
        state.close()


def main():
    parser = argparse.ArgumentParser(description='Mattel Hot Wheels RLC + Premium monitor (Canada)')
    parser.add_argument('command', choices=['demo','scan','run','doctor','status','telegram-test','telegram-chats','watch','discover','rescope'])
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--console', action='store_true', help='Offline notification preview; use a separate state file')
    parser.add_argument('--product-url', help='Exact Mattel product URL for doctor, discover, status or watch')
    parser.add_argument('--headed', action='store_true', help='Doctor only: show the anonymous browser for 15 seconds before inspecting')
    parser.add_argument('--launch-at', help='Verified ISO launch time with offset, for example 2026-10-20T09:00:00-07:00')
    args = parser.parse_args()
    if args.headed and (args.command != 'doctor' or not args.product_url):
        parser.error('--headed requires doctor --product-url')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.command == 'demo':
        demo()
        return
    try:
        load_env()
        cfg = Config.load(args.config)
        if args.command == 'watch':
            handle = product_handle(args.product_url)
            at = launch_time(args.launch_at).isoformat()
            cfg.launch_watches = [w for w in cfg.launch_watches if w['handle'] != handle]
            cfg.launch_watches.append({'handle':handle, 'launch_at':at})
            cfg.validate()
            target = Path(args.config)
            temporary = target.with_name(target.name + '.tmp')
            temporary.write_text(json.dumps(asdict(cfg), indent=2) + '\n', encoding='utf-8')
            temporary.replace(target)
            print(json.dumps({'watch_added': handle, 'launch_at_utc': at,
                              'next_step':'Restart the monitor to apply. Existing history is preserved.'}, indent=2))
            return
        if args.command == 'doctor':
            source = ShopifyCatalog(cfg, JsonHTTP())
            if args.product_url:
                p=source.watch_product(product_handle(args.product_url))
                inspector=LaunchInspector(headless=not args.headed,review_ms=15000 if args.headed else 0)
                if args.headed:
                    print('Opening a temporary browser for 15 seconds. Observe any region or access message. Nothing is clicked and no account is used.',flush=True)
                try:
                    result=inspector.inspect(p)
                    print(json.dumps({'read_only':True,'title':p.title,'currency':p.currency,**asdict(result)},indent=2))
                finally:inspector.close()
                return
            products = source.snapshot()
            eligible = [p for p in products if p.eligible()]
            print(json.dumps({'read_only': True, 'catalogue_products': len(products), 'eligible_products': len(eligible)}, indent=2))
            if eligible:
                p = source.details(eligible[0])
                print(json.dumps({'sample_product': p.title, 'presentment_currency': p.currency,
                                  'cad_verified': p.currency == 'CAD'}, indent=2))
            return
        if args.command == 'status':
            if not Path(cfg.state_path).exists():
                print('No state database yet. Run scan to establish a baseline.')
                return
            state = State.open_readonly(cfg.state_path)
            try:
                result = (state.product_status(product_handle(args.product_url), datetime.now(timezone.utc).timestamp())
                          if args.product_url else
                          {'read_only': True, 'products':state.rows_for_status(), 'launch_watches':[dict(w) for w in state.watches()]})
                print(json.dumps(result, indent=2))
            finally:
                state.close()
            return
        if args.command == 'discover':
            handle = product_handle(args.product_url)
            if args.console:
                raise ValueError('discover saves notification history; configure Telegram and omit --console')
            if not Path(cfg.state_path).is_file():
                raise ValueError('Run scan first to establish the saved catalogue.')
        if args.command == 'rescope':
            # One-time switch to new collections (for example the whole store) without an alert flood.
            if not Path(cfg.state_path).is_file():
                raise ValueError('No state database yet; run scan instead.')
            saved = State.open_readonly(cfg.state_path)
            try:
                current = saved.get_setting('scope')
            finally:
                saved.close()
            if current == cfg.scope_key:
                # Safe to run every time: nothing is fetched or changed once migrated.
                print(json.dumps({'rescoped': False, 'reason': 'Already using these collections'}))
                return
            with process_lock(cfg.state_path + '.lock'):
                source = ShopifyCatalog(cfg, JsonHTTP())
                first, second = source.snapshot(), source.snapshot()
                if {p.id for p in first} != {p.id for p in second}:
                    raise CatalogError('Catalogue changed between reads; run rescope again')
                state = State(cfg.state_path)
                try:
                    added = state.rescope(second, cfg.scope_key)
                finally:
                    state.close()
            print(json.dumps({'rescoped': True, 'products_listed': len(second), 'added_without_alert': added}, indent=2))
            return
        if args.command == 'telegram-chats':
            # Only the token is needed here; the chat ID is what this command finds.
            chats = Telegram(os.environ.get('TELEGRAM_BOT_TOKEN'), '0', JsonHTTP()).recent_chats()
            print(json.dumps({'chats': chats, 'next_step': 'Put the channel id in TELEGRAM_CHAT_ID in .env, then run telegram-test.'
                              if chats else 'No chats seen yet: post a message in the channel (bot must be an admin), then run this again.'}, indent=2))
            return
        notifier = ConsoleTelegram() if args.console else Telegram(
            os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID'), JsonHTTP())
        if args.command == 'telegram-test':
            if args.console:
                raise ValueError('telegram-test requires real bot credentials; omit --console')
            notifier.test()
            print('Telegram setup test sent.')
            return
        # Lock held for the full scan/loop and crash recovery, not just each DB write.
        with process_lock(cfg.state_path + '.lock'):
            state = State(cfg.state_path)
            try:
                state.recover()
                engine = Engine(cfg, state, ShopifyCatalog(cfg, JsonHTTP()), notifier)
                if args.command == 'discover':
                    print(json.dumps(engine.discover_product(handle), indent=2), flush=True)
                    return
                delay = cfg.interval_seconds
                catalogue_due = time.monotonic()
                while True:
                    if time.monotonic() >= catalogue_due:
                        try:
                            print(json.dumps(engine.scan()), flush=True)
                            delay = cfg.interval_seconds
                        except CatalogError as exc:
                            logging.error('%s', exc)
                            engine.record_scan_failure(str(exc))
                            if args.command == 'scan':
                                raise
                            delay = min(max(delay * 2, cfg.interval_seconds), 3600)
                        catalogue_due = time.monotonic() + delay + random.uniform(0, min(10, delay * 0.05))
                    else:
                        engine.scan_launches()
                        engine.scan_discovery()
                    if args.command == 'scan':
                        break
                    time.sleep(min(60, max(0.5,catalogue_due-time.monotonic()), engine.next_watch_delay(),engine.next_discovery_delay()))
            finally:
                if 'engine' in locals():engine.close()
                state.close()
    except KeyboardInterrupt:
        print('Stopped. State saved.')
    except (ValueError, RuntimeError, CatalogError, DeliveryError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
