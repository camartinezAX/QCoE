#!/usr/bin/env python3
"""
Environment Downtime Dashboard — Auto-loader
Reads cookies from your Chrome browser to authenticate with ServiceNow.
No credentials needed. No manual steps. Just run it.
"""
import http.server
import json
import os
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

SNOW_TABLES = {
    "sc_req_item": {
        "url": f"https://{INSTANCE}/sc_req_item.do",
        "segments": [
            "cat_item.name=Site Reliability Request",
            "short_descriptionLIKEdown",
            "short_descriptionLIKEoutage",
            "short_descriptionLIKEenvironment",
            "short_descriptionLIKEdisruption",
            "short_descriptionLIKEunavailable",
        ],
    },
    "incident": {
        "url": f"https://{INSTANCE}/incident.do",
        "segments": [
            "short_descriptionLIKEdown",
            "short_descriptionLIKEoutage",
            "short_descriptionLIKEenvironment",
            "short_descriptionLIKEdisruption",
            "short_descriptionLIKEunavailable",
            "short_descriptionLIKEissue",
        ],
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


def _do_fetch_background(date_from=None, date_to=None):
    """Background worker: fetches pages and updates fetch_progress."""
    global fetch_progress

    def _update(state, msg="", pages=0, records=0, error=None):
        with _fetch_lock:
            fetch_progress["state"] = state
            fetch_progress["message"] = msg
            fetch_progress["pages_done"] = pages
            fetch_progress["records_so_far"] = records
            fetch_progress["error"] = error

    date_label = ""
    if date_from and date_to:
        date_label = f" ({date_from} to {date_to})"
    print(f"  {C_CYAN}⟳ Fetching data from ServiceNow{date_label}...{C_RESET}")

    _update("fetching", "Reading browser cookies...")

    try:
        import browser_cookie3
    except ImportError:
        pip_cmd = [sys.executable, "-m", "pip", "install",
                   "browser_cookie3", "requests", "pycryptodome"]
        subprocess.check_call(pip_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import browser_cookie3

    try:
        from Crypto.Cipher import AES  # noqa: F401
    except ImportError:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pycryptodome"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    import requests as req

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
            jar = browser_fn(domain_name=".service-now.com")
            found = [c for c in jar if "service-now" in c.domain]
            if found:
                cj = jar
                sn_cookies = found
                print(f"{C_GREEN}✓ {len(found)} cookies{C_RESET}")
                break
            else:
                msg = f"{browser_name}: opened but no ServiceNow cookies found"
                browser_errors.append(msg)
                print(f"{C_DIM}no ServiceNow cookies{C_RESET}")
        except PermissionError as e:
            msg = f"{browser_name}: permission denied — try closing {browser_name} completely and retry"
            browser_errors.append(msg)
            print(f"{C_DIM}permission denied{C_RESET}")
        except Exception as e:
            err_str = str(e)
            if "admin" in err_str.lower():
                msg = f"{browser_name}: requires admin — try Edge or Firefox instead"
            elif "Crypto" in err_str or "decrypt" in err_str.lower():
                msg = f"{browser_name}: decryption failed — run: pip install pycryptodome"
            else:
                msg = f"{browser_name}: {err_str[:100]}"
            browser_errors.append(msg)
            print(f"{C_DIM}skipped ({err_str[:80]}){C_RESET}")
            continue

    if not cj or not sn_cookies:
        details = "; ".join(browser_errors) if browser_errors else "unknown error"
        hint = ("Open ServiceNow in Chrome/Edge/Firefox first, "
                "then close the browser completely, then retry. "
                "Details: " + details)
        _update("error", error=hint)
        print(f"  {C_RED}✗ No cookies found{C_RESET}")
        for err in browser_errors:
            print(f"  {C_RED}  • {err}{C_RESET}")
        return

    print(f"  {C_DIM}  Found {len(sn_cookies)} session cookies{C_RESET}")

    headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}

    date_clause = ""
    if date_from:
        date_clause += f"^sys_created_on>={date_from}"
    if date_to:
        date_clause += f"^sys_created_on<={date_to} 23:59:59"

    all_records = []
    page_num = 0
    seen_ids = set()

    for table_name, table_cfg in SNOW_TABLES.items():
        table_url = table_cfg["url"]
        query = "^NQ".join(seg + date_clause for seg in table_cfg["segments"])
        print(f"  {C_CYAN}  Querying {table_name}...{C_RESET}")
        _update("fetching", f"Querying {table_name}...", pages=page_num, records=len(all_records))

        first_row = 0
        while True:
            page_num += 1
            params = {
                "JSONv2": "",
                "sysparm_query": query,
                "displayvalue": "true",
                "sysparm_record_count": str(PAGE_SIZE),
                "sysparm_first_row": str(first_row),
            }
            try:
                resp = req.get(table_url, params=params, cookies=cj,
                               headers=headers, timeout=60)
            except req.exceptions.ConnectionError:
                _update("error", error="Cannot reach ServiceNow. Check your network.")
                return
            except req.exceptions.Timeout:
                _update("error", error="Request timed out. ServiceNow may be slow.")
                return

            if resp.status_code == 401:
                _update("error", error="Session expired. Open ServiceNow in Chrome to refresh.")
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

            page = data.get("records", data.get("result", []))
            for rec in page:
                rid = rec.get("sys_id") or rec.get("number", "")
                if rid not in seen_ids:
                    seen_ids.add(rid)
                    rec["_source_table"] = table_name
                    all_records.append(rec)

            print(f"  {C_DIM}  {table_name} page {page_num}: {len(page)} records (total unique: {len(all_records)}){C_RESET}")
            _update("fetching", f"{table_name} — {len(all_records)} records so far...",
                    pages=page_num, records=len(all_records))

            if len(page) < PAGE_SIZE:
                break
            first_row += PAGE_SIZE

    DashboardHandler.cached_data = all_records
    save_cache(all_records)
    _update("done", f"Fetched {len(all_records)} tickets", pages=page_num, records=len(all_records))
    print(f"  {C_GREEN}✓ Fetched {len(all_records)} tickets from ServiceNow{C_RESET}")


def start_fetch(date_from=None, date_to=None):
    """Kick off a background fetch. Returns immediately."""
    with _fetch_lock:
        if fetch_progress["state"] == "fetching":
            return False
    fetch_progress["state"] = "fetching"
    fetch_progress["pages_done"] = 0
    fetch_progress["records_so_far"] = 0
    fetch_progress["error"] = None
    fetch_progress["message"] = "Starting..."
    t = threading.Thread(target=_do_fetch_background, args=(date_from, date_to), daemon=True)
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
        return qs.get("from", [None])[0], qs.get("to", [None])[0]

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
            date_from, date_to = self._parse_date_params()
            started = start_fetch(date_from, date_to)
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
    python_path = sys.executable

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

    elif os.name == "nt":
        bat_dir = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        bat_path = bat_dir / "snow_dashboard.bat"
        bat_content = f'@echo off\nstart /B "" "{python_path}" "{script_path}" --daemon\n'
        bat_path.write_text(bat_content)
        subprocess.Popen(
            [python_path, script_path, "--daemon"],
            creationflags=0x00000008,
            close_fds=True
        )
        print(f"\n  {C_GREEN}✓ Installed as startup service{C_RESET}")
        print(f"  {C_DIM}  The dashboard proxy will auto-start on login.{C_RESET}")
        print(f"  {C_DIM}  To uninstall: delete {bat_path}{C_RESET}\n")
    else:
        print(f"  {C_RED}Auto-start not supported on this OS.{C_RESET}")
        print(f"  {C_DIM}  Run '{python_path} {script_path} --daemon &' manually.{C_RESET}\n")


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        run_daemon()
    elif "--install" in sys.argv:
        install_autostart()
    else:
        main()
