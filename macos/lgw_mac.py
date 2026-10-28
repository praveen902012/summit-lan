#!/usr/bin/env python3
"""lan-gateway for macOS.

Turns a Mac into an allowlist gateway:
    Wi-Fi (internet) -> Mac (pf NAT + bootpd DHCP + DNS allowlist + pf firewall) -> Ethernet -> access point

Devices behind the access point can reach ONLY the domains/IPs in config.env plus
the local app. The Mac's own traffic is never filtered.

Pieces (all built into macOS except this file):
  pf       NAT and firewall, loaded into the anchor "com.apple/250.lan-gateway",
           which the stock /etc/pf.conf already evaluates (no edits to pf.conf)
  bootpd   DHCP for the LAN (/etc/bootpd.plist)
  this     DNS server on the LAN address: answers only allowed domains and adds
           each answer's IPs to the pf table <allowed_dns4> before replying
  launchd  keeps the DNS server and the local app running across reboots

Runs on the Python 3.8+ that the installer finds. Usage: see lan-gateway-mac.
"""
import ipaddress
import json
import os
import plistlib
import pwd
import re
import shlex
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time

NAME = "lan-gateway"
ANCHOR = "com.apple/100.lan-gateway"
LABEL = "in.t4gc.lan-gateway"
APP_LABEL = "in.t4gc.lan-gateway-app"
LIB_DIR = "/usr/local/lib/lan-gateway"
CONF_DIR = "/usr/local/etc/lan-gateway"
CONFIG = os.environ.get("LGW_CONFIG", CONF_DIR + "/config.env")
PF_FILE = CONF_DIR + "/pf.anchor"
STATE = CONF_DIR + "/state.json"
PLIST = "/Library/LaunchDaemons/%s.plist" % LABEL
APP_PLIST = "/Library/LaunchDaemons/%s.plist" % APP_LABEL
LOG = "/var/log/lan-gateway.log"
BOOTPD_PLIST = "/etc/bootpd.plist"
BOOTPS_SYSTEM_PLIST = "/System/Library/LaunchDaemons/bootps.plist"
BOOTPD_LABEL = "com.apple.bootpd"
DHCP_LEASES = "/var/db/dhcpd_leases"
DNS_TABLE = "allowed_dns4"
STATIC_TABLE = "allowed_static4"
HERE = os.path.dirname(os.path.abspath(__file__))

PFCTL = os.environ.get("LGW_PFCTL", "/sbin/pfctl")


class Fail(Exception):
    pass


def info(msg):
    print("==> " + msg, flush=True)


def warn(msg):
    print("WARNING: " + msg, file=sys.stderr, flush=True)


def run(cmd, check=True):
    """Run a command, return stdout+stderr text."""
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if check and p.returncode != 0:
        raise Fail("command failed: %s\n%s" % (" ".join(cmd), p.stdout.strip()))
    return p.stdout


def need_root():
    if os.geteuid() != 0:
        raise Fail("run this with sudo")


def need_mac():
    if sys.platform != "darwin":
        raise Fail("this is the macOS version; on Linux use ../lan-gateway")


# ------------------------------------------------------------------ config --

def parse_env(path):
    """Read the shell-style config.env (KEY="value", values may span lines)."""
    text = open(path).read()
    out = {}
    pat = re.compile(r'^([A-Z][A-Z0-9_]*)=(?:"((?:[^"\\]|\\.)*)"|\'([^\']*)\'|([^\s#]*))', re.M | re.S)
    for m in pat.finditer(text):
        val = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else m.group(4))
        out[m.group(1)] = val or ""
    return out


DOMAIN_RE = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class Config:
    def __init__(self, path):
        self.path = path
        e = parse_env(path)
        self.raw = e
        self.wan_if = e.get("WAN_IF", "").strip()
        self.lan_if = e.get("LAN_IF", "").strip()
        try:
            self.lan_addr = ipaddress.IPv4Address(e.get("LAN_ADDR", "192.168.50.1"))
            prefix = int(e.get("LAN_PREFIX", "24"))
            if not 8 <= prefix <= 30:
                raise ValueError
            self.lan_net = ipaddress.IPv4Network("%s/%d" % (self.lan_addr, prefix), strict=False)
            self.dhcp_start = ipaddress.IPv4Address(e.get("DHCP_START", ""))
            self.dhcp_end = ipaddress.IPv4Address(e.get("DHCP_END", ""))
        except ValueError:
            raise Fail("LAN_ADDR / LAN_PREFIX / DHCP_START / DHCP_END in %s are not valid" % path)
        for ip in (self.dhcp_start, self.dhcp_end):
            if ip not in self.lan_net:
                raise Fail("%s is outside %s" % (ip, self.lan_net))
        if self.dhcp_start > self.dhcp_end:
            raise Fail("DHCP_START is after DHCP_END")
        if self.dhcp_start <= self.lan_addr <= self.dhcp_end:
            raise Fail("LAN_ADDR must not be inside the DHCP range")
        self.lease_seconds = parse_duration(e.get("DHCP_LEASE", "12h"))

        self.domains = []
        for d in e.get("ALLOWED_DOMAINS", "").split():
            d = d.lower().rstrip(".")
            for pre in ("http://", "https://"):
                if d.startswith(pre):
                    d = d[len(pre):]
            d = d.split("/")[0]
            if not DOMAIN_RE.match(d):
                raise Fail("invalid domain in ALLOWED_DOMAINS: %r" % d)
            self.domains.append(d)
        if not self.domains:
            raise Fail("ALLOWED_DOMAINS is empty")

        self.static_ips = []
        for ip in e.get("ALLOWED_IPS", "").split():
            try:
                self.static_ips.append(str(ipaddress.IPv4Network(ip, strict=False)))
            except ValueError:
                raise Fail("invalid address in ALLOWED_IPS: %r" % ip)

        self.upstreams = []
        for ip in e.get("UPSTREAM_DNS", "").split():
            try:
                self.upstreams.append(str(ipaddress.IPv4Address(ip)))
            except ValueError:
                raise Fail("invalid UPSTREAM_DNS %r" % ip)
        if not self.upstreams:
            raise Fail("UPSTREAM_DNS is empty")

        self.tcp_ports = ports(e.get("ALLOWED_TCP_PORTS", ""))
        self.udp_ports = ports(e.get("ALLOWED_UDP_PORTS", ""))
        self.app_ports = ports(e.get("LOCAL_APP_PORTS", ""))
        r = e.get("LOCAL_APP_HTTP_REDIRECT", "").strip()
        self.app_redirect = ports(r)[0] if r else None
        if self.app_redirect == 80:
            self.app_redirect = None
        if self.app_redirect and self.app_redirect not in self.app_ports:
            self.app_ports.append(self.app_redirect)
        self.app_name = e.get("LOCAL_APP_NAME", "").strip().lower().rstrip(".")
        if self.app_name and not DOMAIN_RE.match(self.app_name):
            raise Fail("invalid LOCAL_APP_NAME")
        self.app_repo = e.get("LOCAL_APP_REPO", "").strip()
        self.app_branch = e.get("LOCAL_APP_BRANCH", "").strip()
        self.app_dir = e.get("LOCAL_APP_DIR", "").strip()
        self.app_command = e.get("LOCAL_APP_COMMAND_MAC", "").strip() or e.get("LOCAL_APP_COMMAND", "").strip()
        self.log_queries = e.get("LOG_DNS_QUERIES", "yes").strip() == "yes"

    def is_allowed(self, name):
        for d in self.domains:
            if name == d or name.endswith("." + d):
                return True
        return False


