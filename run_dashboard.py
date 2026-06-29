#!/usr/bin/env python3
"""
Environment Downtime Dashboard — Auto-loader
Reads cookies from your Chrome browser to authenticate with ServiceNow.
No credentials needed. No manual steps. Just run it.
"""
import http.server
import json
import os
import re
from urllib.parse import urlparse, parse_qs
import signal
import subprocess
import sys
import webbrowser
import threading
import warnings
from pathlib import Path
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, "reconfigure") else None

PORT = 8080
INSTANCE = "bofi.service-now.com"
CACHE_FILE = Path(__file__).parent / ".snow_cache.json"
CACHE_MAX_AGE = timedelta(hours=2)
SCRIPT_DIR = Path(__file__).parent
DASHBOARD_HTML = "Environment_Downtime_Dashboard.html"
DASHBOARD_URL = "https://camartinezax.github.io/QCoE/Environment_Downtime_Dashboard.html"
COOKIE_CACHE_FILE = Path(__file__).parent / ".snow_cookie_jar.json"
COOKIE_JAR_MAX_AGE = timedelta(hours=12)

# Keep the dashboard resilient when the system Python is removed/updated.
# We prefer running from a local venv under SCRIPT_DIR so pip installs are isolated.
MIN_PYTHON = (3, 11)
VENV_DIR = SCRIPT_DIR / ".venv"


def _venv_python_path():
    return VENV_DIR / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )


def _in_venv():
    return getattr(sys, "base_prefix", sys.prefix) != sys.prefix


