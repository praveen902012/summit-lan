# summit-lan

Laptop-as-gateway for the gamesworkshop Summit: share Wi-Fi internet to an access point, allow only a short list of sites, and serve the Summit app to the room. Scripts: `lan-gateway` (Linux) and `macos/lan-gateway-mac` (macOS).

Turns a laptop (**Ubuntu/Linux** or **macOS**) into a filtered gateway for an access point:

```
Internet ── Wi-Fi ── [ laptop: NAT + DHCP + DNS allowlist + firewall ] ── LAN cable ── Access point ── devices
```

- **The laptop keeps full internet.** Only traffic arriving on the LAN cable is filtered.
- **Devices on the access point** can reach only:
  - the domains in `config.env` (subdomains included): `idlistack.com`, `rforum.t4gc.in`, `netlify.app`, `netlify.com`, and the Ubuntu / Android / Apple / Windows connectivity checks, so phones and laptops show "Connected"
  - the fixed IPs you listed (91.189.91.61, 142.251.150.120, 17.253.18.201, 23.212.0.48)
  - the **gamesworkshop Summit** app (`pixel` branch) running on the laptop, at `http://app.lan` (audience: `http://app.lan/audience`)
- Everything else is blocked, including other DNS servers, DNS-over-HTTPS, and visiting sites by raw IP.

## Capacity (200 devices)

- **Addresses:** the LAN is `192.168.50.0/23`, which gives 497 DHCP addresses with 2-hour leases. That leaves room for phones that reconnect with a new random MAC address.
- **Access points are the real limit.** One consumer router in AP mode handles about 30–60 active phones. For 200, use **4 access points** (about 50 each), or 2–3 business-grade ones:
  - Connect them all to a **switch**, and the switch to the laptop's Ethernet.
  - Put every AP in **Access Point / Bridge mode** with **DHCP off**, the **same SSID and password**, and **different channels** (5 GHz: 36, 52, 100, 149; 2.4 GHz: 1, 6, 11).
- **The laptop is fine.** The firewall, DNS allowlist and Summit app (one live connection per phone) easily handle 200 phones. Blocked traffic doesn't use firewall memory; only allowed connections do.
- **Internet:** all phones share the laptop's Wi-Fi uplink, but only the allowed sites use it. Summit itself is local. Keep the laptop near its Wi-Fi router, on a channel different from the APs.

## How it works

1. **dnsmasq** hands out addresses (DHCP) and answers DNS only for allowed domains. Every other name gets `REFUSED`.
2. Each time an allowed domain resolves, dnsmasq adds its IPs to an nftables set (`inet lan_gw allowed_dns4`).
3. **nftables** forwards LAN traffic only to IPs in that set (TCP 80/443, UDP 443), then NATs it out over Wi-Fi.

## Requirements

- Ubuntu 24.04 (or Debian 12 / Fedora 37+). Needs dnsmasq 2.87 or newer; the installer checks.
- Laptop connected to Wi-Fi, with an Ethernet port (a USB adapter is fine).
- `ufw` and `firewalld` turned off. The installer stops and tells you if they're on.

## Install

```bash
cd lan-gateway
sudo ./lan-gateway install   # run with sudo from your normal account
```

The installer also clones `ciaokitty/gamesworkshop` (`pixel` branch) into `~/gamesworkshop` and runs it as a service. If the folder already exists, it uses that checkout.

The Wi-Fi and Ethernet interfaces are detected automatically. If there's more than one wired interface, set `WAN_IF` / `LAN_IF` in `config.env`; `ip -br link` lists the names.

Then set up the access point:

1. Switch it to **Access Point / Bridge mode** and **turn off its DHCP server**.
2. Plug the laptop's cable into one of its **LAN** ports (not WAN).

## The local app (gamesworkshop Summit)