def ports(text):
    out = []
    for p in text.split():
        if not p.isdigit() or not 1 <= int(p) <= 65535:
            raise Fail("bad port %r" % p)
        out.append(int(p))
    return out


def parse_duration(s):
    m = re.match(r"^(\d+)([smhd]?)$", s.strip())
    if not m:
        raise Fail("bad DHCP_LEASE %r" % s)
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def set_env_value(path, key, value):
    text = open(path).read()
    new, n = re.subn(r'^%s=.*$' % key, '%s="%s"' % (key, value), text, count=1, flags=re.M)
    if n == 0:
        new = text.rstrip("\n") + '\n%s="%s"\n' % (key, value)
    with open(path, "w") as f:
        f.write(new)


# -------------------------------------------------------------- interfaces --

def hardware_ports():
    """[(port name, device)] from networksetup."""
    out = run(["networksetup", "-listallhardwareports"])
    found = []
    for block in out.split("\n\n"):
        m1 = re.search(r"Hardware Port: (.+)", block)
        m2 = re.search(r"Device: (\S+)", block)
        if m1 and m2:
            found.append((m1.group(1).strip(), m2.group(1).strip()))
    return found


def service_for_device(dev):
    """Network service name (as networksetup expects) for a device like en5."""
    out = run(["networksetup", "-listnetworkserviceorder"])
    for m in re.finditer(r"^\(\d+\)\s+(.+)\n\(Hardware Port: .*?, Device: (\S+)\)", out, re.M):
        if m.group(2) == dev:
            return m.group(1).lstrip("*").strip()
    return None


def is_real_ethernet(port, dev):
    """Exclude Wi-Fi, Bluetooth, iPhone USB, the Thunderbolt Bridge ports ("Thunderbolt 1"),
    and the "Ethernet Adapter (enN)" placeholders macOS creates on Apple-silicon Macs."""
    p = port.lower()
    if not dev.startswith("en"):
        return False
    if any(s in p for s in ("wi-fi", "bluetooth", "thunderbolt bridge", "iphone", "ipad")):
        return False
    if re.match(r"^thunderbolt \d+$", p) or re.match(r"^ethernet adapter \(en\d+\)$", p):
        return False
    return True


def detect_interfaces(cfg):
    hw = hardware_ports()
    if not cfg.wan_if:
        out = run(["route", "-n", "get", "default"], check=False)
        m = re.search(r"interface:\s*(\S+)", out)
        if not m:
            raise Fail("no default route. Connect to Wi-Fi first, or set WAN_IF in config.env")
        cfg.wan_if = m.group(1)
        if not any(d == cfg.wan_if and "wi-fi" in p.lower() for p, d in hw):
            warn("%s (default route) is not Wi-Fi; set WAN_IF if that is wrong" % cfg.wan_if)
    if not cfg.lan_if:
        cands = [(p, d) for p, d in hw if d != cfg.wan_if and is_real_ethernet(p, d)]
        active = [(p, d) for p, d in cands if "status: active" in run(["ifconfig", d], check=False)]
        if len(active) == 1:
            pick = active
        elif len(cands) == 1:
            pick = cands
        else:
            pick = []
        if not pick:
            if not cands:
                raise Fail("no Ethernet adapter found. Plug your USB-C/Thunderbolt Ethernet adapter into the Mac\n"
                           "(it shows up as e.g. 'USB 10/100/1000 LAN' in: networksetup -listallhardwareports),\n"
                           "connect its cable to a LAN port on the access point, and run this again.")
            names = ", ".join("%s (%s%s)" % (d, p, ", cable connected" if (p, d) in active else "")
                              for p, d in cands)
            raise Fail("more than one Ethernet adapter: %s.\n"
                       "Connect only the one going to the access point, or set LAN_IF in config.env" % names)
        cfg.lan_if = pick[0][1]
    devs = [d for _, d in hw]
    for dev in (cfg.wan_if, cfg.lan_if):
        if dev not in devs:
            raise Fail("interface %s not found (see: networksetup -listallhardwareports)" % dev)
    if cfg.wan_if == cfg.lan_if:
        raise Fail("WAN_IF and LAN_IF are the same interface")


