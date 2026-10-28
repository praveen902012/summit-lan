"""Settings page for lan-gateway on macOS.

Served by the root gateway service on 127.0.0.1 only (never on the LAN). Every API
call needs the token from /usr/local/etc/lan-gateway/ui-token, which
`lan-gateway-mac ui` passes to the browser in the URL fragment. Host and Origin
headers are checked too, so other websites open in the Mac's browser cannot use it.
"""
import grp
import hmac
import html.parser
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import lgw_mac as m

UI_PORT = int(os.environ.get("LGW_UI_PORT", "8699"))
TOKEN_FILE = m.CONF_DIR + "/ui-token"
NOTES_FILE = m.CONF_DIR + "/notes.json"
HTML_FILE = os.path.join(m.HERE, "ui.html")

# UI field -> config.env key
FIELDS = [
    ("wan_if", "WAN_IF"), ("lan_if", "LAN_IF"),
    ("lan_addr", "LAN_ADDR"), ("lan_prefix", "LAN_PREFIX"),
    ("dhcp_start", "DHCP_START"), ("dhcp_end", "DHCP_END"), ("dhcp_lease", "DHCP_LEASE"),
    ("upstream_dns", "UPSTREAM_DNS"),
    ("tcp_ports", "ALLOWED_TCP_PORTS"), ("udp_ports", "ALLOWED_UDP_PORTS"),
    ("app_name", "LOCAL_APP_NAME"), ("app_ports", "LOCAL_APP_PORTS"),
    ("app_redirect", "LOCAL_APP_HTTP_REDIRECT"),
    ("app_repo", "LOCAL_APP_REPO"), ("app_branch", "LOCAL_APP_BRANCH"), ("app_dir", "LOCAL_APP_DIR"),
    ("app_command", "LOCAL_APP_COMMAND"), ("app_command_mac", "LOCAL_APP_COMMAND_MAC"),
    ("log_queries", "LOG_DNS_QUERIES"),
]
SAFE_VALUE = re.compile(r"^[A-Za-z0-9 ._:/@+=,~-]*$")


# ----------------------------------------------------------------- token --