- Runs as `lan-gateway-app.service` with `python3 scripts/run_summit.py --port 8688`, as your user, from `~/gamesworkshop`. It starts at boot.
- **Host screen** (on the laptop): `http://localhost:8688/`
- **Audience** (phones on the access point): `http://app.lan/audience`, or `http://192.168.50.1:8688/audience`
- The app needs nothing from the internet (no CDNs or external fonts), so it works fully behind the allowlist. The live room's event stream was tested through the firewall.
- Restarting the app resets the live room.

| Command | What it does |
|---|---|
| `sudo lan-gateway app status` | Is it running? |
| `sudo lan-gateway app restart` | Restart (resets the room) |
| `sudo lan-gateway app update` | `git pull` the latest `pixel` commits and restart |
| `sudo lan-gateway app logs` | Follow the app's output |
| `sudo lan-gateway app stop` | Stop it, e.g. to run `./run-summit.sh` by hand |

To use a different app, change `LOCAL_APP_*` in `config.env`. To run the app yourself, leave `LOCAL_APP_REPO` blank and make sure it listens on `0.0.0.0`, not `127.0.0.1`.

## Everyday commands

| Command | What it does |
|---|---|
| `sudo lan-gateway status` | Interfaces, connected devices, allowed IPs, drop counters, refused DNS names |
| `sudo lan-gateway check example.com` | Is this domain allowed? |
| `sudo nano /etc/lan-gateway/config.env` then `sudo lan-gateway reload` | Add or remove domains. The new config is validated first, and nothing changes if it's invalid |
| `sudo lan-gateway uninstall` | Remove everything and restore normal networking |
| `./lan-gateway render ./out` | Write the generated configs to `./out` to review them (no root needed) |

Everything starts automatically at boot: `lan-gateway.service`, then `dnsmasq` and the app.

`tests/simulate.sh` (run as root on a spare Linux box) builds a fake internet, laptop and phone with network namespaces and checks every rule.

## When a site loads only partly

Most sites pull fonts, scripts or images from other domains (for example `fonts.googleapis.com` or a CDN). To find them:

```bash
sudo lan-gateway status        # see "Recently refused DNS names"
journalctl -u dnsmasq -f       # live view while a device loads the page
```

Add the names you need to `ALLOWED_DOMAINS` and run `sudo lan-gateway reload`.


## macOS

The `macos/` folder is a separate version for Macs. It reads the same `config.env`, so it gives devices the same result; the Linux files are unchanged.

| | Linux | macOS |
|---|---|---|
| Firewall + NAT | nftables | pf (anchor `com.apple/100.lan-gateway`; `/etc/pf.conf` is not edited) |
| DHCP | dnsmasq | bootpd (built into macOS) |
| DNS allowlist | dnsmasq + nftset | `lgw_mac.py`, a small DNS server that adds allowed IPs to the pf table before replying |
| Services | systemd | launchd (`in.t4gc.lan-gateway`, `in.t4gc.lan-gateway-app`) |

### Install on a Mac

Requirements: an Ethernet port or USB/Thunderbolt Ethernet adapter, Wi-Fi connected, **Internet Sharing turned off**, and Python 3.8+. Homebrew Python, or the Xcode Command Line Tools (`xcode-select --install`, which also provides git), are both fine.

```bash
cd lan-gateway
sudo ./macos/lan-gateway-mac install    # run with sudo from your normal account
```

This also clones the Summit app into `~/gamesworkshop` and runs it with `LOCAL_APP_COMMAND_MAC` (`paperweights/room/server.py` directly, because `run_summit.py` reads `/proc`, which only exists on Linux). Keep the app folder outside Desktop/Documents/Downloads, since macOS privacy controls block background services there.

The installer turns IPv4 and IPv6 off for the Ethernet service in System Settings, so macOS doesn't fight over that port. Uninstall puts it back to DHCP.

### Settings page on a Mac

```bash
lan-gateway-mac ui        # opens the settings page in your browser (use sudo if it says permission denied)
```

