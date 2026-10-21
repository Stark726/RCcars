"""Read-only rendered-page launch inspection. No cart, login, or payment actions."""
from dataclasses import dataclass, replace
from datetime import datetime, timezone, timedelta
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Inspection:
    status: str  # scheduled, no_timer, unknown
    launch_at: str | None = None
    label: str | None = None
    ui_buyable: bool = False
    reason: str = ''
    diagnostics: dict | None = None


_LAUNCH = re.compile(
    r'\bLaunch(?:es|ing)?\s+(?:on\s+)?'
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})\s+(?:at\s+)?'
    r'(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>am|pm)\s*'
    r'(?P<zone>PST|PDT|PT|EST|EDT|ET|UTC)\b', re.I)
_MONTHS = {name.lower(): index for index,name in enumerate(
    ['January','February','March','April','May','June','July','August','September','October','November','December'], 1)}
_CHALLENGE = re.compile(r'access denied|verify (?:that )?you are (?:a )?human|checking your browser|unusual traffic|press and hold|connection needs to be verified', re.I)


def classify(snapshot, title):
    normalize = lambda s: ' '.join(str(s).split()).casefold()
    if normalize(snapshot.get('title','')) != normalize(title):
        return Inspection('unknown', reason='Rendered product identity did not match')
    text = ' '.join(str(snapshot.get('text','')).split())
    if _CHALLENGE.search(text):
        return Inspection('unknown', reason='Verification or access challenge')
    matches = list(_LAUNCH.finditer(text))
    if len(matches) > 1:
        return Inspection('unknown', reason='Multiple launch dates in the product section')
    coming = bool(re.search(r'coming soon|launching soon|countdown|\blaunch(?:es|ing)?\b', text, re.I))
    if matches:
        m = matches[0]
        try:
            hour=int(m['hour']); minute=int(m['minute'])
            if not 1 <= hour <= 12 or not 0 <= minute <= 59: raise ValueError('invalid time')
            hour=hour % 12 + (12 if m['ampm'].lower()=='pm' else 0)
            zone=m['zone'].upper()
            if zone in {'PT','ET'}:
                tz=ZoneInfo('America/Los_Angeles' if zone=='PT' else 'America/New_York')
            else:
                offsets={'PST':-8,'PDT':-7,'EST':-5,'EDT':-4,'UTC':0}
                tz=timezone(timedelta(hours=offsets[zone]))
            local=datetime(int(m['year']),_MONTHS[m['month'].lower()],int(m['day']),hour,minute,tzinfo=tz)
            # Reject an ambiguous/nonexistent DST time instead of guessing.
            if zone in {'PT','ET'}:
                back=local.astimezone(timezone.utc).astimezone(tz)
                if back.replace(tzinfo=None)!=local.replace(tzinfo=None) or local.replace(fold=0).utcoffset()!=local.replace(fold=1).utcoffset():
                    raise ValueError('ambiguous DST time')
            at=local.astimezone(timezone.utc).isoformat()
            return Inspection('scheduled',at,m.group(0),bool(snapshot.get('add_enabled')),
                              'Date read from rendered product section')
        except (ValueError, KeyError, ZoneInfoNotFoundError):
            return Inspection('unknown',reason='Launch date/time zone could not be resolved; install tzdata')
    if snapshot.get('add_enabled') and not snapshot.get('countdown_visible'):
        return Inspection('no_timer',ui_buyable=True,reason='Enabled product purchase control; no countdown or launch date')
    if coming or snapshot.get('countdown_visible'):
        return Inspection('unknown',reason='Timer/coming-soon marker found without a supported launch date')
    if snapshot.get('sold_out'):
        return Inspection('no_timer',ui_buyable=False,reason='Sold-out product control; no launch marker found')
    return Inspection('unknown',reason='Product purchase/timer layout is not recognized')