def check_subnet_overlap(cfg):
    out = run(["ifconfig", cfg.wan_if], check=False)
    for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-f]+)", out):
        net = ipaddress.IPv4Network("%s/%s" % (m.group(1), str(ipaddress.IPv4Address(int(m.group(2), 16)))), strict=False)
        if net.overlaps(cfg.lan_net):
            raise Fail("LAN subnet %s overlaps the Wi-Fi network %s. Change LAN_ADDR/DHCP_* in config.env"
                       % (cfg.lan_net, net))


# ---------------------------------------------------------------- render --

def render_pf(cfg):
    def lst(items):
        return "{ " + ", ".join(str(i) for i in items) + " }"
    lines = [
        "# Generated by lan-gateway-mac from %s - edit config.env and run: sudo lan-gateway-mac reload" % cfg.path,
        "# Loaded into pf anchor %s. Only traffic arriving on %s (the access point side)" % (ANCHOR, cfg.lan_if),
        "# is filtered; the Mac's own traffic over %s is untouched." % cfg.wan_if,
        "",
        'lan_if = "%s"' % cfg.lan_if,
        'wan_if = "%s"' % cfg.wan_if,
        'lan_net = "%s"' % cfg.lan_net,
        'lan_addr = "%s"' % cfg.lan_addr,
        "",
        "# Filled at runtime by the lan-gateway DNS server with IPs of allowed domains",
        "table <%s> persist" % DNS_TABLE,
        "# Fixed IPs from ALLOWED_IPS",
        "table <%s> persist%s" % (STATIC_TABLE, (" " + lst(cfg.static_ips)) if cfg.static_ips else ""),
        "",
        "nat on $wan_if inet from $lan_net to ! $lan_net -> ($wan_if)",
    ]
    if cfg.app_redirect:
        lines.append("rdr on $lan_if inet proto tcp from $lan_net to $lan_addr port 80 -> $lan_addr port %d"
                     % cfg.app_redirect)
    lines += [
        "",
        "pass out quick on $lan_if all",
        "pass in quick on $lan_if inet proto udp from any port 68 to any port 67",
        "pass in quick on $lan_if inet proto { tcp, udp } from $lan_net to $lan_addr port 53",
        "pass in quick on $lan_if inet proto icmp from $lan_net to $lan_addr icmp-type echoreq",
    ]
    if cfg.app_ports:
        lines.append("pass in quick on $lan_if inet proto tcp from $lan_net to $lan_addr port %s"
                     % lst(cfg.app_ports))
    for proto, plist in (("tcp", cfg.tcp_ports), ("udp", cfg.udp_ports)):
        if plist:
            for table in (DNS_TABLE, STATIC_TABLE):
                lines.append("pass in quick on $lan_if inet proto %s from $lan_net to <%s> port %s"
                             % (proto, table, lst(plist)))
    lines += ["block drop in quick on $lan_if all", ""]
    return "\n".join(lines)


def render_bootpd(cfg):
    return {
        "bootp_enabled": False,
        "detect_other_dhcp_server": True,
        "dhcp_enabled": [cfg.lan_if],
        "reply_threshold_seconds": 0,
        "Subnets": [{
            "name": NAME,
            "net_address": str(cfg.lan_net.network_address),
            "net_mask": str(cfg.lan_net.netmask),
            "net_range": [str(cfg.dhcp_start), str(cfg.dhcp_end)],
            "allocate": True,
            "lease_max": cfg.lease_seconds,
            "lease_min": cfg.lease_seconds,
            "dhcp_router": str(cfg.lan_addr),
            "dhcp_domain_name_server": [str(cfg.lan_addr)],
        }],
    }


def render_daemon_plist(python):
    return {
        "Label": LABEL,
        "ProgramArguments": [python, LIB_DIR + "/lgw_mac.py", "daemon"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "StandardOutPath": LOG,
        "StandardErrorPath": LOG,
    }


def app_argv(cfg, python):
    argv = shlex.split(cfg.app_command)
    if argv and argv[0] in ("python3", "python"):
        argv[0] = python
    return argv


def render_app_plist(cfg, python, user, home, app_dir):
    return {
        "Label": APP_LABEL,
        "ProgramArguments": app_argv(cfg, python),
        "WorkingDirectory": app_dir,
        "UserName": user,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "EnvironmentVariables": {"HOME": home, "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": home + "/Library/Logs/lan-gateway-app.log",
        "StandardErrorPath": home + "/Library/Logs/lan-gateway-app.log",
    }


def render_all(cfg, out_dir, python="/usr/bin/python3"):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "pf.anchor"), "w") as f:
        f.write(render_pf(cfg))
    with open(os.path.join(out_dir, "bootpd.plist"), "wb") as f:
        plistlib.dump(render_bootpd(cfg), f)
    with open(os.path.join(out_dir, LABEL + ".plist"), "wb") as f:
        plistlib.dump(render_daemon_plist(python), f)
    if cfg.app_repo or cfg.app_dir:
        home = os.path.expanduser("~")
        app_dir = cfg.app_dir or os.path.join(home, repo_basename(cfg.app_repo))
        with open(os.path.join(out_dir, APP_LABEL + ".plist"), "wb") as f:
            plistlib.dump(render_app_plist(cfg, python, os.environ.get("USER", "you"), home, app_dir), f)


def repo_basename(url):
    base = url.rstrip("/").split("/")[-1]
    return base[:-4] if base.endswith(".git") else base


# --------------------------------------------------------------- state ----

def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(CONF_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE)


# ------------------------------------------------------------ pf / net ----

def pf_enabled():
    return "Status: Enabled" in run([PFCTL, "-s", "info"], check=False)


