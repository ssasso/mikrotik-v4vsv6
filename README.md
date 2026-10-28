# mikrotik-v4vsv6

Per-interface **IPv4 vs IPv6 traffic graphs** for MikroTik routers, built on RRDtool.

MikroTik's SNMP counters give you total traffic per interface, but not how much of it is IPv4 and how much is IPv6. `v4vsv6` fills that gap with a single small Python script: it reads the byte counters of dedicated firewall rules through the RouterOS API, stores them in RRD files and renders stacked in/out graphs (IPv4 + IPv6) as static PNG files with an `index.html`. No database, no daemon, no Grafana: it is light enough for a Raspberry Pi and works alongside an existing monitoring system such as LibreNMS, Zabbix or Prometheus.

## Features

- IPv4 and IPv6 stacked graphs, separately for inbound and outbound traffic, for any number of interfaces
- Four time ranges per graph: 24 hours, 7 days, 30 days, 1 year
- Two rows of graphs per time range: **bandwidth** (bit/s) and **transferred volume** (bytes per hour / 6 hours / 12 hours / day), with the total volume and the IPv6 share in the legend
- Average, maximum and last value for each family, plus the IPv6 share over the displayed period
- Configurable sampling interval (default: 15 minutes)
- Configurable output directory (default: `/var/www/html/v4vsv6`), ready to be served by any web server
- Prints the RouterOS commands needed on the router (read-only API user and counter rules)
- Handles counter resets (reboots, re-created rules) without producing spikes
- Single script, no daemon: run it from cron or a systemd timer

## How it works

1. On the router, empty `passthrough` rules in `/ip firewall mangle` and `/ipv6 firewall mangle` count the bytes entering (`prerouting`) and leaving (`postrouting`) each monitored interface. Each rule carries a comment such as `v4v6:ether1:in`.
2. Every run, the script logs in to the RouterOS API with a read-only account, reads the rules and picks the ones whose comment starts with `v4v6:`.
3. The counters are stored in one RRD file per interface (`DERIVE` data sources, so resets are ignored rather than graphed as spikes).
4. The PNG graphs and `index.html` are regenerated in the output directory.

## Requirements