class LaunchInspector:
    def __init__(self, *, headless=True, test_origin=None, settle_ms=2500, review_ms=0):
        self.headless=headless
        self.test_origin=test_origin
        self.settle_ms=settle_ms
        self.review_ms=review_ms
        self._timeout_types=(TimeoutError,)
        self._pw=self._browser=self._context=None

    def close(self):
        for obj in [self._context,self._browser]:
            if obj:
                try:obj.close()
                except Exception:pass
        if self._pw:
            try:self._pw.stop()
            except Exception:pass
        self._pw=self._browser=self._context=None

    def _allowed(self,url, *, allow_root=False):
        p=urlsplit(url)
        if p.username is not None or p.password is not None:return False
        origin=f'{p.scheme}://{p.netloc}'
        path_pattern=r'/(?:en-ca/)?products/[a-z0-9-]+/?' if allow_root else r'/en-ca/products/[a-z0-9-]+/?'
        if self.test_origin and origin==self.test_origin and p.hostname in {'localhost','127.0.0.1'}:
            return bool(re.fullmatch(path_pattern,p.path))
        return p.scheme=='https' and p.netloc=='creations.mattel.com' and bool(re.fullmatch(path_pattern,p.path))

    def _page_scope(self,url):
        if not self._allowed(url,allow_root=True):return 'unverified'
        return 'canadian_product' if urlsplit(url).path.startswith('/en-ca/') else 'canonical_product'

    @staticmethod
    def _public_url(url):
        """Report only public store paths; never leak login tokens or return URLs."""
        try:
            p=urlsplit(url)
            if p.username is not None or p.password is not None:
                return '[redacted URL]'
            if p.scheme not in {'http','https'} or p.hostname not in {'creations.mattel.com','localhost','127.0.0.1'}:
                return '[external or non-web URL]'
            path=p.path
            if not re.fullmatch(r'/(?:[a-z]{2}-[a-z]{2}/)?products/[a-z0-9-]+/?',path) and path not in {'','/','/en-ca','/en-ca/'}:
                path='/[redacted path]'
            return f'{p.scheme}://{p.netloc}{path}'
        except ValueError:
            return '[invalid URL]'

    def _navigation_problem(self, current, expected, status):
        current_parts,expected_parts=urlsplit(current),urlsplit(expected)
        if (not self._allowed(current,allow_root=True) or
                (current_parts.scheme,current_parts.netloc)!=(expected_parts.scheme,expected_parts.netloc)):
            return 'Browser navigated outside the permitted Mattel product pages'
        # Mattel can redirect a Canadian URL to its public root product page.
        # Permit only that exact handle for read-only metadata. The product's
        # Canadian URL, price/stock source and checkout region checks stay intact.
        if current_parts.path.rstrip('/').rsplit('/',1)[-1] != expected_parts.path.rstrip('/').rsplit('/',1)[-1]:
            return 'Browser redirected to a different product'
        if status is None:
            return 'No document response was received'
        if status >= 400:
            return f'Product document returned HTTP {status}'
        return None

    def _start(self):
        if self._context:return
        from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeoutError
        self._timeout_types=(TimeoutError,BrowserTimeoutError)
        self._pw=sync_playwright().start()
        self._browser=self._pw.chromium.launch(headless=self.headless)
        self._context=self._browser.new_context(locale='en-CA')

    def inspect(self,product):
        if not self._allowed(product.url):
            return Inspection('unknown',reason='Product URL is outside the Canadian storefront')
        page=None
        document={'status':None}
        stage='starting_browser'
        navigation_timed_out=False
        def diagnostics():
            return {'requested_url':self._public_url(product.url),
                    'final_url':self._public_url(page.url) if page else None,
                    'http_status':document['status'], 'headless':self.headless,
                    'page_scope':self._page_scope(page.url) if page else 'unverified',
                    'stage':stage,'navigation_timed_out':navigation_timed_out}
        def unknown(reason):
            return Inspection('unknown',reason=reason,diagnostics=diagnostics())
        try:
            self._start()
            page=self._context.new_page()
            page.set_default_timeout(10000)
            # A JavaScript redirect can create a new document after goto returns.
            # Track only top-level document responses, never subresource errors.
            def on_response(response):
                if response.request.resource_type=='document' and response.frame==page.main_frame:
                    document['status']=response.status
            page.on('response',on_response)
            stage='navigation'
            try:
                response=page.goto(product.url,wait_until='domcontentloaded',timeout=25000)
            except self._timeout_types:
                # Only a navigation timeout can use this fallback. A valid
                # response alone never establishes a usable product or no timer.
                navigation_timed_out=True
                response=None
            if document['status'] is None and response:
                document['status']=response.status
            # Doctor's visible browser is diagnostic only. No clicks or challenge
            # handling are performed; the same URL/status checks still apply.
            if self.review_ms:
                stage='review'
                page.wait_for_timeout(self.review_ms)
            problem=self._navigation_problem(page.url,product.url,document['status'])
            if problem:return unknown(problem)
            if navigation_timed_out and document['status']!=200:
                return unknown('Navigation timed out without an HTTP 200 product document')
            stage='product_heading'
            heading=re.compile(r'^\s*'+r'\s+'.join(re.escape(word) for word in product.title.split())+r'\s*$',re.I)
            product_heading=page.get_by_role('heading',level=1,name=heading).first
            # A verification interstitial ends the attempt at once; it is never
            # clicked or solved, and the next scheduled poll simply retries.
            challenge=page.get_by_role('heading',level=1,name=_CHALLENGE).first
            product_heading.or_(challenge).first.wait_for(state='visible',timeout=15000)
            if not product_heading.is_visible():
                return unknown('Verification or access challenge')
            stage='render_wait'
            page.wait_for_timeout(self.settle_ms)
            problem=self._navigation_problem(page.url,product.url,document['status'])
            if problem:return unknown(problem)
            stage='extraction'
            result=self._snapshot(page,product)
            problem=self._navigation_problem(page.url,product.url,document['status'])
            if problem:return unknown(problem)
            classified=classify(result,product.title)
            if navigation_timed_out:
                if document['status']!=200:
                    return unknown('Navigation timed out without an HTTP 200 product document')
                if classified.status=='no_timer':
                    return unknown('Navigation timed out; absence of a timer is not verified')
                # A positive date can inform scheduling, but a partially loaded
                # page cannot authorize even the enabled-control checkout hint.
                classified=replace(classified,ui_buyable=False)
            stage='complete'
            if classified.status!='unknown' and self._page_scope(page.url)=='canonical_product':
                classified=replace(classified,reason=classified.reason+'; public root page, Canadian checkout verification still required')
            return replace(classified,diagnostics=diagnostics())
        except ImportError:
            return Inspection('unknown',reason='Playwright is required to verify timers; install requirements-checkout.txt')
        except Exception as exc:
            # Do not include browser exceptions: they may expose authenticated URLs.
            result=unknown(f'Browser page inspection failed during {stage} ({type(exc).__name__})')
            self.close()
            return result
        finally:
            if page:
                try:page.close()
                except Exception:pass

    @staticmethod
    def _snapshot(page,product):
        return page.evaluate(r'''({title,variants}) => {
          const norm=s=>(s||'').replace(/\s+/g,' ').trim().toLowerCase();
          const visible=e=>!!(e && (e.offsetWidth||e.offsetHeight||e.getClientRects().length));
          const h=[...document.querySelectorAll('h1')].find(e=>visible(e)&&norm(e.innerText)===norm(title));
          if(!h)return {title:'',text:document.body.innerText.slice(0,5000)};
          let root=h.parentElement;
          while(root && root.tagName!=='BODY') {
            const controls=[...root.querySelectorAll('button,input[type=submit]')].filter(visible);
            if(controls.some(e=>/^(add to (bag|cart)|pre[- ]?order|sold out|coming soon)\b/i.test((e.innerText||e.value||'').trim())))break;
            root=root.parentElement;
          }
          if(!root || root.tagName==='BODY')root=h.closest('main')||h.parentElement;
          const text=root.innerText||'';
          const buttons=[...root.querySelectorAll('button,input[type=submit]')].filter(visible);
          const relevant=e=>{
            const form=e.closest('form');
            const id=form?.querySelector('input[name=id],select[name=id]')?.value;
            return !id || variants.includes(String(id));
          };
          return {title:h.innerText,text,
            add_enabled:buttons.some(e=>relevant(e)&&/^(add to (bag|cart)|pre[- ]?order)(\s|$)/i.test((e.innerText||e.value||'').trim())&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'),
            sold_out:buttons.some(e=>relevant(e)&&/^sold out$/i.test((e.innerText||e.value||'').trim())),
            countdown_visible:[...root.querySelectorAll('[class*=countdown i],[id*=countdown i]')].some(visible)};
        }''',{'title':product.title,'variants':[v.id for v in product.variants]})