def _run(cmd, timeout=None):
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _python_ok(python_path):
    if not python_path:
        return False
    try:
        r = _run(
            [str(python_path), "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
            timeout=5,
        )
        if r.returncode != 0:
            return False
        parts = (r.stdout or "").strip().split()
        if len(parts) < 2:
            return False
        major, minor = int(parts[0]), int(parts[1])
        return (major, minor) >= MIN_PYTHON
    except Exception:
        return False


def _find_bootstrap_python():
    # Prefer user-managed Python (survives CLT removals), then fall back to current.
    candidates = []
    if os.name != "nt":
        candidates.extend(
            [
                Path.home() / ".local" / "bin" / "python3.12",
                Path.home() / ".local" / "bin" / "python3.11",
                Path("/opt/homebrew/bin/python3.12"),
                Path("/usr/local/bin/python3.12"),
            ]
        )
    # Last resort: whatever is running us now.
    candidates.append(Path(sys.executable))

    for c in candidates:
        try:
            if c and c.exists() and _python_ok(c):
                return c
        except Exception:
            continue
    return Path(sys.executable)


def _reexec_into_supported_python_if_needed():
    """If we were started with an unsupported Python (< MIN_PYTHON), try to re-exec into a supported one."""
    if os.environ.get("AXOS_DASHBOARD_NO_PY_REEXEC") == "1":
        return

    current = Path(sys.executable)
    if _python_ok(current):
        return

    # Try to find a supported Python (excluding the current interpreter).
    candidates = []
    if os.name != "nt":
        candidates.extend(
            [
                Path.home() / ".local" / "bin" / "python3.12",
                Path.home() / ".local" / "bin" / "python3.11",
                Path("/opt/homebrew/bin/python3.12"),
                Path("/usr/local/bin/python3.12"),
            ]
        )

    for c in candidates:
        try:
            if c and c.exists() and _python_ok(c):
                os.environ["AXOS_DASHBOARD_NO_PY_REEXEC"] = "1"
                os.execv(str(c), [str(c), os.path.abspath(__file__), *sys.argv[1:]])
        except Exception:
            continue

    print(
        f"{C_RED}ERROR:{C_RESET} Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required. "
        "Please install Python 3.12+ (python.org) or ensure python3.12 is available, then retry."
    )
    raise SystemExit(2)


def _ensure_venv_and_deps():
    """Create/repair a local venv and install required deps into it.

    Returns the venv python path when available, otherwise None.
    """
    vpy = _venv_python_path()
    try:
        if vpy.exists():
            return vpy

        bootstrap_py = _find_bootstrap_python()
        if not _python_ok(Path(bootstrap_py)):
            return None
        print(f"  {C_DIM}  Creating local venv: {VENV_DIR}{C_RESET}")
        r = _run([str(bootstrap_py), "-m", "venv", str(VENV_DIR)], timeout=120)
        if r.returncode != 0:
            return None

        vpy = _venv_python_path()
        if not vpy.exists():
            return None

        # Install deps into the venv (not system Python).
        pip_base = [str(vpy), "-m", "pip", "install", "--disable-pip-version-check", "--no-input"]
        _run(pip_base + ["--upgrade", "pip"], timeout=120)
        deps = ["browser_cookie3", "requests", "pycryptodome", "rookiepy", "websocket-client"]
        r2 = _run(pip_base + deps, timeout=180)
        if r2.returncode != 0:
            # Non-fatal: the dashboard can still serve /api/progress even if fetch deps fail.
            print(f"  {C_DIM}  Note: dependency install had issues; fetch may fail until resolved.{C_RESET}")
        return vpy
    except Exception:
        return None


def _reexec_into_venv_if_present():
    """If a local venv exists, re-exec into it so sys.executable points at venv python."""
    if _in_venv():
        return
    if os.environ.get("AXOS_DASHBOARD_NO_REEXEC") == "1":
        return
    vpy = _venv_python_path()
    if not vpy.exists():
        return
    os.environ["AXOS_DASHBOARD_NO_REEXEC"] = "1"
    os.execv(str(vpy), [str(vpy), os.path.abspath(__file__), *sys.argv[1:]])

SNOW_TABLES = {
    "sc_req_item": {
        "url": f"https://{INSTANCE}/sc_req_item.do",
        "date_field": "opened_at",
        "segments": [""],
    },
    "incident": {
        "url": f"https://{INSTANCE}/incident.do",
        "date_field": "opened_at",
        "segments": [""],
    },
    "change_request": {
        "url": f"https://{INSTANCE}/change_request.do",
        # For downtime scheduling, planned start date is the meaningful timeline.
        "date_field": "start_date",
        "segments": [""],
    },
}
PAGE_SIZE = 500

def _ansi_supported():
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            mode = ctypes.c_ulong()
            h = k.GetStdHandle(-11)
            k.GetConsoleMode(h, ctypes.byref(mode))
            k.SetConsoleMode(h, mode.value | 0x0004)
            return True
        except Exception:
            return False
    return True

if _ansi_supported():
    C_ORANGE = "\033[38;5;214m"
    C_GREEN = "\033[32m"
    C_RED = "\033[31m"
    C_CYAN = "\033[36m"
    C_DIM = "\033[2m"
    C_BOLD = "\033[1m"
    C_RESET = "\033[0m"
else:
    C_ORANGE = C_GREEN = C_RED = C_CYAN = C_DIM = C_BOLD = C_RESET = ""


def banner():
    print(f"""
{C_ORANGE}{C_BOLD}╔══════════════════════════════════════════════╗
║   AXOS — Environment Downtime Dashboard      ║
╚══════════════════════════════════════════════╝{C_RESET}
""")


def check_cache():
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
            if datetime.now() - cached_at < CACHE_MAX_AGE:
                records = data.get("records", [])
                age = datetime.now() - cached_at
                mins = int(age.total_seconds() / 60)
                print(f"  {C_GREEN}✓{C_RESET} Using cached data ({len(records)} tickets, {mins}m old)")
                print(f"  {C_DIM}  Click 'Refresh' in the dashboard to fetch fresh data{C_RESET}")
                return records
            else:
                print(f"  {C_DIM}  Cache expired, fetching fresh data...{C_RESET}")
        except Exception:
            pass
    return None


def save_cache(records):
    CACHE_FILE.write_text(json.dumps({
        "cached_at": datetime.now().isoformat(),
        "records": records
    }))


fetch_progress = {
    "state": "idle",
    "pages_done": 0,
    "records_so_far": 0,
    "message": "",
    "error": None,
}
_fetch_lock = threading.Lock()


_NO_WINDOW = 0x08000000


def _try_cdp_cookies(domain_filter, force_refresh=False):
    """Windows-only: read cookies via Chrome DevTools Protocol.

    Chrome/Edge v130+ use App-Bound Encryption that blocks external cookie
    reads without admin.  CDP bypasses this because the *browser itself*
    decrypts cookies internally.

    Strategy:
      1. Return cached cookies if still valid (skip if *force_refresh*).
      2. Check if any browser is already running with a debug port (free).
      3. If not, restart ONE browser with a debug port.
         The browser restores all previous tabs (``--restore-last-session``).
      4. Read cookies via CDP WebSocket and cache for 12 h.

    Each browser gets its own port so they never collide.
    """
    if os.name != "nt":
        return None

    import requests as rq

    if not force_refresh and COOKIE_CACHE_FILE.exists():
        try:
            data = json.loads(COOKIE_CACHE_FILE.read_text())
            cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
            if datetime.now() - cached_at < COOKIE_JAR_MAX_AGE:
                cookies = data.get("cookies", [])
                if cookies:
                    jar = rq.cookies.RequestsCookieJar()
                    for c in cookies:
                        jar.set(c["name"], c["value"],
                                domain=c["domain"], path=c["path"])
                    sn = [cc for cc in jar if "service-now" in (cc.domain or "")]
                    if sn:
                        print(f"  {C_GREEN}  Using cached cookies "
                              f"({len(sn)} ServiceNow cookies){C_RESET}")
                        return jar
        except Exception:
            pass

    try:
        import websocket  # noqa: F401
    except ImportError:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--quiet",
                 "websocket-client"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW)
        except Exception:
            pass

    import time

    browsers = [
        ("Chrome", "chrome.exe", 9222,
         [os.path.join(os.environ.get("ProgramFiles", ""),
                       "Google", "Chrome", "Application", "chrome.exe"),
          os.path.join(os.environ.get("ProgramFiles(x86)", ""),
                       "Google", "Chrome", "Application", "chrome.exe"),
          os.path.join(os.environ.get("LOCALAPPDATA", ""),
                       "Google", "Chrome", "Application", "chrome.exe")]),
        ("Edge", "msedge.exe", 9223,
         [os.path.join(os.environ.get("ProgramFiles(x86)", ""),
                       "Microsoft", "Edge", "Application", "msedge.exe"),
          os.path.join(os.environ.get("ProgramFiles", ""),
                       "Microsoft", "Edge", "Application", "msedge.exe")]),
    ]

    def _cdp_read_cookies(port, label):
        """Connect to a CDP debug port and return ServiceNow cookies."""
        try:
            import websocket as ws_mod
            ver = rq.get(f"http://localhost:{port}/json/version",
                         timeout=3).json()
            ws_url = ver.get("webSocketDebuggerUrl")
            if not ws_url:
                pages = rq.get(f"http://localhost:{port}/json",
                               timeout=3).json()
                if pages:
                    ws_url = pages[0].get("webSocketDebuggerUrl")
            if not ws_url:
                return None

            ws = ws_mod.create_connection(ws_url, timeout=10)
            ws.send(json.dumps({"method": "Storage.getCookies", "id": 1}))
            resp = json.loads(ws.recv())
            ws.close()

            all_cookies = resp.get("result", {}).get("cookies", [])
            sn = [c for c in all_cookies
                  if domain_filter in c.get("domain", "")]
            if sn:
                print(f"  {C_GREEN}  {label} CDP: {len(sn)} ServiceNow "
                      f"cookies{C_RESET}")
                return sn
            print(f"  {C_DIM}  {label}: no ServiceNow cookies "
                  f"via CDP{C_RESET}")
        except Exception as e:
            print(f"  {C_DIM}  {label} CDP error: "
                  f"{str(e)[:100]}{C_RESET}")
        return None

    # Phase 1: check ports already active (no browser restart needed)
    if not force_refresh:
        for bname, _, port, _ in browsers:
            try:
                rq.get(f"http://localhost:{port}/json/version", timeout=2)
                print(f"  {C_DIM}  {bname} CDP already available "
                      f"on port {port}{C_RESET}")
                sn = _cdp_read_cookies(port, bname)
                if sn:
                    return _cdp_jar(sn)
            except Exception:
                pass

    # Phase 2: restart browsers one at a time until we get cookies
    for bname, proc_name, port, exe_candidates in browsers:
        exe_path = next((p for p in exe_candidates if os.path.isfile(p)),
                        None)
        if not exe_path:
            continue

        print(f"  {C_CYAN}  Briefly restarting {bname} with debug port "
              f"for cookie access...{C_RESET}")
        subprocess.run(["taskkill", "/F", "/IM", proc_name],
                       capture_output=True, creationflags=_NO_WINDOW)
        time.sleep(3)

        subprocess.Popen(
            [exe_path,
             f"--remote-debugging-port={port}",
             "--remote-allow-origins=*",
             "--restore-last-session",
             "--no-first-run"],
            creationflags=0x00000008,
        )

        cdp_ready = False
        for _ in range(20):
            time.sleep(1)
            try:
                rq.get(f"http://localhost:{port}/json/version", timeout=2)
                cdp_ready = True
                break
            except Exception:
                pass

        if not cdp_ready:
            print(f"  {C_DIM}  {bname} CDP did not start{C_RESET}")
            continue

        sn = _cdp_read_cookies(port, bname)
        if sn:
            return _cdp_jar(sn)

    return None


