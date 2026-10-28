#!/usr/bin/env bash
# End-to-end simulation: inet <-> laptop <-> client, using the real generated rules.
set -u
P="$(cd "$(dirname "$0")/.." && pwd)"
cd "$(mktemp -d)"
cleanup(){ for n in inet laptop client; do ip netns pids $n 2>/dev/null | xargs -r kill 2>/dev/null; ip netns del $n 2>/dev/null; done; }
cleanup
for n in inet laptop client; do ip netns add $n; ip -n $n link set lo up; done
ip link add wan0 netns laptop type veth peer name up0 netns inet
ip link add lan0 netns laptop type veth peer name eth0 netns client
ip -n inet addr add 10.99.0.2/24 dev up0; ip -n inet link set up0 up
# "Internet" hosts: allowed site, blocked site, fake public DNS
for a in 10.99.0.50 10.99.0.60 10.99.0.53; do ip -n inet addr add $a/32 dev up0; done
ip -n laptop addr add 10.99.0.1/24 dev wan0; ip -n laptop link set wan0 up
ip -n laptop route add default via 10.99.0.2
ip -n inet route add 192.168.50.0/24 via 10.99.0.1   # must NOT be used if NAT works; removed below
ip -n inet route del 192.168.50.0/24

# Fake upstream DNS in the internet namespace
ip netns exec inet dnsmasq --no-daemon --no-resolv --no-hosts --listen-address=10.99.0.53 --bind-interfaces \
  --address=/idlistack.com/10.99.0.50 --address=/blocked.example/10.99.0.60 --pid-file= >/dev/null 2>&1 &
# Web servers: allowed + blocked
for a in 10.99.0.50 10.99.0.60; do mkdir -p www$a; echo "site $a" > www$a/index.html
  ip netns exec inet python3 -m http.server 80 --bind $a --directory www$a >/dev/null 2>&1 & done

# Render config pointing at the fake internet
sed -e 's/^WAN_IF=.*/WAN_IF="wan0"/' -e 's/^LAN_IF=.*/LAN_IF="lan0"/' -e 's/^UPSTREAM_DNS=.*/UPSTREAM_DNS="10.99.0.53"/' $P/config.env > cfg.env
$P/lan-gateway render out cfg.env >/dev/null
# Same steps as 'lan-gateway apply'
ip -n laptop link set lan0 up; ip -n laptop addr add 192.168.50.1/24 dev lan0
ip netns exec laptop sysctl -q -w net.ipv4.ip_forward=1
ip netns exec laptop nft -f out/rules.nft || { echo NFT_FAIL; exit 1; }
ip netns exec laptop dnsmasq --no-daemon -C out/dnsmasq.conf --pid-file= --dhcp-leasefile=$PWD/leases >dns.log 2>&1 &
# Local app on the laptop, port 8688
mkdir -p app; echo "local app" > app/index.html
ip netns exec laptop python3 -m http.server 8688 --bind 0.0.0.0 --directory app >/dev/null 2>&1 &
sleep 2

# Client gets an address by DHCP-like static config
ip -n client link set eth0 up; ip -n client addr add 192.168.50.150/24 dev eth0; ip -n client route add default via 192.168.50.1

C="ip netns exec client"
t(){ local name="$1" expect="$2"; shift 2; if "$@" >/dev/null 2>&1; then r=PASS; else r=FAIL; fi
  if [ "$expect" = ok ]; then [ $r = PASS ] && echo "PASS  $name" || echo "FAIL  $name"; else [ $r = FAIL ] && echo "PASS  $name (blocked)" || echo "FAIL  $name (should be blocked)"; fi; }
t "client: allowed domain resolves"         ok   $C dig +short +time=2 +tries=1 idlistack.com @192.168.50.1 | grep -q 10.99.0.50
t "client: open allowed site"               ok   $C curl -sf -m3 --resolve idlistack.com:80:$($C dig +short idlistack.com @192.168.50.1) http://idlistack.com/
t "client: unlisted domain gets no answer"  no   sh -c "$C dig +short +time=2 +tries=1 blocked.example @192.168.50.1 | grep -q ."
t "client: blocked site by raw IP"          no   $C curl -sf -m3 http://10.99.0.60/
t "client: bypass DNS via outside server"   no   $C dig +short +time=2 +tries=1 blocked.example @10.99.0.53
t "client: local app via http://app.lan"    ok   sh -c "$C curl -sf -m3 --resolve app.lan:80:\$($C dig +short app.lan @192.168.50.1) http://app.lan/ | grep -q 'local app'"
t "client: local app on :8688"              ok   $C curl -sf -m3 http://192.168.50.1:8688/
t "client: laptop SSH port closed"          no   $C curl -s -m3 telnet://192.168.50.1:22
t "laptop: own access to blocked site"      ok   ip netns exec laptop curl -sf -m3 http://10.99.0.60/
t "laptop: own DNS to outside server"       ok   ip netns exec laptop dig +short +time=2 +tries=1 blocked.example @10.99.0.53
echo "--- firewall set filled by dnsmasq:"; ip netns exec laptop nft list set inet lan_gw allowed_dns4 | grep elements
cleanup