def pf_enable():
    """Enable pf with a reference token, remembered so uninstall can release it."""
    out = run([PFCTL, "-E"], check=False)
    m = re.search(r"Token\s*:\s*(\d+)", out)
    if m:
        st = load_state()
        st.setdefault("pf_tokens", []).append(m.group(1))
        save_state(st)


def ensure_main_ruleset():
    """The stock /etc/pf.conf evaluates anchors under com.apple/*; reload it if missing."""
    rules = run([PFCTL, "-s", "rules"], check=False)
    nat = run([PFCTL, "-s", "nat"], check=False)
    if 'anchor "com.apple/*"' not in rules or 'nat-anchor "com.apple/*"' not in nat:
        info("Loading the default macOS pf ruleset (/etc/pf.conf)")
        run([PFCTL, "-f", "/etc/pf.conf"], check=False)


def anchor_loaded():
    return bool(run([PFCTL, "-a", ANCHOR, "-s", "rules"], check=False).strip())


def load_anchor():
    run([PFCTL, "-a", ANCHOR, "-f", PF_FILE])


def ensure_lan_address(cfg):
    out = run(["ifconfig", cfg.lan_if], check=False)
    if "inet %s " % cfg.lan_addr not in out:
        run(["ifconfig", cfg.lan_if, "up"], check=False)
        run(["ifconfig", cfg.lan_if, "inet", str(cfg.lan_addr), "netmask", str(cfg.lan_net.netmask), "alias"],
            check=False)
        return True
    return False


def ensure_forwarding():
    if run(["sysctl", "-n", "net.inet.ip.forwarding"], check=False).strip() != "1":
        run(["sysctl", "-w", "net.inet.ip.forwarding=1"], check=False)


class PfTable:
    """Adds IPs to the pf table, skipping ones added in the last 10 minutes."""

    def __init__(self):
        self.lock = threading.Lock()
        self.added = {}

    def reset(self):
        with self.lock:
            self.added.clear()

    def add(self, ips):
        now = time.time()
        with self.lock:
            new = [ip for ip in ips if now - self.added.get(ip, 0) > 600]
            if not new:
                return
            run([PFCTL, "-a", ANCHOR, "-t", DNS_TABLE, "-T", "add"] + new, check=False)
            for ip in new:
                self.added[ip] = now


# ---------------------------------------------------------------- DNS ----

T_A, T_AAAA, T_SVCB, T_HTTPS, T_ANY = 1, 28, 64, 65, 255
NOERROR, SERVFAIL, NXDOMAIN, REFUSED = 0, 2, 3, 5


def parse_question(msg):
    if len(msg) < 17:
        raise ValueError("short")
    if struct.unpack(">H", msg[4:6])[0] != 1:
        raise ValueError("qdcount")
    i, labels = 12, []
    while True:
        n = msg[i]
        if n == 0:
            i += 1
            break
        if n & 0xC0:
            raise ValueError("compressed question")
        labels.append(msg[i + 1:i + 1 + n])
        i += 1 + n
        if i >= len(msg):
            raise ValueError("truncated")
    qtype, _qclass = struct.unpack(">HH", msg[i:i + 4])
    return b".".join(labels).decode("ascii", "replace").lower(), qtype, i + 4


def make_reply(query, qend, rcode, answers=b"", ancount=0):
    qflags = struct.unpack(">H", query[2:4])[0]
    flags = 0x8000 | (qflags & 0x7900) | 0x0080 | rcode
    return query[:2] + struct.pack(">HHHHH", flags, 1, ancount, 0, 0) + query[12:qend] + answers


def a_answer(ip, ttl=60):
    return struct.pack(">HHHIH", 0xC00C, T_A, 1, ttl, 4) + socket.inet_aton(ip)