def ensure_token():
    try:
        tok = open(TOKEN_FILE).read().strip()
        if len(tok) >= 20:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    os.makedirs(m.CONF_DIR, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        f.write(tok + "\n")
    os.chmod(TOKEN_FILE, 0o640)
    try:   # readable by admin users, so `lan-gateway-mac ui` works without sudo
        os.chown(TOKEN_FILE, 0, grp.getgrnam("admin").gr_gid)
    except (KeyError, OSError):
        pass
    return tok


# ---------------------------------------------------------------- config --

def read_raw():
    return m.parse_env(m.CONFIG)


def load_notes():
    try:
        return json.load(open(NOTES_FILE))
    except (OSError, ValueError):
        return {}


def render_config_env(raw):
    """config.env in the same layout as the repo's, from a dict of KEY -> value."""
    def v(key):
        return raw.get(key, "")

    def block(key):
        items = [x for x in v(key).split() if x]
        return '%s="\n%s\n"' % (key, "\n".join(items)) if items else '%s=""' % key

    return """# lan-gateway configuration (written by the settings page, %s)
# Apply changes on an installed Mac with the settings page (lan-gateway-mac ui)
# or by editing this file and running:  sudo lan-gateway-mac reload
#
# Only devices on the LAN side (behind the access point) are restricted.
# The laptop itself keeps full, unfiltered internet access over Wi-Fi.

# ---- Interfaces ----
WAN_IF="%s"
LAN_IF="%s"

# ---- LAN addressing (must not overlap the Wi-Fi network's subnet) ----
LAN_ADDR="%s"
LAN_PREFIX="%s"
DHCP_START="%s"
DHCP_END="%s"
DHCP_LEASE="%s"

# ---- Upstream DNS the laptop uses to resolve the allowed domains ----
UPSTREAM_DNS="%s"

# ---- Allowed domains (one per line; every subdomain is included; no comments inside the quotes) ----
%s

# ---- IP addresses that are always allowed, even without a DNS lookup ----
%s

# ---- Ports LAN clients may use toward allowed destinations ----
ALLOWED_TCP_PORTS="%s"
ALLOWED_UDP_PORTS="%s"

# ---- Application running locally on this laptop ----
LOCAL_APP_NAME="%s"
LOCAL_APP_PORTS="%s"
LOCAL_APP_HTTP_REDIRECT="%s"
LOCAL_APP_REPO="%s"
LOCAL_APP_BRANCH="%s"
LOCAL_APP_DIR="%s"
LOCAL_APP_COMMAND="%s"
LOCAL_APP_COMMAND_MAC="%s"

# ---- Logging ----
LOG_DNS_QUERIES="%s"
""" % (time.strftime("%Y-%m-%d %H:%M"),
       v("WAN_IF"), v("LAN_IF"), v("LAN_ADDR"), v("LAN_PREFIX"), v("DHCP_START"), v("DHCP_END"),
       v("DHCP_LEASE"), v("UPSTREAM_DNS"), block("ALLOWED_DOMAINS"), block("ALLOWED_IPS"),
       v("ALLOWED_TCP_PORTS"), v("ALLOWED_UDP_PORTS"),
       v("LOCAL_APP_NAME"), v("LOCAL_APP_PORTS"), v("LOCAL_APP_HTTP_REDIRECT"), v("LOCAL_APP_REPO"),
       v("LOCAL_APP_BRANCH"), v("LOCAL_APP_DIR"), v("LOCAL_APP_COMMAND"), v("LOCAL_APP_COMMAND_MAC"),
       v("LOG_DNS_QUERIES") or "yes")


def to_host(text):
    """'https://Foo.example.org/path' -> 'foo.example.org'."""
    t = text.strip().lower()
    if "://" in t:
        t = urllib.parse.urlsplit(t).hostname or ""
    t = t.split("/")[0].split(":")[0].strip(".")
    if t.startswith("*."):
        t = t[2:]
    return t


def config_view():
    raw = read_raw()
    out = {k: raw.get(key, "") for k, key in FIELDS}
    out["domains"] = raw.get("ALLOWED_DOMAINS", "").split()
    out["ips"] = raw.get("ALLOWED_IPS", "").split()
    out["notes"] = load_notes()
    return out


def apply_config(payload, schedule_restart):
    """Validate the edited settings; unless dry_run, write them and restart the gateway."""
    old = read_raw()
    raw = dict(old)
    for field, key in FIELDS:
        if field in payload:
            val = str(payload[field]).strip()
            if not SAFE_VALUE.match(val):
                raise m.Fail("%s contains characters that aren't allowed: %r" % (key, val))
            raw[key] = val
    if "domains" in payload:
        doms, seen = [], set()
        for d in payload["domains"]:
            h = to_host(str(d))
            if not m.DOMAIN_RE.match(h):
                raise m.Fail("'%s' is not a valid domain name" % d)
            if h not in seen:
                seen.add(h)
                doms.append(h)
        raw["ALLOWED_DOMAINS"] = " ".join(doms)
    if "ips" in payload:
        ips = []
        for ip in payload["ips"]:
            try:
                ips.append(str(ipaddress.IPv4Network(str(ip).strip(), strict=False)).replace("/32", ""))
            except ValueError:
                raise m.Fail("'%s' is not a valid IPv4 address" % ip)
        raw["ALLOWED_IPS"] = " ".join(ips)

    fd, tmp = tempfile.mkstemp(prefix="config.", dir=m.CONF_DIR)
    os.close(fd)
    try:
        with open(tmp, "w") as f:
            f.write(render_config_env(raw))
        cfg = m.Config(tmp)                       # raises Fail with a readable message
        on_mac = sys.platform == "darwin"
        if on_mac:
            if not cfg.wan_if or not cfg.lan_if:
                m.detect_interfaces(cfg)
                raw["WAN_IF"], raw["LAN_IF"] = cfg.wan_if, cfg.lan_if
                with open(tmp, "w") as f:
                    f.write(render_config_env(raw))
                cfg = m.Config(tmp)
            m.detect_interfaces(cfg)              # validates the names exist
            m.check_subnet_overlap(cfg)
        pf_tmp = tmp + ".pf"
        with open(pf_tmp, "w") as f:
            f.write(m.render_pf(cfg))
        try:
            m.run([m.PFCTL, "-n", "-a", m.ANCHOR, "-f", pf_tmp])
        finally:
            os.remove(pf_tmp)

        changed = sorted(k for k in set(raw) | set(old) if raw.get(k, "") != old.get(k, ""))
        if payload.get("dry_run"):
            return {"ok": True, "changed": changed}

        if "notes" in payload and isinstance(payload["notes"], dict):
            keep = set(cfg.domains)
            notes = {to_host(k): str(v)[:200] for k, v in payload["notes"].items() if to_host(k) in keep and v}
            with open(NOTES_FILE, "w") as f:
                json.dump(notes, f, indent=2)

        if not changed:
            return {"ok": True, "changed": [], "restarting": False}
        old_lan = old.get("LAN_IF", "")
        if on_mac and old_lan and old_lan != cfg.lan_if:
            move_lan_port(old, cfg)
        os.replace(tmp, m.CONFIG)
        tmp = None
        with open(m.PF_FILE, "w") as f:
            f.write(m.render_pf(cfg))
        if on_mac:
            m.write_plist(m.BOOTPD_PLIST, m.render_bootpd(cfg))
        schedule_restart()
        return {"ok": True, "changed": changed, "restarting": True}
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


def move_lan_port(old, cfg):
    """The access point moved to another Ethernet port: give the old one back to macOS."""
    st = m.load_state()
    if old.get("LAN_ADDR"):
        m.run(["ifconfig", old["LAN_IF"], "inet", old["LAN_ADDR"], "-alias"], check=False)
    if st.get("service"):
        m.run(["networksetup", "-setdhcp", st["service"]], check=False)
        m.run(["networksetup", "-setv6automatic", st["service"]], check=False)
    service = m.service_for_device(cfg.lan_if)
    if service:
        m.run(["networksetup", "-setv4off", service], check=False)
        m.run(["networksetup", "-setv6off", service], check=False)
    st.update({"lan_if": cfg.lan_if, "lan_addr": str(cfg.lan_addr), "service": service})
    m.save_state(st)


# ---------------------------------------------------------------- status --

def dhcp_clients(cfg):
    out = []
    try:
        text = open(m.DHCP_LEASES).read()
    except OSError:
        return out
    for block in re.findall(r"\{(.*?)\}", text, re.S):
        ip = re.search(r"ip_address=(\S+)", block)
        nm = re.search(r"name=(.*)", block)
        lease = re.search(r"lease=(0x[0-9a-f]+)", block)
        if not ip:
            continue
        try:
            if ipaddress.IPv4Address(ip.group(1)) not in cfg.lan_net:
                continue
        except ValueError:
            continue
        expires = int(lease.group(1), 16) if lease else 0
        if expires and expires < time.time():
            continue
        out.append({"ip": ip.group(1), "name": nm.group(1).strip() if nm else ""})
    return out


def log_activity(cfg, max_lines=4000):
    """Refused names (not on the current list) and recent DNS lines from the gateway log."""
    refused = {}
    recent = []
    try:
        with open(m.LOG, errors="replace") as f:
            lines = f.readlines()[-max_lines:]
    except OSError:
        lines = []
    for line in lines:
        mm = re.match(r"^(\S+ \S+) REFUSED (\S+) (\S+)", line)
        if mm:
            when, client, name = mm.groups()
            if cfg.is_allowed(name):
                continue
            r = refused.setdefault(name, {"name": name, "count": 0, "last": "", "clients": set()})
            r["count"] += 1
            r["last"] = when
            r["clients"].add(client)
    for line in lines[-60:]:
        recent.append(line.rstrip("\n"))
    items = sorted(refused.values(), key=lambda r: (r["last"], r["count"]), reverse=True)[:40]
    for r in items:
        r["devices"] = len(r.pop("clients"))
    return items, recent


def app_port_state(cfg):
    if not cfg.app_ports:
        return "none"
    port = cfg.app_ports[0]
    import socket
    for host in (str(cfg.lan_addr), "127.0.0.1"):
        try:
            socket.create_connection((host, port), timeout=0.5).close()
            return "lan" if host != "127.0.0.1" else "localhost-only"
        except OSError:
            continue
    return "down"


def interfaces():
    if sys.platform != "darwin":
        return []
    out = []
    for port, dev in m.hardware_ports():
        if not dev.startswith("en"):
            continue
        wifi = "wi-fi" in port.lower()
        if not wifi and not m.is_real_ethernet(port, dev):
            continue
        status = m.run(["ifconfig", dev], check=False)
        out.append({"dev": dev, "port": port, "wifi": wifi, "active": "status: active" in status})
    return out


def state(started):
    cfg = m.Config(m.CONFIG)
    refused, recent = log_activity(cfg)
    on_mac = sys.platform == "darwin"
    link = m.run(["ifconfig", cfg.lan_if], check=False) if on_mac else ""
    table = m.run([m.PFCTL, "-a", m.ANCHOR, "-t", m.DNS_TABLE, "-T", "show"], check=False)
    st = m.load_state()
    return {
        "config": config_view(),
        "status": {
            "cable": ("status: active" in link) if on_mac else None,
            "address_set": ("inet %s " % cfg.lan_addr in link) if on_mac else None,
            "pf": m.pf_enabled() if on_mac else None,
            "allowed_ips": len([x for x in table.split() if re.match(r"^\d", x)]),
            "clients": dhcp_clients(cfg),
            "app": app_port_state(cfg),
            "app_service": m.launchd_running(m.APP_LABEL) if on_mac and os.path.exists(m.APP_PLIST) else "not installed",
            "dhcp_capacity": int(cfg.dhcp_end) - int(cfg.dhcp_start) + 1,
            "since": started,
            "wan_if": cfg.wan_if, "lan_if": cfg.lan_if, "lan_addr": str(cfg.lan_addr),
            "app_dir": st.get("app_dir", ""),
        },
        "interfaces": interfaces(),
        "refused": refused,
        "recent": recent,
    }


def check_domain(name):
    cfg = m.Config(m.CONFIG)
    name = to_host(name)
    if not m.DOMAIN_RE.match(name):
        raise m.Fail("'%s' is not a valid domain name" % name)
    if not cfg.is_allowed(name) and name != cfg.app_name:
        return {"name": name, "allowed": False, "message": "Not on the list, so phones get no answer for it."}
    try:
        rcode, ips = m.lan_dns_query(cfg, name)
    except OSError as exc:
        return {"name": name, "allowed": True, "message": "The gateway's DNS did not answer (%s)." % exc}
    if not ips:
        return {"name": name, "allowed": True,
                "message": "On the list, but it has no IPv4 address (DNS code %d)." % rcode}
    return {"name": name, "allowed": True, "ips": ips,
            "message": "Phones can reach it at %s." % ", ".join(ips)}


# ------------------------------------------------------------------ scan --

RESOURCE_RELS = {"stylesheet", "preload", "modulepreload", "icon", "shortcut", "apple-touch-icon",
                 "manifest", "preconnect", "dns-prefetch", "prefetch"}
NOISE_HOSTS = {"www.w3.org", "w3.org", "schema.org", "purl.org", "ogp.me", "json-schema.org",
               "xmlns.com", "example.com", "www.example.com", "localhost"}


class PageParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.resources = []    # (url, why)
        self.links = []
        self.scripts = []      # same-site script/style URLs to read further
        self.inline = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script":
            self._in_script = True
            if a.get("src"):
                self.resources.append((a["src"], "script"))
                self.scripts.append(a["src"])
        elif tag == "link" and a.get("href"):
            rels = set(a.get("rel", "").lower().split())
            if rels & RESOURCE_RELS:
                why = "stylesheet" if "stylesheet" in rels else ("font or file" if rels & {"preload", "prefetch"} else
                                                               "connection hint" if rels & {"preconnect", "dns-prefetch"} else "icon")
                self.resources.append((a["href"], why))
                if "stylesheet" in rels:
                    self.scripts.append(a["href"])
        elif tag in ("img", "source", "video", "audio", "iframe", "embed", "track"):
            for key in ("src", "poster", "data-src"):
                if a.get(key):
                    self.resources.append((a[key], "frame" if tag == "iframe" else "image or media"))
            for key in ("srcset", "data-srcset"):
                for part in a.get(key, "").split(","):
                    url = part.strip().split(" ")[0]
                    if url:
                        self.resources.append((url, "image or media"))
        elif tag == "object" and a.get("data"):
            self.resources.append((a["data"], "embedded file"))
        elif tag == "form" and a.get("action"):
            self.resources.append((a["action"], "form submits here"))
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if a.get("style"):
            self.inline.append(a["style"])

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script or "url(" in data:
            self.inline.append(data)


URL_IN_TEXT = re.compile(r"""https?://([a-zA-Z0-9.-]+\.[a-zA-Z]{2,63})(?=[/:'"`\s)?\\]|$)""")
CSS_URL = re.compile(r"""(?:url\(\s*['"]?|@import\s+['"])(https?://[^'")\s]+)""")


def is_ip(host):
    try:
        ipaddress.IPv4Address(host)
        return True
    except ValueError:
        return False


def fetch(url, max_bytes=3000000):
    """Download with the Mac's own curl (system certificates); returns (final_url, body)."""
    fd, path = tempfile.mkstemp(prefix="scan.")
    os.close(fd)
    try:
        p = subprocess.run(
            ["curl", "-sSL", "--compressed", "--max-time", "15", "--max-filesize", str(max_bytes),
             "--proto", "=http,https", "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
             "-o", path, "-w", "%{url_effective}", url],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if p.returncode != 0:
            raise m.Fail("could not download %s: %s" % (url, p.stderr.strip() or "curl exit %d" % p.returncode))
        with open(path, "rb") as f:
            return p.stdout.strip() or url, f.read(max_bytes).decode("utf-8", "replace")
    finally:
        os.remove(path)


def scan(url):
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if not urllib.parse.urlsplit(url).hostname:
        raise m.Fail("enter a web address, like https://example.org")
    final, body = fetch(url)
    page_host = (urllib.parse.urlsplit(final).hostname or "").lower()
    first_host = (urllib.parse.urlsplit(url).hostname or "").lower()
    p = PageParser()
    try:
        p.feed(body)
    except Exception:
        pass

    found = {}   # host -> {"why": set, "kind": "load" | "script"}

    def note(host, why, kind):
        host = (host or "").lower().strip(".")
        if not host or host in NOISE_HOSTS or not (m.DOMAIN_RE.match(host) or is_ip(host)):
            return
        e = found.setdefault(host, {"why": set(), "kind": kind})
        e["why"].add(why)
        if kind == "load":
            e["kind"] = "load"

    for raw_url, why in p.resources:
        abs_url = urllib.parse.urljoin(final, raw_url.strip())
        note(urllib.parse.urlsplit(abs_url).hostname, why, "load")
    for text in p.inline:
        for u in CSS_URL.findall(text):
            note(urllib.parse.urlsplit(u).hostname, "image or font in a style", "load")
        for h in URL_IN_TEXT.findall(text):
            note(h, "mentioned in the page's scripts", "script")

    # Read the site's own scripts and stylesheets for more hosts.
    read = 0
    for raw_url in p.scripts:
        abs_url = urllib.parse.urljoin(final, raw_url.strip())
        h = (urllib.parse.urlsplit(abs_url).hostname or "").lower()
        if read >= 12 or h not in (page_host, first_host):
            continue
        try:
            _, text = fetch(abs_url, 2000000)
        except m.Fail:
            continue
        read += 1
        for u in CSS_URL.findall(text):
            note(urllib.parse.urlsplit(u).hostname, "image or font in a stylesheet", "load")
        for hh in URL_IN_TEXT.findall(text):
            note(hh, "mentioned in the site's scripts", "script")

    cfg = m.Config(m.CONFIG)

    def allowed(host):
        if is_ip(host):
            ip = ipaddress.IPv4Address(host)
            return any(ip in ipaddress.IPv4Network(n) for n in cfg.static_ips)
        return cfg.is_allowed(host)

    resources = []
    for host in sorted(found, key=lambda h: (found[h]["kind"] != "load", h)):
        if host in (first_host, page_host):
            continue
        e = found[host]
        resources.append({"host": host, "why": sorted(e["why"]), "kind": e["kind"],
                          "allowed": allowed(host), "ip": is_ip(host)})
    links = sorted({(urllib.parse.urlsplit(urllib.parse.urljoin(final, l)).hostname or "").lower()
                    for l in p.links} - {"", page_host, first_host} - set(found))
    links = [{"host": h, "allowed": cfg.is_allowed(h), "ip": False} for h in links if m.DOMAIN_RE.match(h)]
    pages = [{"host": h, "allowed": allowed(h), "ip": is_ip(h)} for h in dict.fromkeys([first_host, page_host])
             if h and (m.DOMAIN_RE.match(h) or is_ip(h))]
    return {"url": final, "pages": pages, "resources": resources, "links": links,
            "scripts_read": read}


# ------------------------------------------------------------------ app ----

def app_action(action):
    if sys.platform != "darwin" or not os.path.exists(m.APP_PLIST):
        raise m.Fail("the Summit app service is not installed")
    if action == "restart":
        m.run(["launchctl", "kickstart", "-k", "system/" + m.APP_LABEL], check=False)
    elif action == "stop":
        m.run(["launchctl", "bootout", "system/" + m.APP_LABEL], check=False)
    elif action == "start":
        m.launchd_load(m.APP_PLIST, m.APP_LABEL)
    else:
        raise m.Fail("unknown action")
    return {"ok": True}


def app_log():
    st = m.load_state()
    if not st.get("app_user"):
        return []
    import pwd
    path = os.path.join(pwd.getpwnam(st["app_user"]).pw_dir, "Library/Logs/lan-gateway-app.log")
    try:
        with open(path, errors="replace") as f:
            return [l.rstrip("\n") for l in f.readlines()[-80:]]
    except OSError:
        return []


# ---------------------------------------------------------------- server --

class Handler(BaseHTTPRequestHandler):
    server_version = "lan-gateway-ui"
    token = ""
    started = ""
    restart = None

    def log_message(self, fmt, *args):
        pass

    def _allowed_origin(self):
        okhosts = {"127.0.0.1:%d" % UI_PORT, "localhost:%d" % UI_PORT}
        if self.headers.get("Host", "") not in okhosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {"http://" + h for h in okhosts}

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                         "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; "
                         "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        got = self.headers.get("X-LGW-Token", "")
        return bool(got) and hmac.compare_digest(got, self.token)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1000000:
            raise m.Fail("request too large")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise m.Fail("bad JSON")

    def do_GET(self):
        if not self._allowed_origin():
            return self._send(403, {"error": "forbidden"})
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            try:
                return self._send(200, open(HTML_FILE, "rb").read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, {"error": "ui.html is missing"})
        if not self._authorized():
            return self._send(401, {"error": "Open this page with: lan-gateway-mac ui"})
        try:
            if path == "/api/state":
                return self._send(200, state(self.started))
            if path == "/api/app-log":
                return self._send(200, {"lines": app_log()})
            if path == "/api/config.env":
                return self._send(200, open(m.CONFIG, "rb").read(), "text/plain; charset=utf-8")
        except m.Fail as exc:
            return self._send(400, {"error": str(exc)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed_origin():
            return self._send(403, {"error": "forbidden"})
        if not self._authorized():
            return self._send(401, {"error": "Open this page with: lan-gateway-mac ui"})
        path = urllib.parse.urlsplit(self.path).path
        try:
            body = self._body()
            if path == "/api/apply":
                return self._send(200, apply_config(body, self.restart))
            if path == "/api/scan":
                return self._send(200, scan(str(body.get("url", ""))))
            if path == "/api/check":
                return self._send(200, check_domain(str(body.get("name", ""))))
            if path == "/api/app":
                return self._send(200, app_action(str(body.get("action", ""))))
        except m.Fail as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:          # keep the page usable; report what broke
            return self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
        self._send(404, {"error": "not found"})


def restart_gateway():
    def later():
        time.sleep(0.8)
        if sys.platform == "darwin":
            m.run(["killall", "bootpd"], check=False)
            m.run(["launchctl", "kickstart", "-k", "system/" + m.LABEL], check=False)
        else:   # test environments: exit and let the supervisor restart us
            os._exit(0)
    threading.Thread(target=later, daemon=True).start()


def start(log):
    Handler.token = ensure_token()
    Handler.started = time.strftime("%Y-%m-%d %H:%M:%S")
    Handler.restart = staticmethod(restart_gateway)
    while True:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", UI_PORT), Handler)
            break
        except OSError as exc:
            log("waiting to start the settings page on 127.0.0.1:%d (%s)" % (UI_PORT, exc))
            time.sleep(3)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("settings page on http://127.0.0.1:%d/ (open it with: lan-gateway-mac ui)" % UI_PORT)
    return srv
