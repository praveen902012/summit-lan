"""Welcome pop-up (captive portal) for lan-gateway on macOS.

How it works, together with the pf rules from lgw_mac.render_pf():
  * Phones that haven't tapped through yet: pf sends every plain-http request
    (port 80, any destination, including the phones' "internet check") to this
    server, and refuses https to the internet. Phones read the redirect as
    "this network needs a sign-in" and show the welcome page.
  * Tapping "Join the session" adds the phone's IP to the pf table
    <portal_ok>. From then on it gets the normal rules: allowed sites work,
    internet checks pass, and the phone treats the Wi-Fi as its connection.
Released phones are remembered in portal.json and expire after RELEASE_HOURS.
"""
import html
import ipaddress
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import lgw_mac as m

PORTAL_PORT = m.PORTAL_PORT
PORTAL_TABLE = m.PORTAL_TABLE
STATE_FILE = m.CONF_DIR + "/portal.json"
RELEASE_HOURS = 12

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>%(title)s</title>
<style>
:root { --bg:#EEF2F0; --sheet:#FFFFFF; --ink:#0E2430; --soft:#4B5F69; --accent:#0B6F6A; --on:#FFFFFF; --rule:#CBD6D2; }
@media (prefers-color-scheme: dark) { :root { --bg:#0D1A20; --sheet:#13242C; --ink:#E7F0EE; --soft:#A3B6B4; --accent:#47B8AE; --on:#062225; --rule:#29404A; color-scheme: dark; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:400 18px/1.5 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
  padding: max(24px, env(safe-area-inset-top)) max(20px, env(safe-area-inset-right)) max(24px, env(safe-area-inset-bottom)) max(20px, env(safe-area-inset-left)); }
main { max-width: 30rem; margin: 0 auto; display: grid; gap: 18px; }
h1 { margin: 8px 0 0; font-size: 1.9rem; line-height: 1.15; font-weight: 800; text-wrap: balance; }
p { margin: 0; }
.soft { color: var(--soft); }
form { margin: 6px 0 0; }
button { width: 100%%; border: 0; border-radius: 14px; padding: 20px; background: var(--accent); color: var(--on);
  font-family: inherit; font-weight: 800; font-size: 1.35rem; line-height: 1.2; cursor: pointer; }
button:focus-visible { outline: 3px solid var(--ink); outline-offset: 3px; }
.after { background: var(--sheet); border: 1px solid var(--rule); border-radius: 12px; padding: 14px 16px; font-size: 1rem; }
.addr { font: 600 1.05rem ui-monospace, "SF Mono", Menlo, monospace; overflow-wrap: anywhere; }
</style></head>
<body><main>
<h1>%(title)s</h1>
<p class="soft">You're on the Summit Wi-Fi. Tap below to open the live session.</p>
<form method="post" action="/go"><button type="submit">Join the session</button></form>
<div class="after">
  <p>If this window closes, open your browser and go to:</p>
  <p class="addr">%(next_url)s</p>
</div>
</main></body></html>
"""


class Portal:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.lock = threading.Lock()
        self.released = self._load()

    # -------------------------------------------------------------- state --
    def _load(self):
        try:
            data = json.load(open(STATE_FILE))
            return {ip: float(t) for ip, t in data.items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save(self):
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.released, f)
        os.replace(tmp, STATE_FILE)

    def next_url(self):
        host = str(self.cfg.lan_addr)
        return "http://%s%s" % (host, self.cfg.portal_next)

    def release(self, ip):
        with self.lock:
            new = ip not in self.released
            self.released[ip] = time.time()
            self._save()
        m.run([m.PFCTL, "-a", m.ANCHOR, "-t", PORTAL_TABLE, "-T", "add", ip], check=False)
        if new:
            self.log("PORTAL %s tapped through" % ip)

    def is_released(self, ip):
        with self.lock:
            t = self.released.get(ip)
        return t is not None and time.time() - t < RELEASE_HOURS * 3600

    def resync(self):
        """Put remembered phones back into the pf table (after the anchor was reloaded)."""
        self.housekeep()
        with self.lock:
            ips = list(self.released)
        if ips:
            m.run([m.PFCTL, "-a", m.ANCHOR, "-t", PORTAL_TABLE, "-T", "add"] + ips, check=False)

    def housekeep(self):
        cutoff = time.time() - RELEASE_HOURS * 3600
        with self.lock:
            old = [ip for ip, t in self.released.items() if t < cutoff]
            for ip in old:
                del self.released[ip]
            if old:
                self._save()
        if old:
            m.run([m.PFCTL, "-a", m.ANCHOR, "-t", PORTAL_TABLE, "-T", "delete"] + old, check=False)

    def count(self):
        with self.lock:
            return len(self.released)

    # ------------------------------------------------------------- server --
    def start(self):
        portal = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "summit-wifi"
            protocol_version = "HTTP/1.0"     # one request per connection, so pf re-routes after release

            def log_message(self, fmt, *args):
                pass

            def _send(self, code, body=b"", ctype="text/html; charset=utf-8", location=None):
                self.send_response(code)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _from_lan(self):
                try:
                    return ipaddress.IPv4Address(self.client_address[0]) in portal.cfg.lan_net
                except ValueError:
                    return False

            def _welcome_url(self):
                return "http://%s/welcome" % portal.cfg.lan_addr

            def do_GET(self):
                if not self._from_lan():
                    return self._send(403, b"forbidden", "text/plain")
                ip = self.client_address[0]
                path = urllib.parse.urlsplit(self.path).path
                host = (self.headers.get("Host") or "").split(":")[0].lower()
                ours = host in (str(portal.cfg.lan_addr), portal.cfg.app_name) if portal.cfg.app_name else host == str(portal.cfg.lan_addr)
                if portal.is_released(ip):
                    # A phone that already tapped through, still on an old connection or a
                    # cached route: send it on to where it was going (pf now routes it normally).
                    target = portal.next_url() if ours else "http://%s%s" % (self.headers.get("Host", ""), self.path)
                    return self._send(302, b"", location=target)
                if ours and path in ("/welcome", "/"):
                    body = PAGE % {"title": html.escape(portal.cfg.portal_title),
                                   "next_url": html.escape(portal.next_url())}
                    return self._send(200, body.encode())
                # Everything else (internet checks, other sites, other paths): to the welcome page.
                return self._send(302, b"", location=self._welcome_url())

            do_HEAD = do_GET

            def do_POST(self):
                if not self._from_lan():
                    return self._send(403, b"forbidden", "text/plain")
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(min(n, 4096))
                path = urllib.parse.urlsplit(self.path).path
                if path == "/go":
                    portal.release(self.client_address[0])
                    return self._send(303, b"", location=portal.next_url())
                return self._send(302, b"", location=self._welcome_url())

        while True:
            try:
                srv = ThreadingHTTPServer((str(self.cfg.lan_addr), PORTAL_PORT), Handler)
                break
            except OSError as exc:
                self.log("waiting to start the welcome page on %s:%d (%s)" % (self.cfg.lan_addr, PORTAL_PORT, exc))
                time.sleep(3)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.log("welcome pop-up on %s:%d (%d phones remembered)" % (self.cfg.lan_addr, PORTAL_PORT, self.count()))
        return srv