def skip_name(msg, i):
    while True:
        n = msg[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += 1 + n


def a_records(msg):
    try:
        qd, an = struct.unpack(">HH", msg[4:8])
        i = 12
        for _ in range(qd):
            i = skip_name(msg, i) + 4
        ips = []
        for _ in range(an):
            i = skip_name(msg, i)
            rtype, _c, _ttl, rlen = struct.unpack(">HHIH", msg[i:i + 10])
            i += 10
            if rtype == T_A and rlen == 4:
                ips.append(socket.inet_ntoa(msg[i:i + 4]))
            i += rlen
        return ips
    except (IndexError, struct.error):
        return []


def query_udp(server, q, timeout=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.sendto(q, (server, 53))
        while True:
            data, _ = s.recvfrom(4096)
            if data[:2] == q[:2]:
                return data
    finally:
        s.close()


def query_tcp(server, q, timeout=3.0):
    s = socket.create_connection((server, 53), timeout=timeout)
    try:
        s.sendall(struct.pack(">H", len(q)) + q)
        n = struct.unpack(">H", recv_exact(s, 2))[0]
        return recv_exact(s, n)
    finally:
        s.close()


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise OSError("closed")
        buf += chunk
    return buf


class Resolver:
    def __init__(self, cfg, table, log):
        self.cfg, self.table, self.log = cfg, table, log

    def forward(self, q, tcp=False):
        for up in self.cfg.upstreams:
            try:
                r = query_tcp(up, q) if tcp else query_udp(up, q)
                if not tcp and len(r) > 3 and r[2] & 0x02:   # truncated: retry over TCP
                    r = query_tcp(up, q)
                return r
            except OSError:
                continue
        return None

    def answer(self, q, client, tcp=False):
        try:
            name, qtype, qend = parse_question(q)
        except (ValueError, IndexError, struct.error):
            return None
        cfg = self.cfg
        if cfg.app_name and (name == cfg.app_name or name.endswith("." + cfg.app_name)):
            if qtype in (T_A, T_ANY):
                return make_reply(q, qend, NOERROR, a_answer(str(cfg.lan_addr)), 1)
            return make_reply(q, qend, NOERROR)
        if name == "use-application-dns.net":          # tells Firefox to turn off DoH
            return make_reply(q, qend, NXDOMAIN)
        if not cfg.is_allowed(name):
            self.log("REFUSED %s %s" % (client, name))
            return make_reply(q, qend, REFUSED)
        if qtype in (T_AAAA, T_SVCB, T_HTTPS):
            # No IPv6 on the LAN; HTTPS records can carry address hints that skip A lookups.
            return make_reply(q, qend, NOERROR)
        r = self.forward(q, tcp)
        if r is None:
            return make_reply(q, qend, SERVFAIL)
        ips = a_records(r)
        if ips:
            self.table.add(ips)                          # before the client can connect
        if cfg.log_queries:
            self.log("ALLOW %s %s -> %s" % (client, name, " ".join(ips) or "-"))
        return r


class UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        r = self.server.resolver.answer(data, self.client_address[0])
        if r:
            if len(r) > 512 and not has_edns(data):
                r = truncate_reply(data, r)   # client retries over TCP
            sock.sendto(r, self.client_address)


def has_edns(q):
    return struct.unpack(">H", q[10:12])[0] > 0


def truncate_reply(q, r):
    try:
        _n, _t, qend = parse_question(q)
    except (ValueError, IndexError, struct.error):
        return r[:512]
    flags = struct.unpack(">H", r[2:4])[0] | 0x0200
    return r[:2] + struct.pack(">HHHHH", flags, 1, 0, 0, 0) + q[12:qend]


class TCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        s = self.request
        s.settimeout(10)
        try:
            while True:
                n = struct.unpack(">H", recv_exact(s, 2))[0]
                q = recv_exact(s, n)
                r = self.server.resolver.answer(q, self.client_address[0], tcp=True)
                if not r:
                    return
                s.sendall(struct.pack(">H", len(r)) + r)
        except (OSError, struct.error):
            return


class UDPServer(socketserver.ThreadingMixIn, socketserver.UDPServer):
    daemon_threads = True
    allow_reuse_address = True


class TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_logger():
    lock = threading.Lock()

    def log(msg):
        with lock:
            print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)
    return log


def start_dns(cfg, table, log, port=53):
    resolver = Resolver(cfg, table, log)
    servers = []
    for cls, handler in ((UDPServer, UDPHandler), (TCPServer, TCPHandler)):
        while True:
            try:
                srv = cls((str(cfg.lan_addr), port), handler)
                break
            except OSError as exc:
                log("waiting to bind DNS on %s:%d (%s)" % (cfg.lan_addr, port, exc))
                time.sleep(3)
        srv.resolver = resolver
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    log("DNS allowlist serving on %s:%d for %d domains" % (cfg.lan_addr, port, len(cfg.domains)))
    return servers


def cmd_daemon(args):
    """Run by launchd as root: keep the network pieces in place and serve DNS."""
    need_root()
    cfg = Config(CONFIG)
    log = make_logger()
    table = PfTable()
    net = "--no-network" not in args

    def ensure():
        if not net:
            return
        if ensure_lan_address(cfg):
            log("added %s/%d to %s" % (cfg.lan_addr, cfg.lan_net.prefixlen, cfg.lan_if))
        ensure_forwarding()
        ensure_main_ruleset()
        if not anchor_loaded():
            load_anchor()
            table.reset()
            log("loaded pf anchor %s" % ANCHOR)
        if not pf_enabled():
            pf_enable()
            log("enabled pf")

    if net:
        ensure_lan_address(cfg)
        ensure_forwarding()
        ensure_main_ruleset()
        load_anchor()                    # always fresh at start: empty table, current rules
        if not pf_enabled():
            pf_enable()
        log("network ready: %s -> %s (LAN %s via %s)" % (cfg.lan_if, cfg.wan_if, cfg.lan_net, cfg.lan_addr))
    port = int(os.environ.get("LGW_DNS_PORT", "53"))
    start_dns(cfg, table, log, port)
    while True:
        time.sleep(15)
        try:
            ensure()
        except Exception as exc:          # keep serving DNS whatever happens
            log("maintenance error: %s" % exc)


# ------------------------------------------------------------ launchd ----

def launchd_load(plist_path, label):
    run(["launchctl", "bootout", "system/" + label], check=False)
    time.sleep(0.5)
    out = run(["launchctl", "bootstrap", "system", plist_path], check=False)
    if "error" in out.lower() or "failed" in out.lower():
        run(["launchctl", "load", "-w", plist_path], check=False)
    run(["launchctl", "enable", "system/" + label], check=False)


def launchd_running(label):
    out = run(["launchctl", "print", "system/" + label], check=False)
    m = re.search(r"\bstate = (\w+)", out)
    return m.group(1) if m else "not loaded"


def write_plist(path, data):
    with open(path, "wb") as f:
        plistlib.dump(data, f)
    os.chmod(path, 0o644)


# -------------------------------------------------------------- install ----

def find_git():
    for g in ("/opt/homebrew/bin/git", "/usr/local/bin/git"):
        if os.path.exists(g):
            return g
    if run(["xcode-select", "-p"], check=False).strip().startswith("/") and os.path.exists("/usr/bin/git"):
        return "/usr/bin/git"
    raise Fail("git is not installed. Run: xcode-select --install  (or: brew install git)")


def as_user(user, cmd):
    return run(["sudo", "-u", user, "-H"] + cmd)


def lan_dns_query(cfg, name, qtype=T_A):
    qid = os.urandom(2)
    q = qid + struct.pack(">HHHHH", 0x0100, 1, 0, 0, 0)
    for label in name.split("."):
        q += bytes([len(label)]) + label.encode()
    q += b"\x00" + struct.pack(">HH", qtype, 1)
    r = query_udp(str(cfg.lan_addr), q, timeout=3)
    return r[3] & 0x0F, a_records(r)


def firewall_allow(path):
    fw = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    if os.path.exists(fw) and "enabled" in run([fw, "--getglobalstate"], check=False).lower():
        run([fw, "--add", path], check=False)
        run([fw, "--unblockapp", path], check=False)
        return True
    return False


def python_app_paths(python):
    """Binaries the macOS application firewall sees for this Python."""
    real = os.path.realpath(python)
    paths = [real]
    m = re.match(r"(.*/Python\.framework/Versions/[^/]+)/", real)
    if m:
        app = m.group(1) + "/Resources/Python.app/Contents/MacOS/Python"
        if os.path.exists(app):
            paths.append(app)
    return paths


def cmd_install(args):
    need_mac()
    need_root()
    src_config = os.path.join(os.path.dirname(HERE), "config.env")
    if not os.path.exists(src_config):
        raise Fail("config.env not found next to the macos folder")
    cfg = Config(src_config)
    detect_interfaces(cfg)
    check_subnet_overlap(cfg)
    if "bridge100" in run(["ifconfig", "-l"], check=False).split():
        raise Fail("Internet Sharing appears to be on (bridge100 exists). Turn it off in "
                   "System Settings > General > Sharing > Internet Sharing, then run this again")
    python = sys.executable
    service = service_for_device(cfg.lan_if)
    info("Wi-Fi (internet): %s    LAN (access point): %s%s    LAN: %s via %s"
         % (cfg.wan_if, cfg.lan_if, " [%s]" % service if service else "", cfg.lan_net, cfg.lan_addr))
    info("Python: %s" % python)

    os.makedirs(CONF_DIR, exist_ok=True)
    os.makedirs(LIB_DIR, exist_ok=True)
    shutil.copy2(src_config, CONFIG)
    set_env_value(CONFIG, "WAN_IF", cfg.wan_if)
    set_env_value(CONFIG, "LAN_IF", cfg.lan_if)
    cfg = Config(CONFIG)
    shutil.copy2(os.path.join(HERE, "lgw_mac.py"), LIB_DIR + "/lgw_mac.py")
    shutil.copy2(os.path.join(HERE, "lan-gateway-mac"), "/usr/local/bin/lan-gateway-mac")
    os.chmod("/usr/local/bin/lan-gateway-mac", 0o755)

    with open(PF_FILE, "w") as f:
        f.write(render_pf(cfg))
    run([PFCTL, "-n", "-a", ANCHOR, "-f", PF_FILE])  # syntax check only

    st = load_state()
    st.update({"python": python, "lan_if": cfg.lan_if, "lan_addr": str(cfg.lan_addr),
               "lan_mask": str(cfg.lan_net.netmask), "service": service})
    if "bootpd_backup" not in st:
        if os.path.exists(BOOTPD_PLIST):
            shutil.copy2(BOOTPD_PLIST, CONF_DIR + "/bootpd.plist.backup")
            st["bootpd_backup"] = CONF_DIR + "/bootpd.plist.backup"
        else:
            st["bootpd_backup"] = ""
    save_state(st)

    if service:
        info("Taking '%s' off DHCP so macOS leaves the LAN port to lan-gateway" % service)
        run(["networksetup", "-setv4off", service], check=False)
        run(["networksetup", "-setv6off", service], check=False)
    ensure_lan_address(cfg)

    info("Configuring DHCP (bootpd)")
    write_plist(BOOTPD_PLIST, render_bootpd(cfg))
    run(["launchctl", "enable", "system/" + BOOTPD_LABEL], check=False)
    out = run(["launchctl", "bootstrap", "system", BOOTPS_SYSTEM_PLIST], check=False)
    if "error" in out.lower() and "already" not in out.lower() and "5:" not in out:
        run(["launchctl", "load", "-w", BOOTPS_SYSTEM_PLIST], check=False)
    run(["killall", "bootpd"], check=False)          # on-demand; restarts with the new config

    info("Starting the firewall and DNS allowlist")
    write_plist(PLIST, render_daemon_plist(python))
    launchd_load(PLIST, LABEL)
    for p in python_app_paths(python):
        if firewall_allow(p):
            info("Allowed %s through the macOS application firewall" % p)
    ok = False
    for _ in range(20):
        try:
            rcode, ips = lan_dns_query(cfg, cfg.app_name or cfg.domains[0])
            if rcode == NOERROR:
                ok = True
                break
        except OSError:
            pass
        time.sleep(1)
    if ok:
        info("DNS allowlist is answering on %s" % cfg.lan_addr)
    else:
        warn("the DNS server is not answering yet; see: sudo lan-gateway-mac status  and  %s" % LOG)

    if cfg.app_repo:
        install_app(cfg, python)

    url = "http://" + (cfg.app_name or str(cfg.lan_addr))
    info("Done.")
    print("\nNext:")
    print("  1. Put the access point in Access Point / Bridge mode with its DHCP off,")
    print("     and plug the Mac's Ethernet into one of its LAN ports.")
    if cfg.app_repo:
        print("  2. The app is running as a service (sudo lan-gateway-mac app status).")
        print("     Host screen on this Mac: http://localhost:%s/" % (cfg.app_ports[0] if cfg.app_ports else 80))
    else:
        print("  2. Start your app listening on 0.0.0.0 or %s on port %s."
              % (cfg.lan_addr, " ".join(map(str, cfg.app_ports)) or "?"))
    print("  3. Connect a phone to the access point and open %s/audience and https://idlistack.com" % url)
    print("  4. Check with: sudo lan-gateway-mac status")


def install_app(cfg, python):
    user = os.environ.get("SUDO_USER", "")
    if not user or user == "root":
        raise Fail("run the installer with sudo from your normal account, so the app runs as that user")
    home = pwd.getpwnam(user).pw_dir
    app_dir = cfg.app_dir or os.path.join(home, repo_basename(cfg.app_repo))
    git = find_git()
    if os.path.isdir(os.path.join(app_dir, ".git")):
        origin = as_user(user, [git, "-C", app_dir, "remote", "get-url", "origin"]).strip()
        if origin.rstrip("/").lower().replace(".git", "") != cfg.app_repo.rstrip("/").lower().replace(".git", ""):
            raise Fail("%s already exists but is a different repository (%s)" % (app_dir, origin))
        info("Using existing app checkout in %s" % app_dir)
        if cfg.app_branch:
            cur = as_user(user, [git, "-C", app_dir, "rev-parse", "--abbrev-ref", "HEAD"]).strip()
            if cur != cfg.app_branch:
                warn("%s is on branch '%s', not '%s'. Leaving it as it is." % (app_dir, cur, cfg.app_branch))
    elif os.path.exists(app_dir):
        raise Fail("%s exists and is not a git checkout; set LOCAL_APP_DIR in config.env" % app_dir)
    else:
        info("Cloning %s (%s) into %s" % (cfg.app_repo, cfg.app_branch or "default branch", app_dir))
        cmd = [git, "clone"] + (["-b", cfg.app_branch] if cfg.app_branch else []) + [cfg.app_repo, app_dir]
        as_user(user, cmd)

    st = load_state()
    st.update({"app_user": user, "app_dir": app_dir})
    save_state(st)
    os.makedirs(home + "/Library/Logs", exist_ok=True)
    write_plist(APP_PLIST, render_app_plist(cfg, python, user, home, app_dir))
    launchd_load(APP_PLIST, APP_LABEL)
    port = cfg.app_ports[0] if cfg.app_ports else None
    if port:
        for _ in range(60):
            try:
                socket.create_connection((str(cfg.lan_addr), port), timeout=1).close()
                info("App is up on http://%s:%d/" % (cfg.lan_addr, port))
                return
            except OSError:
                time.sleep(1)
        warn("the app did not answer on port %d yet; see: sudo lan-gateway-mac app logs" % port)


def cmd_reload(args):
    need_mac()
    need_root()
    if not os.path.exists(CONFIG):
        raise Fail("not installed; run: sudo ./lan-gateway-mac install")
    cfg = Config(CONFIG)
    detect_interfaces(cfg)
    tmp = PF_FILE + ".new"
    with open(tmp, "w") as f:
        f.write(render_pf(cfg))
    try:
        run([PFCTL, "-n", "-a", ANCHOR, "-f", tmp])
    except Fail:
        os.remove(tmp)
        raise Fail("new firewall rules failed validation; nothing changed")
    os.replace(tmp, PF_FILE)
    write_plist(BOOTPD_PLIST, render_bootpd(cfg))
    run(["killall", "bootpd"], check=False)
    run(["launchctl", "kickstart", "-k", "system/" + LABEL], check=False)
    info("Reloaded.")


def cmd_status(args):
    need_mac()
    need_root()
    cfg = Config(CONFIG)
    st = load_state()
    print("Wi-Fi (internet): %s    LAN: %s (%s/%d)" % (cfg.wan_if, cfg.lan_if, cfg.lan_addr, cfg.lan_net.prefixlen))
    print("DNS/firewall service: %s   app service: %s   pf: %s   forwarding: %s" % (
        launchd_running(LABEL), launchd_running(APP_LABEL) if os.path.exists(APP_PLIST) else "not installed",
        "enabled" if pf_enabled() else "DISABLED",
        run(["sysctl", "-n", "net.inet.ip.forwarding"], check=False).strip()))
    link = run(["ifconfig", cfg.lan_if], check=False)
    print("%s: %s, %s" % (cfg.lan_if, "cable connected" if "status: active" in link else "NO CABLE / link down",
                          "address set" if "inet %s " % cfg.lan_addr in link else "address MISSING"))

    print("\n--- DHCP clients ---")
    try:
        text = open(DHCP_LEASES).read()
        for block in re.findall(r"\{(.*?)\}", text, re.S):
            ip = re.search(r"ip_address=(\S+)", block)
            nm = re.search(r"name=(.*)", block)
            if ip and ipaddress.IPv4Address(ip.group(1)) in cfg.lan_net:
                print("  %-16s %s" % (ip.group(1), nm.group(1).strip() if nm else ""))
    except OSError:
        print("  (none yet)")

    print("\n--- IPs currently allowed via DNS ---")
    ips = run([PFCTL, "-a", ANCHOR, "-t", DNS_TABLE, "-T", "show"], check=False).split()
    print("  " + (" ".join(i for i in ips if re.match(r"^\d", i)) or "(none yet)"))

    print("\n--- Firewall rules (packets matched) ---")
    out = run([PFCTL, "-a", ANCHOR, "-v", "-s", "rules"], check=False)
    rule = None
    for line in out.splitlines():
        if not line.startswith(" "):
            rule = line.strip()
        else:
            m = re.search(r"Packets: (\d+)", line)
            if m and rule:
                print("  %8s  %s" % (m.group(1), rule))
                rule = None

    print("\n--- Local app ---")
    for p in cfg.app_ports:
        lines = run(["lsof", "-nP", "-iTCP:%d" % p, "-sTCP:LISTEN"], check=False).splitlines()[1:]
        addrs = [l.split()[-2] for l in lines if len(l.split()) > 2]
        if any(a.startswith(("*:", "%s:" % cfg.lan_addr)) for a in addrs):
            print("  port %d: listening and reachable from the LAN" % p)
        elif addrs:
            print("  port %d: listening on localhost only. Bind the app to 0.0.0.0 or %s" % (p, cfg.lan_addr))
        else:
            print("  port %d: nothing is listening" % p)

    print("\n--- Recently refused DNS names (add any your sites need to ALLOWED_DOMAINS) ---")
    counts = {}
    try:
        with open(LOG, errors="replace") as f:
            for line in f.readlines()[-3000:]:
                m = re.search(r"REFUSED \S+ (\S+)", line)
                if m:
                    counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    except OSError:
        pass
    for name, n in sorted(counts.items(), key=lambda kv: -kv[1])[:15]:
        print("  %5d %s" % (n, name))
    if not counts:
        print("  (none)")
    if st.get("app_user"):
        print("\nApp log: ~%s/Library/Logs/lan-gateway-app.log" % st["app_user"])
    print("DNS log: %s" % LOG)


def cmd_check(args):
    need_mac()
    need_root()
    if not args:
        raise Fail("usage: lan-gateway-mac check DOMAIN")
    cfg = Config(CONFIG)
    name = args[0].lower()
    try:
        rcode, ips = lan_dns_query(cfg, name)
    except OSError as exc:
        raise Fail("the DNS server on %s did not answer (%s)" % (cfg.lan_addr, exc))
    if rcode != NOERROR or not ips:
        print("%s: BLOCKED (DNS %s)" % (name, {REFUSED: "refused", NXDOMAIN: "no such name"}.get(rcode, "no answer")))
        return 1
    ok = True
    for ip in ips:
        if name == cfg.app_name:
            print("%s -> %s: local app" % (name, ip))
            continue
        hit = any("1/1 addresses match" in run([PFCTL, "-a", ANCHOR, "-t", t, "-T", "test", ip], check=False)
                  for t in (DNS_TABLE, STATIC_TABLE))
        print("%s -> %s: %s" % (name, ip, "allowed" if hit else "resolves but IP is NOT in the firewall table"))
        ok = ok and hit
    return 0 if ok else 1


def cmd_app(args):
    need_mac()
    need_root()
    if not os.path.exists(APP_PLIST):
        raise Fail("no app service installed (LOCAL_APP_REPO is blank in config.env)")
    st = load_state()
    sub = args[0] if args else "status"
    log = os.path.join(pwd.getpwnam(st["app_user"]).pw_dir, "Library/Logs/lan-gateway-app.log")
    if sub == "status":
        print("app service: %s" % launchd_running(APP_LABEL))
        print(run(["launchctl", "print", "system/" + APP_LABEL], check=False)[:1500])
    elif sub == "start":
        launchd_load(APP_PLIST, APP_LABEL)
    elif sub == "stop":
        run(["launchctl", "bootout", "system/" + APP_LABEL], check=False)
        info("Stopped. It starts again at boot, or with: sudo lan-gateway-mac app start")
    elif sub == "restart":
        run(["launchctl", "kickstart", "-k", "system/" + APP_LABEL], check=False)
        info("Restarted. Note: restarting resets the live room.")
    elif sub == "update":
        info("Pulling latest code in %s" % st["app_dir"])
        print(as_user(st["app_user"], [find_git(), "-C", st["app_dir"], "pull", "--ff-only"]))
        run(["launchctl", "kickstart", "-k", "system/" + APP_LABEL], check=False)
        info("Updated and restarted. Note: restarting resets the live room.")
    elif sub == "logs":
        os.execvp("tail", ["tail", "-n", "50", "-f", log])
    else:
        raise Fail("usage: lan-gateway-mac app status|start|stop|restart|update|logs")
    return 0


def cmd_uninstall(args):
    need_mac()
    need_root()
    info("Removing lan-gateway")
    st = load_state()
    for label, plist in ((APP_LABEL, APP_PLIST), (LABEL, PLIST)):
        run(["launchctl", "bootout", "system/" + label], check=False)
        if os.path.exists(plist):
            os.remove(plist)
    run([PFCTL, "-a", ANCHOR, "-F", "all"], check=False)
    for tok in st.get("pf_tokens", []):
        run([PFCTL, "-X", tok], check=False)
    if st.get("lan_if") and st.get("lan_addr"):
        run(["ifconfig", st["lan_if"], "inet", st["lan_addr"], "-alias"], check=False)
    if st.get("service"):
        run(["networksetup", "-setdhcp", st["service"]], check=False)
        run(["networksetup", "-setv6automatic", st["service"]], check=False)
    backup = st.get("bootpd_backup")
    if backup and os.path.exists(backup):
        shutil.copy2(backup, BOOTPD_PLIST)
    elif os.path.exists(BOOTPD_PLIST):
        os.remove(BOOTPD_PLIST)
    run(["launchctl", "bootout", "system/" + BOOTPD_LABEL], check=False)
    run(["killall", "bootpd"], check=False)
    run(["sysctl", "-w", "net.inet.ip.forwarding=0"], check=False)
    for path in ("/usr/local/bin/lan-gateway-mac",):
        if os.path.exists(path):
            os.remove(path)
    shutil.rmtree(LIB_DIR, ignore_errors=True)
    shutil.rmtree(CONF_DIR, ignore_errors=True)
    info("Done. Normal networking restored.%s" % (
        " (The app's code folder %s was left in place.)" % st["app_dir"] if st.get("app_dir") else ""))


def cmd_render(args):
    if not args:
        raise Fail("usage: lan-gateway-mac render DIR [CONFIG]")
    cfg = Config(args[1] if len(args) > 1 else os.path.join(os.path.dirname(HERE), "config.env"))
    cfg.wan_if = cfg.wan_if or "en0"
    cfg.lan_if = cfg.lan_if or "en5"
    render_all(cfg, args[0], sys.executable)
    print("Wrote pf.anchor, bootpd.plist and launchd plists to %s (WAN_IF=%s LAN_IF=%s)"
          % (args[0], cfg.wan_if, cfg.lan_if))


COMMANDS = {
    "install": cmd_install, "reload": cmd_reload, "status": cmd_status, "check": cmd_check,
    "app": cmd_app, "uninstall": cmd_uninstall, "render": cmd_render, "daemon": cmd_daemon,
}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        return 1
    try:
        return COMMANDS[argv[0]](argv[1:]) or 0
    except Fail as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