- A MikroTik router running RouterOS with the API service enabled (tested on: _fill in your RouterOS version_)
- A Linux host with:
  - Python 3.8+
  - [`rrdtool`](https://oss.oetiker.ch/rrdtool/) (command-line tool)
  - the [`routeros-api`](https://pypi.org/project/RouterOS-api/) Python library
  - a web server, if you want to browse the graphs (optional)

## Installation

On the Linux host (Debian/Raspberry Pi OS shown):

```sh
apt install rrdtool fonts-dejavu-core python3-pip
pip install --break-system-packages routeros-api   # PEP 668 systems; omit the flag elsewhere

mkdir -p /opt/v4vsv6
cp v4vsv6.py /opt/v4vsv6/
cp v4vsv6.conf.example /opt/v4vsv6/v4vsv6.conf
chmod 600 /opt/v4vsv6/v4vsv6.conf     # it contains the router password
```

Edit `/opt/v4vsv6/v4vsv6.conf` (see [Configuration](#configuration)), then generate the router configuration:

```sh
python3 /opt/v4vsv6/v4vsv6.py --print-routeros
```

Paste the output into a RouterOS terminal after replacing `<MONITOR_IP>` and the placeholder password. It creates:

- a group with only the `read` and `api` policies, and a user bound to it, restricted to the monitoring host's IP address
- the counter rules for every interface listed in the configuration, for both IPv4 and IPv6

Run it once by hand, then schedule it:

```sh
python3 /opt/v4vsv6/v4vsv6.py
```

```
# /etc/cron.d/v4vsv6
*/15 * * * * root python3 /opt/v4vsv6/v4vsv6.py
```

Graphs need at least two samples to appear, since the counters are cumulative. Point your web server at the output directory (default `/var/www/html/v4vsv6`) and open `index.html`.

## Configuration

The script reads `/opt/v4vsv6/v4vsv6.conf` by default; use `-c /path/to/file.conf` for another location.

```ini
[router]
host = 192.168.88.1
# 8728 = api, 8729 = api-ssl
port = 8728
use_ssl = no
ssl_verify = no
username = v4v6
password = CHANGEME

[general]
# Comma separated, as named in RouterOS
interfaces = ether1
# Also the RRD step: must match the cron schedule
interval_minutes = 15
output_dir = /var/www/html/v4vsv6
data_dir = /var/lib/v4vsv6
title = IPv4 vs IPv6 traffic
rrdtool = rrdtool
# Also draw volume (bytes) graphs under the bandwidth graphs
volume_graphs = yes
```

| Option | Default | Description |
| --- | --- | --- |
| `host` | – | Router address |
| `port` | `8728` | API port (`8729` for api-ssl) |
| `use_ssl` | `no` | Use the TLS API service |
| `ssl_verify` | `no` | Verify the router certificate and hostname |
| `username` / `password` | `v4v6` / – | Read-only RouterOS account |
| `interfaces` | – | Interfaces to graph, comma separated |
| `interval_minutes` | `15` | Sampling interval, also the RRD step |
| `output_dir` | `/var/www/html/v4vsv6` | Where PNG files and `index.html` are written |
| `data_dir` | `/var/lib/v4vsv6` | Where the RRD files are stored |
| `title` | `IPv4 vs IPv6 traffic` | Title of the HTML page |
| `rrdtool` | `rrdtool` | Path of the rrdtool binary |
| `volume_graphs` | `yes` | Draw the volume (bytes) graphs in addition to the bandwidth graphs |

### Adding an interface

Add it to `interfaces` and run `--print-routeros` again: only the rules for the new interface need to be pasted on the router.

### Data retention

Full resolution is kept for 14 days, roughly hourly averages for 120 days and roughly daily averages for 5 years. About 100 KB per interface.

## Volume graphs

Under each bandwidth graph there is a second graph showing how much data was transferred (IPv4 and IPv6 stacked). No extra data is collected: the RRD stores the average rate of every interval, and the volume of a bar is that rate multiplied by the bar length.

| Time range | One bar is |
| --- | --- |
| Last 24 hours | 1 hour |
| Last 7 days | 6 hours |
| Last 30 days | 12 hours |
| Last year | 1 day |

The legend shows the total volume of the period, the largest bar and the IPv6 share of the volume. Units are decimal (1 GB = 10^9 bytes). Set `volume_graphs = no` to disable them.

Things to know:

- Bars are aligned to the Unix epoch, so day boundaries are in UTC, not local time.
- Volumes are IP-level bytes counted by the firewall rules, without Ethernet headers: they are slightly lower than the interface counters or your ISP's figures. Do not use them to the byte against a data cap.
- Intervals in which the router could not be polled for more than two sampling periods count as unknown and are not estimated, so the totals can be underestimated after an outage. Counter resets (reboot, re-created rules) lose the traffic of that one interval.
- In the year graph the current day is not shown until it is complete.

## Command-line usage

```
v4vsv6.py                      collect + graph
v4vsv6.py --print-routeros     print the RouterOS commands (user + counter rules)
v4vsv6.py -c /path/file.conf   use a different configuration file
```

## Limitations and notes

- **FastTrack.** Packets handled by FastTrack bypass mangle, so they are not counted and the graphs under-report. Check with `/ip firewall filter print stats where action=fasttrack-connection` (and the `/ipv6` equivalent). If it is active, exclude the monitored interface, for example `/ip firewall filter set [find action=fasttrack-connection] in-interface=!ether1 out-interface=!ether1`, or disable FastTrack. To validate the numbers, compare IPv4 + IPv6 with the interface counters in your monitoring system: a few percent of difference is normal (Ethernet headers), a large gap means traffic is escaping the rules.
- **Scope of the counters.** The rules count traffic forwarded through the router and traffic to or from the router itself, on the monitored interface.
- **Changing the interval.** An RRD file cannot change its step. If you change `interval_minutes` after the first run, the script refuses to continue; delete or move the files in `data_dir` (history is lost) and update the cron schedule.
- **API security.** The plain API sends the password in clear text. Restrict the service and the user to the monitoring host's IP, or use `api-ssl` with a certificate on the router.
- **Other mangle rules.** If you have mangle rules with `passthrough=no`, place the counter rules above them, otherwise some packets are not counted.
- **Interface names.** Names are used in rule comments and file names; characters outside `A-Za-z0-9_.-` are replaced by `_` in file names.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `No counters found on the router` | Rules not created, wrong comment format, or the rules are disabled |
| `WARNING: no counters found for 'X'` | The interface name in the configuration does not match the one in the rule comments |
| `rrdtool not found` | Install `rrdtool` or set the `rrdtool` option |
| `Missing library` | `pip install routeros-api` |
| Authentication or connection error | Check user, password, `address=` restrictions on the user and the API service |
| Empty graphs on the first run | Wait for at least two samples |
| Garbled or missing text in the graphs | Install a font package, e.g. `fonts-dejavu-core` |