def _cdp_jar(sn_cookies):
    """Turn a list of CDP cookie dicts into a cached RequestsCookieJar."""
    import requests as rq

    cookies_list = [dict(name=c["name"], value=c["value"],
                         domain=c["domain"], path=c.get("path", "/"))
                    for c in sn_cookies]

    COOKIE_CACHE_FILE.write_text(json.dumps({
        "cached_at": datetime.now().isoformat(),
        "cookies": cookies_list,
    }))

    jar = rq.cookies.RequestsCookieJar()
    for c in cookies_list:
        jar.set(c["name"], c["value"],
                domain=c["domain"], path=c["path"])
    return jar


def _do_fetch_background(date_from=None, date_to=None, fetch_filters=None):
    """Background worker: fetches pages and updates fetch_progress."""
    global fetch_progress

    def _update(state, msg="", pages=0, records=0, error=None):
        with _fetch_lock:
            fetch_progress["state"] = state
            fetch_progress["message"] = msg
            fetch_progress["pages_done"] = pages
            fetch_progress["records_so_far"] = records
            fetch_progress["error"] = error

    _do_fetch_background._retried_401 = False

    fetch_filters = fetch_filters or {}
    date_label = ""
    if date_from and date_to:
        date_label = f" ({date_from} to {date_to})"
    ff_parts = []
    if fetch_filters.get("sd"): ff_parts.append(f"short_description~{fetch_filters['sd']}")
    if fetch_filters.get("rb"): ff_parts.append(f"reported_by~{fetch_filters['rb']}")
    if fetch_filters.get("rf"): ff_parts.append(f"requested_for~{fetch_filters['rf']}")
    if fetch_filters.get("rt"): ff_parts.append(f"request_type~{fetch_filters['rt']}")
    if fetch_filters.get("st"): ff_parts.append(f"state~{fetch_filters['st']}")
    adv_raw = fetch_filters.get("adv") or ""
    adv_n = 0
    if isinstance(adv_raw, str) and adv_raw.strip():
        try:
            adv_obj = json.loads(adv_raw)
            adv_n = len((adv_obj or {}).get("rules") or [])
        except Exception:
            adv_n = 0
    if adv_n:
        ff_parts.append(f"adv_rules~{adv_n}")
    ff_label = (" | " + ", ".join(ff_parts)) if ff_parts else ""
    print(f"  {C_CYAN}⟳ Fetching data from ServiceNow{date_label}{ff_label}...{C_RESET}")

    _update("fetching", "Reading browser cookies...")

    _install_deps = [sys.executable, "-m", "pip", "install", "--quiet"]

    try:
        import browser_cookie3
    except ImportError:
        subprocess.check_call(_install_deps + ["browser_cookie3", "requests", "pycryptodome"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import browser_cookie3

    try:
        from Crypto.Cipher import AES  # noqa: F401
    except ImportError:
        subprocess.check_call(_install_deps + ["pycryptodome"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    rookiepy_available = False
    try:
        import rookiepy
        rookiepy_available = True
    except ImportError:
        try:
            subprocess.check_call(_install_deps + ["rookiepy"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            import rookiepy
            rookiepy_available = True
        except Exception:
            pass

    import requests as req

    def _copy_cookie_db(browser_name):
        """Copy locked cookie DB to temp so we can read while browser is open."""
        import shutil, tempfile
        if os.name != "nt":
            return None
        local = os.environ.get("LOCALAPPDATA", "")
        if not local:
            return None
        paths = {
            "Chrome": os.path.join(local, "Google", "Chrome", "User Data"),
            "Edge": os.path.join(local, "Microsoft", "Edge", "User Data"),
        }
        ud = paths.get(browser_name)
        if not ud or not os.path.isdir(ud):
            return None
        for profile in ["Default", "Profile 1", "Profile 2", "Profile 3"]:
            for sub in ["Network", ""]:
                cookie_file = os.path.join(ud, profile, sub, "Cookies") if sub else os.path.join(ud, profile, "Cookies")
                if os.path.isfile(cookie_file):
                    try:
                        tmp = os.path.join(tempfile.gettempdir(), f"snow_{browser_name.lower()}_cookies")
                        shutil.copy2(cookie_file, tmp)
                        for wal_ext in ["-wal", "-shm"]:
                            src = cookie_file + wal_ext
                            if os.path.isfile(src):
                                shutil.copy2(src, tmp + wal_ext)
                        return tmp
                    except Exception:
                        continue
        return None

    def _try_rookiepy(domain):
        """Try rookiepy as fallback — better Windows cookie support."""
        if not rookiepy_available:
            return None
        for fn_name in ["chrome", "edge", "firefox", "chromium"]:
            fn = getattr(rookiepy, fn_name, None)
            if not fn:
                continue
            try:
                cookies = fn([domain])
                if cookies:
                    jar = req.cookies.RequestsCookieJar()
                    for c in cookies:
                        jar.set(c.get("name", ""), c.get("value", ""),
                                domain=c.get("domain", ""), path=c.get("path", "/"))
                    sn = [cc for cc in jar if "service-now" in (cc.domain or "")]
                    if sn:
                        print(f"  {C_GREEN}  rookiepy/{fn_name} found {len(sn)} cookies{C_RESET}")
                        return jar
            except Exception:
                continue
        return None

    browsers = [
        ("Chrome", browser_cookie3.chrome),
        ("Edge", browser_cookie3.edge),
        ("Firefox", browser_cookie3.firefox),
    ]

    cj = None
    sn_cookies = []
    browser_errors = []
    for browser_name, browser_fn in browsers:
        try:
            _update("fetching", f"Trying {browser_name} cookies...")
            print(f"  {C_DIM}  Trying {browser_name}...{C_RESET}", end=" ")
            try:
                jar = browser_fn(domain_name=".service-now.com")
            except Exception as first_err:
                tmp_path = _copy_cookie_db(browser_name)
                if tmp_path:
                    print(f"{C_DIM}retrying with copy...{C_RESET}", end=" ")
                    jar = browser_fn(cookie_file=tmp_path, domain_name=".service-now.com")
                else:
                    raise first_err
            found = [c for c in jar if "service-now" in c.domain]
            if found:
                cj = jar
                sn_cookies = found
                print(f"{C_GREEN}✓ {len(found)} cookies{C_RESET}")
                break
            else:
                msg = f"{browser_name}: no ServiceNow cookies"
                browser_errors.append(msg)
                print(f"{C_DIM}no ServiceNow cookies{C_RESET}")
        except Exception as e:
            msg = f"{browser_name}: {str(e)[:120]}"
            browser_errors.append(msg)
            print(f"{C_DIM}failed ({str(e)[:80]}){C_RESET}")
            continue

    if not sn_cookies and rookiepy_available:
        print(f"  {C_CYAN}  Trying rookiepy fallback...{C_RESET}")
        _update("fetching", "Trying rookiepy fallback...")
        cj = _try_rookiepy(".service-now.com")
        if cj:
            sn_cookies = [c for c in cj if "service-now" in (c.domain or "")]

    if not sn_cookies and os.name == "nt":
        _update("fetching", "Reading cookies via browser DevTools...")
        cj = _try_cdp_cookies("service-now")
        if cj:
            sn_cookies = [c for c in cj if "service-now" in (c.domain or "")]

    if not cj or not sn_cookies:
        hint = ("No ServiceNow cookies found. "
                "Please open bofi.service-now.com in Chrome or Edge, "
                "log in, then click Refresh in the dashboard.")
        _update("error", error=hint)
        print(f"  {C_RED}✗ No cookies found{C_RESET}")
        for err in browser_errors:
            print(f"  {C_RED}  • {err}{C_RESET}")
        return

    print(f"  {C_DIM}  Found {len(sn_cookies)} session cookies{C_RESET}")

    headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}

    # Parse advanced rules once (if provided)
    adv_raw_global = fetch_filters.get("adv") or ""
    adv_rules_global = []
    if isinstance(adv_raw_global, str) and adv_raw_global.strip():
        try:
            adv_obj = json.loads(adv_raw_global) or {}
            rr = adv_obj.get("rules") or []
            if isinstance(rr, list):
                adv_rules_global = [r for r in rr if isinstance(r, dict)]
        except Exception:
            adv_rules_global = []

    def _date_clause_for(field_name: str) -> str:
        clause = ""
        if date_from:
            # Use ServiceNow's dateGenerate to avoid UTC/local day-boundary bleed
            # (e.g., 2026-01-01 00:00 UTC showing as Dec 31 in Pacific time).
            clause += f"^{field_name}>=javascript:gs.dateGenerate('{date_from}','00:00:00')"
        if date_to:
            next_day = (datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            clause += f"^{field_name}<javascript:gs.dateGenerate('{next_day}','00:00:00')"
        return clause

    def _normalize_query(q: str) -> str:
        return (q or "").lstrip("^")

    def _clean_user_text(s: str) -> str:
        # Prevent encoded-query delimiter injection.
        return (s or "").replace("^", " ").replace("\n", " ").replace("\r", " ").strip()

    def _build_fetch_filter_clause(table_name: str) -> str:
        sd = _clean_user_text(fetch_filters.get("sd", ""))
        rb = _clean_user_text(fetch_filters.get("rb", ""))
        rf = _clean_user_text(fetch_filters.get("rf", ""))
        rt = _clean_user_text(fetch_filters.get("rt", ""))
        st = _clean_user_text(fetch_filters.get("st", ""))
        adv_raw = fetch_filters.get("adv", "")

        clause = ""

        if sd:
            clause += f"^short_descriptionLIKE{sd}"

        # People fields differ by table
        if rb:
            # "Reported By" maps best to opened_by display name for both tables
            clause += f"^opened_by.nameLIKE{rb}"
        if rf:
            if table_name == "incident":
                clause += f"^caller_id.nameLIKE{rf}"
            elif table_name == "change_request":
                clause += f"^requested_by.nameLIKE{rf}"
            else:
                clause += f"^requested_for.nameLIKE{rf}"

        if rt:
            if table_name == "incident":
                clause += f"^categoryLIKE{rt}"
            elif table_name == "change_request":
                # Best-effort mapping: change type/model varies by org; LIKE keeps it usable.
                clause += f"^typeLIKE{rt}"
            else:
                clause += f"^cat_item.nameLIKE{rt}"

        if st:
            # Choice fields are instance-specific; LIKE works best with labels.
            clause += f"^stateLIKE{st}"

        # Advanced AND/OR builder (optional)
        adv_clause = ""
        if isinstance(adv_raw, str) and adv_raw.strip():
            try:
                adv_obj = json.loads(adv_raw) or {}
                rules = adv_obj.get("rules") or []
                if not isinstance(rules, list):
                    rules = []
            except Exception:
                rules = []

            FIELD_RE = re.compile(r"^[A-Za-z0-9_.]+$")
            RAW_OP_RE = re.compile(r"^[A-Za-z0-9_ !><=]+$")

            def _safe_field(name: str):
                name = (name or "").strip()
                if not name or any(c.isspace() for c in name):
                    return None
                if "^" in name or "\n" in name or "\r" in name:
                    return None
                if not FIELD_RE.match(name):
                    return None
                return name

            def _safe_date(d: str):
                d = (d or "").strip()
                try:
                    datetime.strptime(d, "%Y-%m-%d")
                    return d
                except Exception:
                    return None

            def _safe_value(v: str, max_len: int = 240):
                v = _clean_user_text(v)
                if len(v) > max_len:
                    v = v[:max_len]
                return v

            def _safe_raw_op(op: str):
                op = (op or "").replace("^", " ").replace("\n", " ").replace("\r", " ").strip()
                if not op or len(op) > 40:
                    return None
                if not RAW_OP_RE.match(op):
                    return None
                return op

            SYSID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
            TEXT_OPS = {"like", "not_like", "eq", "neq", "startswith", "endswith"}
            # Choice fields where users typically type the display label (not the internal value).
            CHOICE_LABEL_FIELDS = {"state", "approval", "impact", "urgency", "priority", "risk", "phase_state", "type"}

            def _maybe_dotwalk_name(field_name: str, op_key: str, user_val: str) -> str:
                # IMPORTANT: Do not auto-dotwalk. Users can specify dot-walk fields explicitly.
                return field_name

            # Field existence cache (to avoid invalid-field queries wiping out entire tables)
            _field_keys_cache = {}

            def _get_field_keys(table: str):
                if table in _field_keys_cache:
                    return _field_keys_cache[table]
                cfg = SNOW_TABLES.get(table) or {}
                table_url = cfg.get("url")
                date_field = cfg.get("date_field") or "opened_at"
                keys = None
                if table_url:
                    try:
                        resp = req.get(
                            table_url,
                            params={
                                "JSONv2": "",
                                "sysparm_query": f"sys_idISNOTEMPTY^ORDERBYDESC{date_field}^ORDERBYDESCsys_id",
                                "displayvalue": "true",
                                "sysparm_record_count": "1",
                            },
                            cookies=cj,
                            headers=headers,
                            timeout=30,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            recs = data.get("records", data.get("result", [])) or []
                            if recs and isinstance(recs[0], dict):
                                keys = set(recs[0].keys())
                    except Exception:
                        keys = None
                _field_keys_cache[table] = keys
                return keys

            def _field_exists(table: str, element: str) -> bool:
                if not element:
                    return False
                if element == "123TEXTQUERY321":
                    return True
                if "." in element:
                    root = element.split(".", 1)[0]
                    keys = _get_field_keys(table)
                    if keys is None:
                        return True
                    return root in keys
                keys = _get_field_keys(table)
                if keys is None:
                    # If we can't determine, don't block it (best effort).
                    return True
                return element in keys

            # Choice label -> internal value resolution.
            # We cannot rely on sys_choice (often ACL-restricted), so we infer by sampling recent records:
            # - find a record where display label matches
            # - refetch same sys_id with displayvalue=all to get internal numeric value
            _choice_label_cache = {}

            def _disp_str(v):
                if v is None:
                    return ""
                if isinstance(v, dict):
                    return str(v.get("display_value") or v.get("value") or "")
                return str(v)

            def _get_choice_internal(table: str, element: str, label: str):
                lbl = (label or "").strip().lower()
                if not lbl or not table or not element:
                    return None
                ck = (table, element, lbl)
                if ck in _choice_label_cache:
                    return _choice_label_cache[ck]

                cfg = SNOW_TABLES.get(table) or {}
                table_url = cfg.get("url")
                date_field = cfg.get("date_field") or "opened_at"
                if not table_url:
                    _choice_label_cache[ck] = None
                    return None

                # Pull a recent sample and look for label match.
                sys_id = None
                try:
                    resp = req.get(
                        table_url,
                        params={
                            "JSONv2": "",
                            "sysparm_query": f"sys_idISNOTEMPTY^ORDERBYDESC{date_field}^ORDERBYDESCsys_id",
                            "displayvalue": "true",
                            "sysparm_record_count": "500",
                        },
                        cookies=cj,
                        headers=headers,
                        timeout=60,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        recs = data.get("records", data.get("result", [])) or []
                        for r0 in recs:
                            if not isinstance(r0, dict):
                                continue
                            disp = _disp_str(r0.get(element)).strip().lower()
                            if not disp:
                                continue
                            if disp == lbl or lbl in disp or disp in lbl:
                                sys_id = _disp_str(r0.get("sys_id")).strip()
                                break
                except Exception:
                    sys_id = None

                if not sys_id:
                    _choice_label_cache[ck] = None
                    return None

                # Fetch internal value for that sys_id.
                internal = None
                try:
                    resp2 = req.get(
                        table_url,
                        params={
                            "JSONv2": "",
                            "sysparm_query": f"sys_id={sys_id}",
                            "displayvalue": "all",
                            "sysparm_record_count": "1",
                        },
                        cookies=cj,
                        headers=headers,
                        timeout=30,
                    )
                    if resp2.status_code == 200:
                        data2 = resp2.json()
                        recs2 = data2.get("records", data2.get("result", [])) or []
                        if recs2 and isinstance(recs2[0], dict):
                            internal = _disp_str(recs2[0].get(element)).strip()
                except Exception:
                    internal = None

                _choice_label_cache[ck] = internal or None
                return internal

            first = True
            for r in (rules[:25] if isinstance(rules, list) else []):
                if not isinstance(r, dict):
                    continue
                join = (r.get("join") or "AND").upper()
                join = "OR" if join == "OR" else "AND"
                op = (r.get("op") or "like").strip().lower()
                field = _safe_field(r.get("field") or "")
                if not field:
                    continue
                if not _field_exists(table_name, field):
                    continue

                enc = ""
                if op in ("isempty", "isnotempty"):
                    enc = f"{field}{'ISEMPTY' if op == 'isempty' else 'ISNOTEMPTY'}"
                elif op == "between":
                    d1 = _safe_date(r.get("valueFrom") or "")
                    d2 = _safe_date(r.get("valueTo") or "")
                    if d1 and d2:
                        enc = (f"{field}BETWEENjavascript:gs.dateGenerate('{d1}','00:00:00')"
                               f"@javascript:gs.dateGenerate('{d2}','23:59:59')")
                elif op in ("on", "noton"):
                    d = _safe_date(r.get("value") or "")
                    if d:
                        token = "ON" if op == "on" else "NOTON"
                        enc = (f"{field}{token}{d}"
                               f"@javascript:gs.dateGenerate('{d}','start')"
                               f"@javascript:gs.dateGenerate('{d}','end')")
                elif op in ("before", "at_or_before", "after", "at_or_after"):
                    v = _safe_value(r.get("value") or "")
                    if v:
                        sym = {
                            "before": "<",
                            "at_or_before": "<=",
                            "after": ">",
                            "at_or_after": ">=",
                        }[op]
                        enc = f"{field}{sym}{v}"
                elif op == "like":
                    v = _safe_value(r.get("value") or "")
                    if v:
                        f2 = _maybe_dotwalk_name(field, "like", v)
                        enc = f"{f2}LIKE{v}"
                elif op == "not_like":
                    v = _safe_value(r.get("value") or "")
                    if v:
                        f2 = _maybe_dotwalk_name(field, "not_like", v)
                        enc = f"{f2}NOTLIKE{v}"
                elif op == "eq":
                    v = _safe_value(r.get("value") or "")
                    if v:
                        # For choice fields, treat user input as display label by default.
                        if field in CHOICE_LABEL_FIELDS and (not v.isdigit()) and (not SYSID_RE.match(v)):
                            enc = f"{field}LIKE{v}"
                        else:
                            f2 = _maybe_dotwalk_name(field, "eq", v)
                            enc = f"{f2}={v}"
                elif op == "neq":
                    v = _safe_value(r.get("value") or "")
                    if v:
                        # For choice fields, treat user input as display label by default.
                        if field in CHOICE_LABEL_FIELDS and (not v.isdigit()) and (not SYSID_RE.match(v)):
                            enc = f"{field}NOTLIKE{v}"
                        else:
                            f2 = _maybe_dotwalk_name(field, "neq", v)
                            enc = f"{f2}!={v}"
                elif op == "startswith":
                    v = _safe_value(r.get("value") or "")
                    if v:
                        f2 = _maybe_dotwalk_name(field, "startswith", v)
                        enc = f"{f2}STARTSWITH{v}"
                elif op == "endswith":
                    v = _safe_value(r.get("value") or "")
                    if v:
                        f2 = _maybe_dotwalk_name(field, "endswith", v)
                        enc = f"{f2}ENDSWITH{v}"
                elif op == "in":
                    v = _safe_value(r.get("value") or "", max_len=400)
                    if v:
                        enc = f"{field}IN{v}"
                elif op == "not_in":
                    v = _safe_value(r.get("value") or "", max_len=400)
                    if v:
                        enc = f"{field}NOT IN{v}"
                elif op == "custom":
                    raw_op = _safe_raw_op(r.get("rawOp") or "")
                    v = _safe_value(r.get("value") or "", max_len=400)
                    if raw_op and v:
                        enc = f"{field}{raw_op}{v}"

                if not enc:
                    continue

                if first:
                    adv_clause += f"^{enc}"
                    first = False
                else:
                    adv_clause += (f"^OR{enc}" if join == "OR" else f"^{enc}")

        return clause + adv_clause

    all_records = []
    page_num = 0
    seen_ids = set()

    # In advanced mode, only query tables that can support the requested fields.
    adv_fields_needed = []
    if adv_rules_global:
        FIELD_RE2 = re.compile(r"^[A-Za-z0-9_.]+$")
        for r in adv_rules_global[:50]:
            f = (r.get("field") or "").strip()
            if not f:
                continue
            if "^" in f or "\n" in f or "\r" in f or any(c.isspace() for c in f):
                continue
            if not FIELD_RE2.match(f):
                continue
            adv_fields_needed.append(f)
        # de-dupe preserving order
        seen_f = set()
        adv_fields_needed = [f for f in adv_fields_needed if not (f in seen_f or seen_f.add(f))]

    table_keys_cache = {}

    def _get_any_record_keys(table_name: str):
        if table_name in table_keys_cache:
            return table_keys_cache[table_name]
        cfg = SNOW_TABLES.get(table_name) or {}
        table_url = cfg.get("url")
        date_field = cfg.get("date_field") or "opened_at"
        keys = None
        if table_url:
            try:
                resp = req.get(
                    table_url,
                    params={
                        "JSONv2": "",
                        "sysparm_query": f"sys_idISNOTEMPTY^ORDERBYDESC{date_field}^ORDERBYDESCsys_id",
                        "displayvalue": "true",
                        "sysparm_record_count": "1",
                    },
                    cookies=cj,
                    headers=headers,
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    recs = data.get("records", data.get("result", [])) or []
                    if recs and isinstance(recs[0], dict):
                        keys = set(recs[0].keys())
            except Exception:
                keys = None
        table_keys_cache[table_name] = keys
        return keys

    def _table_supports_adv(table_name: str) -> bool:
        if not adv_fields_needed:
            return True
        keys = _get_any_record_keys(table_name)
        if keys is None:
            # Best-effort: if we can't determine, don't exclude.
            return True
        for f in adv_fields_needed:
            if f == "123TEXTQUERY321":
                continue
            if "." in f:
                root = f.split(".", 1)[0]
                if root not in keys:
                    return False
                continue
            if f not in keys:
                return False
        return True

    for table_name, table_cfg in SNOW_TABLES.items():
        if adv_rules_global and not _table_supports_adv(table_name):
            continue
        table_url = table_cfg["url"]
        date_field = table_cfg.get("date_field") or "opened_at"
        date_clause = _date_clause_for(date_field)
        fetch_clause = _build_fetch_filter_clause(table_name)

        # Filters-only query (no ORDERBY) so we can duplicate it in ^NQ segments safely.
        segs = [s for s in table_cfg["segments"] if s]
        if segs:
            filters_only = "^NQ".join(_normalize_query(seg + date_clause + fetch_clause) for seg in segs)
        else:
            filters_only = _normalize_query(date_clause + fetch_clause)

        # Always enforce deterministic ordering so cursor pagination is stable.
        order_by = f"^ORDERBYDESC{date_field}^ORDERBYDESCsys_id"

        def _build_page_query(last_opened=None, last_sys_id=None):
            base = filters_only
            if last_opened and last_sys_id:
                # Fetch "older" than the last row, with a tie-breaker on sys_id.
                # Segment A: opened_at < last_opened
                if base:
                    seg_a = _normalize_query(f"{base}^{date_field}<{last_opened}")
                    seg_b = _normalize_query(f"{base}^{date_field}={last_opened}^sys_id<{last_sys_id}")
                else:
                    seg_a = _normalize_query(f"{date_field}<{last_opened}")
                    seg_b = _normalize_query(f"{date_field}={last_opened}^sys_id<{last_sys_id}")
                # Segment B: opened_at == last_opened AND sys_id < last_sys_id
                return f"{seg_a}^NQ{seg_b}{order_by}"
            return f"{_normalize_query(base)}{order_by}" if base else _normalize_query(order_by)

        page_query = _build_page_query()

        print(f"  {C_CYAN}  Querying {table_name}...{C_RESET}")
        print(f"  {C_DIM}  Query: {page_query}{C_RESET}")
        _update("fetching", f"Querying {table_name}...", pages=page_num, records=len(all_records))

        stale_streak = 0
        prev_unique = len(all_records)
        last_cursor = None
        while True:
            page_num += 1
            _update("fetching",
                    f"{table_name} — requesting page {page_num}...",
                    pages=page_num,
                    records=len(all_records))
            params = {
                "JSONv2": "",
                "sysparm_query": page_query,
                "displayvalue": "true",
                "sysparm_record_count": str(PAGE_SIZE),
            }
            resp = None
            for attempt in range(3):
                try:
                    # Larger JSONv2 pages can be slow; keep a bit of buffer.
                    resp = req.get(table_url, params=params, cookies=cj,
                                   headers=headers, timeout=90)
                    break
                except (req.exceptions.ConnectionError,
                        req.exceptions.ChunkedEncodingError) as e:
                    if attempt < 2:
                        import time as _t
                        _t.sleep(2 ** attempt)
                        continue
                    _update("error",
                            error="Cannot reach ServiceNow. Check your network.")
                    return
                except req.exceptions.Timeout:
                    if attempt < 2:
                        continue
                    _update("error",
                            error="Request timed out. ServiceNow may be slow.")
                    return
            if resp is None:
                return

            if resp.status_code == 401:
                if COOKIE_CACHE_FILE.exists():
                    try:
                        COOKIE_CACHE_FILE.unlink()
                    except OSError:
                        pass
                if not getattr(_do_fetch_background, "_retried_401", False):
                    _do_fetch_background._retried_401 = True
                    print(f"  {C_CYAN}  Session expired — "
                          f"refreshing cookies...{C_RESET}")
                    _update("fetching",
                            "Session expired, refreshing cookies...")
                    fresh_jar = _try_cdp_cookies("service-now",
                                                 force_refresh=True)
                    if fresh_jar:
                        fresh_sn = [c for c in fresh_jar
                                    if "service-now" in (c.domain or "")]
                        if fresh_sn:
                            cj = fresh_jar
                            sn_cookies = fresh_sn
                            print(f"  {C_GREEN}  Got fresh cookies — "
                                  f"retrying...{C_RESET}")
                            continue
                _update("error",
                        error="Session expired. Open ServiceNow in "
                              "Chrome/Edge to refresh, then retry.")
                print(f"  {C_RED}✗ Session expired (HTTP 401){C_RESET}")
                return
            if resp.status_code != 200:
                _update("error", error=f"ServiceNow returned HTTP {resp.status_code}")
                return

            try:
                data = resp.json()
            except json.JSONDecodeError:
                _update("error", error="ServiceNow returned non-JSON response")
                return

            page = data.get("records", data.get("result", [])) or []
            for rec in page:
                sys_id = rec.get("sys_id") or ""
                rid = f"{table_name}:{sys_id}" if sys_id else f"{table_name}:{rec.get('number','')}"
                if not rid or rid in seen_ids:
                    continue
                seen_ids.add(rid)
                rec["_source_table"] = table_name
                all_records.append(rec)

            print(f"  {C_DIM}  {table_name} page {page_num}: {len(page)} records (total unique: {len(all_records)}){C_RESET}")
            _update("fetching", f"{table_name} — {len(all_records)} records so far...",
                    pages=page_num, records=len(all_records))

            if len(page) < PAGE_SIZE:
                break

            tail = page[-1] if page else None
            tail_opened = (tail.get(date_field) if tail else None) or ""
            tail_sys_id = (tail.get("sys_id") if tail else None) or ""
            if not tail_opened or not tail_sys_id:
                # Can't advance cursor safely.
                break

            new_cursor = (tail_opened, tail_sys_id)
            page_query = _build_page_query(tail_opened, tail_sys_id)

            cur_unique = len(all_records)
            cursor_same = (last_cursor == new_cursor)
            unique_same = (cur_unique == prev_unique)
            if cursor_same or unique_same:
                stale_streak += 1
            else:
                stale_streak = 0
            last_cursor = new_cursor
            prev_unique = cur_unique

            if stale_streak >= 5:
                print(f"  {C_DIM}  No new records after {stale_streak} pages "
                      f"for {table_name}, moving on{C_RESET}")
                break

            if page_num >= 200:
                print(f"  {C_DIM}  Hit page limit for {table_name}{C_RESET}")
                break

    DashboardHandler.cached_data = all_records
    save_cache(all_records)
    _update("done", f"Fetched {len(all_records)} tickets", pages=page_num, records=len(all_records))
    print(f"  {C_GREEN}✓ Fetched {len(all_records)} tickets from ServiceNow{C_RESET}")


def start_fetch(date_from=None, date_to=None, fetch_filters=None):
    """Kick off a background fetch. Returns immediately."""
    with _fetch_lock:
        if fetch_progress["state"] == "fetching":
            return False
    fetch_progress["state"] = "fetching"
    fetch_progress["pages_done"] = 0
    fetch_progress["records_so_far"] = 0
    fetch_progress["error"] = None
    fetch_progress["message"] = "Starting..."
    t = threading.Thread(target=_do_fetch_background, args=(date_from, date_to, fetch_filters), daemon=True)
    t.start()
    return True


def ensure_dashboard_html():
    """Download the dashboard HTML from GitHub Pages if not present locally."""
    html_path = SCRIPT_DIR / DASHBOARD_HTML
    if html_path.exists():
        return True
    print(f"  {C_CYAN}⟳ Dashboard HTML not found locally. Downloading from GitHub Pages...{C_RESET}")
    try:
        from urllib.request import urlopen, Request
        req = Request(DASHBOARD_URL, headers={"User-Agent": "AxosDashboard/1.0"})
        resp = urlopen(req, timeout=15)
        html_path.write_bytes(resp.read())
        print(f"  {C_GREEN}✓ Downloaded {DASHBOARD_HTML}{C_RESET}")
        return True
    except Exception as e:
        print(f"  {C_RED}✗ Could not download dashboard: {e}{C_RESET}")
        return False


class ReuseHTTPServer(http.server.HTTPServer):
    allow_reuse_address = True

class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    cached_data = None

    def _parse_date_params(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        return {
            "from": qs.get("from", [None])[0],
            "to": qs.get("to", [None])[0],
            "sd": qs.get("sd", [""])[0] or "",
            "rb": qs.get("rb", [""])[0] or "",
            "rf": qs.get("rf", [""])[0] or "",
            "rt": qs.get("rt", [""])[0] or "",
            "st": qs.get("st", [""])[0] or "",
            "adv": qs.get("adv", [""])[0] or "",
        }

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "":
            self.send_response(302)
            self.send_header("Location", f"/{DASHBOARD_HTML}")
            self.end_headers()
            return
        if parsed.path == f"/{DASHBOARD_HTML}":
            html_path = SCRIPT_DIR / DASHBOARD_HTML
            if not html_path.exists():
                ensure_dashboard_html()
            if html_path.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(html_path.read_bytes())
                return
        if parsed.path == "/api/snow":
            self.send_json({"result": self.cached_data or []})
            return
        if parsed.path == "/api/refresh":
            params = self._parse_date_params()
            started = start_fetch(params["from"], params["to"], params)
            self.send_json({"status": "started" if started else "already_running"})
            return
        if parsed.path == "/api/progress":
            with _fetch_lock:
                prog = dict(fetch_progress)
            if prog["state"] == "done":
                prog["result"] = DashboardHandler.cached_data or []
            self.send_json(prog)
            return
        if parsed.path == "/api/update-dashboard":
            html_path = SCRIPT_DIR / DASHBOARD_HTML
            if html_path.exists():
                html_path.unlink()
            ok = ensure_dashboard_html()
            self.send_json({"ok": ok, "message": "Updated" if ok else "Failed to download"})
            return
        super().do_GET()

    def do_POST(self):
        if self.path == "/api/receive":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body)
            records = data.get("result", data.get("records", []))
            DashboardHandler.cached_data = records
            save_cache(records)
            self.send_json({"ok": True, "count": len(records)})
            return

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def send_json(self, obj):
        payload = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass


def main():
    banner()

    ensure_dashboard_html()

    records = check_cache()
    if records:
        DashboardHandler.cached_data = records
    else:
        print(f"  {C_DIM}  No cache — the dashboard will fetch data with your selected date range.{C_RESET}")

    os.chdir(SCRIPT_DIR)

    try:
        if os.name == "nt":
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True
            )
            for line in result.stdout.splitlines():
                if f":{PORT}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit() and int(pid) != os.getpid():
                        subprocess.run(["taskkill", "/F", "/PID", pid],
                                       capture_output=True)
                        print(f"  {C_DIM}  Killed previous server on port {PORT} (pid {pid}){C_RESET}")
        else:
            result = subprocess.run(
                ["lsof", "-ti", f":{PORT}"], capture_output=True, text=True
            )
            for pid in result.stdout.strip().split():
                if pid and pid != str(os.getpid()):
                    os.kill(int(pid), signal.SIGKILL)
                    print(f"  {C_DIM}  Killed previous server on port {PORT} (pid {pid}){C_RESET}")
    except Exception:
        pass

    server = ReuseHTTPServer(("", PORT), DashboardHandler)

    url = f"http://localhost:{PORT}/Environment_Downtime_Dashboard.html"
    print(f"\n  {C_GREEN}{'✓' if records else '⚠'} Dashboard running at:{C_RESET} {C_BOLD}{url}{C_RESET}")
    if records:
        print(f"  {C_GREEN}  {len(records)} tickets loaded and ready{C_RESET}")
    print(f"  {C_DIM}  Press Ctrl+C to stop{C_RESET}\n")

    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n  {C_ORANGE}Dashboard stopped.{C_RESET}\n")
        server.server_close()


def run_daemon():
    """Run in background mode: no banner, no browser, just serve."""
    ensure_dashboard_html()
    records = check_cache()
    if records:
        DashboardHandler.cached_data = records
    os.chdir(SCRIPT_DIR)
    try:
        if os.name == "nt":
            result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
            for line in result.stdout.splitlines():
                if f":{PORT}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit() and int(pid) != os.getpid():
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        else:
            result = subprocess.run(["lsof", "-ti", f":{PORT}"], capture_output=True, text=True)
            for pid in result.stdout.strip().split():
                if pid and pid != str(os.getpid()):
                    os.kill(int(pid), signal.SIGKILL)
    except Exception:
        pass
    server = ReuseHTTPServer(("", PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


def install_autostart():
    """Install as auto-start background service."""
    script_path = os.path.abspath(__file__)
    vpy = _ensure_venv_and_deps()
    python_path = str(vpy) if vpy else sys.executable

    if sys.platform == "darwin":
        plist_name = "com.axos.snow-dashboard.plist"
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_path = plist_dir / plist_name
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{plist_name.replace('.plist','')}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{script_path}</string>
        <string>--daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/snow-dashboard.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/snow-dashboard.log</string>
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>"""
        plist_path.write_text(plist_content)
        subprocess.run(["launchctl", "unload", str(plist_path)],
                       capture_output=True)
        subprocess.run(["launchctl", "load", str(plist_path)])
        print(f"\n  {C_GREEN}✓ Installed as background service{C_RESET}")
        print(f"  {C_DIM}  The dashboard proxy will auto-start on login.{C_RESET}")
        print(f"  {C_DIM}  To uninstall: launchctl unload {plist_path}{C_RESET}\n")

        # Best-effort: wait briefly for the daemon to be reachable so automation can curl /api/progress.
        try:
            from urllib.request import urlopen
            from urllib.error import URLError

            for _ in range(30):
                try:
                    with urlopen(f"http://localhost:{PORT}/api/progress", timeout=2) as resp:
                        if 200 <= getattr(resp, "status", 200) < 300:
                            break
                except URLError:
                    pass
                except Exception:
                    pass
                import time

                time.sleep(1)
        except Exception:
            pass

    elif os.name == "nt":
        bat_dir = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        bat_path = bat_dir / "snow_dashboard.bat"
        bat_content = (f'@echo off\n'
                       f'set PYTHONUTF8=1\n'
                       f'set PYTHONIOENCODING=utf-8\n'
                       f'start /B "" "{python_path}" "{script_path}" --daemon\n')
        bat_path.write_text(bat_content)
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        subprocess.Popen(
            [python_path, script_path, "--daemon"],
            creationflags=0x00000008,
            close_fds=True,
            env=env,
        )
        print(f"\n  {C_GREEN}✓ Installed as startup service{C_RESET}")
        print(f"  {C_DIM}  The dashboard proxy will auto-start on login.{C_RESET}")
        print(f"  {C_DIM}  To uninstall: delete {bat_path}{C_RESET}\n")
    else:
        print(f"  {C_RED}Auto-start not supported on this OS.{C_RESET}")
        print(f"  {C_DIM}  Run '{python_path} {script_path} --daemon &' manually.{C_RESET}\n")


if __name__ == "__main__":
    _reexec_into_venv_if_present()
    if "--daemon" in sys.argv:
        run_daemon()
    elif "--install" in sys.argv:
        install_autostart()
    else:
        main()