On the settings page you can:
- **Change the allowed sites:** add or remove sites, with an optional note, and check that a site resolves.
- **Scan a page:** enter a web address, and the page lists the other sites it loads (fonts, images, scripts, form targets). Tick the ones you want and add them in one go.
- **Fix broken sites from the blocked list:** "Recently blocked" lists names phones asked for that aren't allowed. Allow the exact name, or the whole parent domain, with one click.
- **Change settings:** how many devices (100 / 250 / 1,000 or custom), lease length, which Wi-Fi and Ethernet ports to use, DNS servers, web ports, and the Summit app's address and port.
- **Run the Summit app:** restart it and view its log.
- **Back up:** download `config.env`.

Changes collect in a bar at the bottom of the page, shown as + added and − removed. Nothing takes effect until you click **Apply changes**. The gateway then checks the new settings and restarts in a couple of seconds; phones stay connected.

The page only listens on this Mac (`127.0.0.1:8699`), never on the access point side. It needs the key stored in `/usr/local/etc/lan-gateway/ui-token`, which `lan-gateway-mac ui` passes to the browser, so other websites open in your browser can't change your settings.

Running `install` again **keeps the settings you made on this page**. To replace them with the repo's `config.env`, run `sudo ./macos/lan-gateway-mac install --reset-config`.

### Commands on a Mac

Same as Linux, with `lan-gateway-mac`:

```bash
lan-gateway-mac ui                   # settings page
sudo lan-gateway-mac status          # devices, allowed IPs, rule counters, refused names
sudo lan-gateway-mac check example.com
sudo nano /usr/local/etc/lan-gateway/config.env && sudo lan-gateway-mac reload
sudo lan-gateway-mac app status|restart|update|logs
sudo lan-gateway-mac uninstall
./macos/lan-gateway-mac render ./out  # review the generated pf / bootpd / launchd files
```

Logs: `/var/log/lan-gateway.log` (DNS, and the Mac's network upkeep) and `~/Library/Logs/lan-gateway-app.log` (Summit).

Mac notes:
- A background loop re-applies the LAN address, IP forwarding and pf every 15 seconds, so unplugging the adapter or waking from sleep recovers by itself.
- If the macOS application firewall is on, the installer allows the Python it uses, so phones can reach DNS and the app.
- When the Mac sleeps, the access point loses internet and the app. Keep it plugged in and stop it sleeping (e.g. `caffeinate -s` in a Terminal) during sessions.

## Notes and limits (Linux)

- IPv6 is not routed to the LAN, so devices use IPv4 only. dnsmasq filters AAAA records so they don't wait on IPv6.
- `netlify.app` covers every site hosted there, not just yours. To allow only your own site, replace it with `yoursite.netlify.app`.
- Allowing a domain allows its IP. On shared CDNs, other sites on that same IP become reachable too.
- If Docker runs on the laptop, its FORWARD policy can block devices. The installer prints the two `iptables` lines that fix it.
- Only the laptop's LAN address answers on the LAN: DHCP, DNS, ping and the app ports. Other services on the laptop (SSH, and so on) are closed to the LAN.

## Files installed (Linux)

| Path | Purpose |
|---|---|
| `/usr/local/sbin/lan-gateway` | the script |
| `/etc/lan-gateway/` | `config.env` plus the generated `rules.nft` and `dnsmasq.conf` |
| `/etc/dnsmasq.d/lan-gateway.conf` | dnsmasq settings |
| `/etc/systemd/system/lan-gateway.service` | applies the LAN address and firewall at boot |
| `/etc/systemd/system/lan-gateway-app.service` | runs the Summit app (uninstall removes the service, keeps `~/gamesworkshop`) |
| `/etc/NetworkManager/conf.d/90-lan-gateway.conf` | keeps NetworkManager off the LAN port |
| `/etc/sysctl.d/90-lan-gateway.conf` | enables IPv4 forwarding |
