"""
Browser-agent subprocess script (payload for ``Bridge.browser_open``).

Kept as a string constant rather than executable module code on purpose: the
runtime launches it via ``python -c SCRIPT`` (see ``bridge.browser_open`` and
``export_pdf`` for the same pattern), and embedding it in an imported
module keeps the existing subprocess/packaging behavior byte-for-byte identical
while getting ~275 lines of payload out of bridge.py.

Runs headed Playwright Chromium with a persistent profile and exposes a local
HTTP control API: GET /ping /status, POST /navigate /focus /detect-fields
/scrape /check-google-login /stop. The main thread drives the Playwright event loop via
a command queue; a daemon thread serves HTTP; another watches stdin so the
browser exits when the parent app dies.
"""

SCRIPT = r'''
import sys, json, queue as _q, threading, os, shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

def _system_chrome_available():
    """Return whether the machine has a Chrome installation Playwright can use."""
    names = ['google-chrome', 'google-chrome-stable', 'chrome']
    if sys.platform == 'win32':
        local = os.environ.get('LOCALAPPDATA', '')
        program_files = os.environ.get('PROGRAMFILES', '')
        program_files_x86 = os.environ.get('PROGRAMFILES(X86)', '')
        candidates = [
            os.path.join(local, 'Google', 'Chrome', 'Application', 'chrome.exe'),
            os.path.join(program_files, 'Google', 'Chrome', 'Application', 'chrome.exe'),
            os.path.join(program_files_x86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
        ]
        return any(os.path.isfile(p) for p in candidates) or bool(shutil.which('chrome.exe'))
    if sys.platform == 'darwin':
        candidates = [
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            os.path.expanduser('~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        ]
        return any(os.path.isfile(p) for p in candidates)
    return any(shutil.which(name) for name in names)

_browser_channel = 'chrome' if _system_chrome_available() else None

_cmd_q, _res_q = _q.Queue(), _q.Queue()
# Set by Playwright's event thread when the active page is closed by the user.
_page_closed = threading.Event()

class _H(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def _j(self, d, code=200):
        b = json.dumps(d).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _dispatch(self, cmd, timeout=20):
        _cmd_q.put(cmd)
        try:
            return _res_q.get(timeout=timeout)
        except _q.Empty:
            return {"ok": False, "error": "timeout"}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/ping":
            self._j({"ok": True})
        elif path == "/status":
            self._j(self._dispatch({"t": "status"}, timeout=5))
        else:
            self._j({"ok": False, "error": "not found"}, 404)

    def do_OPTIONS(self):
        # CORS preflight — WKWebView sends OPTIONS before every non-simple request.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n)) if n else {}
        if path == "/navigate":
            self._j(self._dispatch({"t": "navigate", "url": body.get("url", "")}, timeout=25))
        elif path == "/focus":
            self._j(self._dispatch({"t": "focus"}, timeout=5))
        elif path == "/detect-fields":
            self._j(self._dispatch({"t": "detect_fields"}, timeout=15))
        elif path == "/scrape":
            self._j(self._dispatch({"t": "scrape", "url": body.get("url", "")}, timeout=35))
        elif path == "/check-google-login":
            self._j(self._dispatch({"t": "check_google_login"}, timeout=20))
        elif path == "/stop":
            _cmd_q.put({"t": "stop"})
            self._j({"ok": True})
        else:
            self._j({"ok": False, "error": "not found"}, 404)


def _watch_stdin():
    """Exit the agent when the parent process dies (stdin reaches EOF).
    This prevents orphan Chromium processes after a force-quit of the app."""
    try:
        sys.stdin.read()
    except Exception:
        pass
    os._exit(0)

threading.Thread(target=_watch_stdin, daemon=True).start()

_start_url = sys.argv[1] if len(sys.argv) > 1 else "about:blank"
_user_data_dir = sys.argv[2] if len(sys.argv) > 2 else ''

if _user_data_dir:
    import pathlib as _pl
    _lock = _pl.Path(_user_data_dir) / 'SingletonLock'
    try:
        if _lock.exists() or _lock.is_symlink():
            _lock.unlink()
    except Exception:
        pass

def _attach_page_listener(page):
    """Register close handler on a page; fires from Playwright's I/O thread."""
    page.on("close", lambda _p: _page_closed.set())

with sync_playwright() as _pw:
    _browser = None
    if _user_data_dir:
        import pathlib as _pl
        _pl.Path(_user_data_dir).mkdir(parents=True, exist_ok=True)
        _ctx = _pw.chromium.launch_persistent_context(
            _user_data_dir,
            headless=False,
            channel=_browser_channel,
            viewport=None,
            ignore_default_args=['--enable-automation'],
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-first-run',
                '--no-default-browser-check',
            ],
        )
        _page = _ctx.pages[0] if _ctx.pages else _ctx.new_page()
    else:
        _browser = _pw.chromium.launch(headless=False)
        _ctx = _browser.new_context()
        _page = _ctx.new_page()
    _attach_page_listener(_page)

    _server = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=_server.serve_forever, daemon=True).start()

    # Signal ready before navigating so bridge.browser_open() returns fast.
    print(json.dumps({"ok": True, "port": _server.server_address[1]}), flush=True)

    try:
        _page.goto(_start_url, wait_until="domcontentloaded", timeout=20000)
    except Exception:
        pass
    try:
        _page.bring_to_front()
    except Exception:
        pass

    while True:
        try:
            _c = _cmd_q.get(timeout=0.2)
        except _q.Empty:
            # Check if the user closed the active tab/page.
            if _page_closed.is_set():
                _page_closed.clear()
                try:
                    # Reopen a blank page so the browser window stays usable.
                    _page = _ctx.new_page()
                    _attach_page_listener(_page)
                except Exception:
                    break  # Browser itself was closed — exit cleanly.
            continue

        if _c.get("t") == "stop":
            break
        try:
            _t = _c["t"]
            if _t == "status":
                _res_q.put({"ok": True, "url": _page.url, "title": _page.title()})
            elif _t == "navigate":
                _page.goto(_c["url"], wait_until="domcontentloaded", timeout=20000)
                _res_q.put({"ok": True, "url": _page.url})
            elif _t == "focus":
                # CDP brings the specific tab to front within its Chrome window.
                _page.bring_to_front()
                # On macOS, activate by the exact PID Playwright launched —
                # not by bundle ID, which would match any running Chrome/Chromium.
                # Only available in non-persistent mode (_browser is not None).
                if sys.platform == "darwin" and _browser:
                    try:
                        from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
                        _chromium_pid = _browser.process.pid
                        _app = NSRunningApplication.runningApplicationWithProcessIdentifier_(_chromium_pid)
                        if _app:
                            _app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
                    except Exception:
                        pass
                _res_q.put({"ok": True})
            elif _t == "detect_fields":
                _forms = _page.evaluate("""(function() {
  var TN={"text":"Text","email":"Email","password":"Password","number":"Number","tel":"Phone","url":"URL","date":"Date","datetime-local":"Date & Time","time":"Time","month":"Month","week":"Week","range":"Range","file":"File Upload","checkbox":"Checkbox","radio":"Radio","color":"Color","search":"Search","textarea":"Long Text","select":"Dropdown"};
  function lbl(el) {
    if (el.id) { try { var L=document.querySelector('label[for="'+el.id+'"]'); if(L) return L.innerText.trim(); } catch(e){} }
    var a=el.getAttribute('aria-label'); if(a) return a.trim();
    var lb=el.getAttribute('aria-labelledby');
    if(lb){var p=lb.split(' ').map(function(i){var d=document.getElementById(i);return d?d.innerText.trim():'';}).filter(function(s){return s;});if(p.length)return p.join(' ');}
    var pl=el.closest('label'); if(pl){var t=pl.innerText.replace(el.value||'','').trim();if(t)return t;}
    return el.placeholder||el.name||el.id||'';
  }
  function hlp(el){var db=el.getAttribute('aria-describedby');if(!db)return '';return db.split(' ').map(function(i){var d=document.getElementById(i);return d?d.innerText.trim():'';}).filter(function(s){return s;}).join(' ');}
  function proc(el){
    var tag=el.tagName.toLowerCase();
    var type=tag==='select'?'select':tag==='textarea'?'textarea':(el.type||'text').toLowerCase();
    if(['hidden','submit','button','reset','image'].indexOf(type)!==-1)return null;
    var f={type:type,typeName:TN[type]||type,label:lbl(el)||'Unlabeled Field',helperText:hlp(el),required:el.required||el.getAttribute('aria-required')==='true',name:el.name||el.id||''};
    if(tag==='select')f.options=Array.prototype.slice.call(el.options,0,25).filter(function(o){return o.value;}).map(function(o){return{value:o.value,text:o.text.trim()};});
    if(type==='file')f.accept=el.getAttribute('accept')||'';
    return f;
  }
  function scan(c){return Array.prototype.slice.call(c.querySelectorAll('input,textarea,select')).map(proc).filter(Boolean);}
  var forms=[];
  var fels=Array.prototype.slice.call(document.querySelectorAll('form'));
  if(fels.length>0)fels.forEach(function(form,i){try{var fields=scan(form);if(!fields.length)return;var name=form.getAttribute('aria-label')||form.getAttribute('name')||form.getAttribute('id')||('Form '+(i+1));forms.push({id:'form-'+i,name:name,fields:fields});}catch(e){}});
  if(!forms.length){try{var loose=Array.prototype.slice.call(document.querySelectorAll('input,textarea,select')).filter(function(el){return !el.closest('form');}).map(proc).filter(Boolean);if(loose.length)forms.push({id:'loose',name:'Application Fields',fields:loose});}catch(e){}}
  return forms;
})()""")
                _res_q.put({"ok": True, "forms": _forms or []})
            elif _t == "scrape":
                # Scrape in a throwaway tab so the user's active page is untouched.
                _sp = _ctx.new_page()
                try:
                    _sp.goto(_c["url"], wait_until="domcontentloaded", timeout=25000)
                    try:
                        _sp.wait_for_timeout(1200)  # let late-rendered content settle
                    except Exception:
                        pass
                    _title = ""
                    try:
                        _title = _sp.title()
                    except Exception:
                        pass
                    _text = _sp.evaluate("() => document.body ? document.body.innerText : ''")
                    _res_q.put({"ok": True, "url": _sp.url, "title": _title, "text": _text or ""})
                finally:
                    try:
                        _sp.close()
                    except Exception:
                        pass
            elif _t == "check_google_login":
                try:
                    _cookies = _ctx.cookies(urls=["https://accounts.google.com", "https://google.com"])
                    _session_names = {"SID", "SAPISID", "__Secure-3PSID", "SSID"}
                    _has_session = any(c["name"] in _session_names for c in _cookies)
                    _email = None
                    if _has_session:
                        _vp = _ctx.new_page()
                        try:
                            _vp.goto("https://accounts.google.com/", wait_until="domcontentloaded", timeout=12000)
                            _vp.wait_for_timeout(1500)
                            _email = _vp.evaluate("""() => {
                                const byData = document.querySelector('[data-email]');
                                if (byData) return byData.getAttribute('data-email');
                                const byAria = document.querySelector('[aria-label*="@"]');
                                if (byAria) return byAria.getAttribute('aria-label');
                                const chip = document.querySelector('.gb_lb, .gb_kb');
                                if (chip) return chip.textContent.trim();
                                return null;
                            }""")
                        except Exception:
                            pass
                        finally:
                            try: _vp.close()
                            except Exception: pass
                    _res_q.put({"ok": True, "logged_in": _has_session, "email": _email})
                except Exception as _ce:
                    _res_q.put({"ok": False, "error": str(_ce)})
            else:
                _res_q.put({"ok": False, "error": "unknown command"})
        except Exception as _e:
            err = str(_e)
            _res_q.put({"ok": False, "error": err})
            # Exit if the browser or context was closed during a command.
            if any(k in err.lower() for k in ("closed", "disconnected", "target")):
                break
'''
