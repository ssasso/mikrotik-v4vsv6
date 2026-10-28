#!/usr/bin/env -S python3 -u
"""
v4vsv6 - Per-interface IPv4 vs IPv6 traffic graphs for MikroTik routers.

Reads the byte counters of dedicated "passthrough" firewall rules (mangle, IPv4
and IPv6) through the RouterOS API. The rules are identified by their comment:

    v4v6:<interface>:<in|out>

The counters are stored in one RRD file per interface, and PNG graphs plus an
index.html are generated with RRDtool. For every time range two kinds of graphs
are produced: bandwidth (bit/s) and transferred volume (bytes per time bucket).

Run it periodically (cron / systemd timer) at the same interval as
interval_minutes in the configuration file.

Usage:
    v4vsv6.py                      # collect + graph (config: /opt/v4vsv6/v4vsv6.conf)
    v4vsv6.py --print-routeros     # print the RouterOS commands to paste on the router
    v4vsv6.py -c /other/path.conf  # use a configuration file in a different location
    python3 -u v4vsv6.py           # force unbuffered stdout/stderr for cron/systemd logs
"""
import argparse
import configparser
import html
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from types import SimpleNamespace

DEFAULT_CONFIG = "/opt/v4vsv6/v4vsv6.conf"
PREFIX = "v4v6"
FAMILIES = ("v4", "v6")
DIRECTIONS = ("in", "out")
DS_ORDER = [f"{f}_{d}" for f in FAMILIES for d in DIRECTIONS]  # v4_in v4_out v6_in v6_out
PERIODS = (
    ("day", "Last 24 hours", "-1d"),
    ("week", "Last 7 days", "-7d"),
    ("month", "Last 30 days", "-30d"),
    ("year", "Last year", "-400d"),
)
# Size of one bar in the volume graphs, in seconds (rounded to a multiple of the
# sampling step). RRDtool aligns the buckets to the Unix epoch, i.e. to UTC.
# Buckets matching a whole archive (daily, weekly) leave out the current,
# still incomplete bucket, so the sizes are kept small enough that recent traffic
# is always included, except for the year graph (today is missing there).
VOLUME_BUCKETS = {"day": 3600, "week": 21600, "month": 43200, "year": 86400}
COLOR = {"v4": "#2E6FD8", "v6": "#E8821E"}


# ---------------------------------------------------------------- configuration
def load_config(path):
    cp = configparser.ConfigParser(interpolation=None)
    if not cp.read(path):
        sys.exit(f"Error: configuration file not found: {path}")
    if not (cp.has_section("router") and cp.has_section("general")):
        sys.exit("Error: the [router] and [general] sections are required")
    r, g = cp["router"], cp["general"]
    interval = g.getint("interval_minutes", fallback=15)
    if interval < 1:
        sys.exit("Error: interval_minutes must be >= 1")
    interfaces = [i.strip() for i in g.get("interfaces", fallback="").split(",") if i.strip()]
    if not interfaces:
        sys.exit("Error: no interface set in [general] interfaces")
    return SimpleNamespace(
        host=r.get("host"),
        port=r.getint("port", fallback=8728),
        use_ssl=r.getboolean("use_ssl", fallback=False),
        ssl_verify=r.getboolean("ssl_verify", fallback=False),
        username=r.get("username", fallback="v4v6"),
        password=r.get("password", fallback=""),
        interfaces=interfaces,
        step=interval * 60,
        output_dir=g.get("output_dir", fallback="/var/www/html/v4vsv6"),
        data_dir=g.get("data_dir", fallback="/var/lib/v4vsv6"),
        title=g.get("title", fallback="IPv4 vs IPv6 traffic"),
        rrdtool=g.get("rrdtool", fallback="rrdtool"),
        volume_graphs=g.getboolean("volume_graphs", fallback=True),
    )


def safe(name):
    """Make an interface name safe to use in file names."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


# ---------------------------------------------------------------- RouterOS
def print_routeros(cfg):
    print("# Read-only API user (replace the IP address and the password)")
    print("/user group add name=v4v6-ro policy=read,api")
    print(f'/user add name={cfg.username} group=v4v6-ro password="CHANGEME" address=<MONITOR_IP>/32')
    print("/ip service set api address=<MONITOR_IP>/32 disabled=no")
    print("# (prefer api-ssl if the router is not on a trusted network: it needs a certificate)")
    print()
    for fam, path in (("IPv4", "/ip firewall mangle"), ("IPv6", "/ipv6 firewall mangle")):
        print(f"# {fam} counter rules")
        print(path)
        for i in cfg.interfaces:
            print(f'add chain=prerouting in-interface={i} action=passthrough comment="{PREFIX}:{i}:in"')
            print(f'add chain=postrouting out-interface={i} action=passthrough comment="{PREFIX}:{i}:out"')
        print()
    print("# If you have other mangle rules with passthrough=no, move these rules to the top.")


def fetch_counters(cfg):
    """Return {(interface, family, direction): bytes} read from the router."""
    try:
        import routeros_api
    except ImportError:
        sys.exit("Missing library: pip install routeros-api")
    pool = routeros_api.RouterOsApiPool(
        cfg.host, username=cfg.username, password=cfg.password, port=cfg.port,
        use_ssl=cfg.use_ssl, ssl_verify=cfg.ssl_verify, ssl_verify_hostname=cfg.ssl_verify,
        plaintext_login=True,
    )
    data = {}
    try:
        api = pool.get_api()
        for fam, path in (("v4", "/ip/firewall/mangle"), ("v6", "/ipv6/firewall/mangle")):
            for rule in api.get_resource(path).get():
                parts = rule.get("comment", "").split(":")
                if len(parts) < 3 or parts[0] != PREFIX or parts[-1] not in DIRECTIONS:
                    continue
                if rule.get("disabled") == "true":
                    continue
                data[(":".join(parts[1:-1]), fam, parts[-1])] = int(rule["bytes"])
    finally:
        pool.disconnect()
    return data


# ---------------------------------------------------------------- RRD
def rrd(cfg, *args):
    try:
        return subprocess.run([cfg.rrdtool, *args], check=True,
                              capture_output=True, text=True).stdout
    except FileNotFoundError:
        sys.exit("rrdtool not found (apt install rrdtool)")
    except subprocess.CalledProcessError as e:
        sys.exit(f"rrdtool {args[0]} failed: {e.stderr.strip()}")


def rra_args(step):
    """Full resolution for 14 days, ~hourly for 120 days, ~daily for 5 years."""
    spans = {}
    for cf, span in ((1, 14 * 86400),
                     (max(1, 3600 // step), 120 * 86400),
                     (max(1, 86400 // step), 5 * 365 * 86400)):
        spans[cf] = max(spans.get(cf, 0), span)
    return [f"RRA:AVERAGE:0.5:{cf}:{-(-span // (step * cf))}" for cf, span in sorted(spans.items())]


def ensure_rrd(cfg, path):
    if os.path.exists(path):
        m = re.search(r"^step = (\d+)", rrd(cfg, "info", path), re.M)
        if m and int(m.group(1)) != cfg.step:
            sys.exit(f"Error: {path} has a step of {m.group(1)}s but the configuration asks for "
                     f"{cfg.step}s. An RRD cannot change its step: move or delete it "
                     f"(history is lost) or restore the previous interval_minutes.")
        return
    ds = [f"DS:{n}:DERIVE:{cfg.step * 2}:0:U" for n in DS_ORDER]
    rrd(cfg, "create", path, "--step", str(cfg.step),
        "--start", str(int(time.time()) - cfg.step), *ds, *rra_args(cfg.step))


def update_rrd(cfg, path, iface, counters):
    vals = [str(counters[(iface, f, d)]) if (iface, f, d) in counters else "U"
            for f in FAMILIES for d in DIRECTIONS]
    if all(v == "U" for v in vals):
        print(f"WARNING: no counters found for '{iface}' "
              f"(missing rules or wrong comment)", file=sys.stderr)
    rrd(cfg, "update", path, "--template", ":".join(DS_ORDER),
        f"{int(time.time())}:" + ":".join(vals))


def draw_graph(cfg, rrd_file, iface, direction, label, start, out_png):
    """Bandwidth graph (bit/s), IPv4 and IPv6 stacked."""
    src = rrd_file.replace(":", "\\:")
    tmp = out_png + ".tmp"
    args = [
        "graph", tmp, "--imgformat", "PNG", "--start", start, "--end", "now",
        "--width", "800", "--height", "220", "--lower-limit", "0", "--slope-mode",
        "--title", f"{iface} - {direction.upper()} - {label}",
        "--vertical-label", "bit/s",
        f"DEF:b4={src}:v4_{direction}:AVERAGE",
        f"DEF:b6={src}:v6_{direction}:AVERAGE",
        "CDEF:v4=b4,8,*", "CDEF:v6=b6,8,*",
        "VDEF:v4avg=v4,AVERAGE", "VDEF:v4max=v4,MAXIMUM", "VDEF:v4last=v4,LAST",
        "VDEF:v6avg=v6,AVERAGE", "VDEF:v6max=v6,MAXIMUM", "VDEF:v6last=v6,LAST",
        "CDEF:share=v6,POP,v6avg,v4avg,v6avg,+,/,100,*",
        "VDEF:sharev=share,AVERAGE",
        f"AREA:v4{COLOR['v4']}:IPv4  ",
        "GPRINT:v4avg:avg %6.2lf %sbit/s", "GPRINT:v4max:max %6.2lf %sbit/s",
        "GPRINT:v4last:last %6.2lf %sbit/s\\l",
        f"STACK:v6{COLOR['v6']}:IPv6  ",
        "GPRINT:v6avg:avg %6.2lf %sbit/s", "GPRINT:v6max:max %6.2lf %sbit/s",
        "GPRINT:v6last:last %6.2lf %sbit/s\\l",
        "GPRINT:sharev:IPv6 share over the period\\: %5.1lf%%\\r",
    ]
    rrd(cfg, *args)
    os.replace(tmp, out_png)


def bucket_seconds(cfg, period_key):
    """Bar size for the volume graph: the nominal size rounded to a multiple of the step."""
    return max(1, round(VOLUME_BUCKETS[period_key] / cfg.step)) * cfg.step


def bucket_label(seconds):
    """Human readable bucket size: 3600 -> 'hour', 7200 -> '2 hours', 90 -> '1 min'..."""
    for size, unit in ((604800, "week"), (86400, "day"), (3600, "hour")):
        if seconds % size == 0:
            n = seconds // size
            return unit if n == 1 else f"{n} {unit}s"
    return f"{max(1, seconds // 60)} min"


def draw_volume_graph(cfg, rrd_file, iface, direction, label, start, bucket, out_png):
    """Volume graph (bytes transferred per time bucket), IPv4 and IPv6 stacked.

    The RRD stores the average rate (bytes/s) of every interval, so the volume of
    a bucket is rate * bucket length. Units are decimal (kB, MB, GB).
    """
    src = rrd_file.replace(":", "\\:")
    tmp = out_png + ".tmp"
    per = bucket_label(bucket)
    args = [
        "graph", tmp, "--imgformat", "PNG", "--start", start, "--end", "now",
        "--step", str(bucket),
        "--width", "800", "--height", "220", "--lower-limit", "0",
        "--title", f"{iface} - {direction.upper()} volume per {per} - {label}",
        "--vertical-label", f"bytes per {per}",
        f"DEF:b4={src}:v4_{direction}:AVERAGE",
        f"DEF:b6={src}:v6_{direction}:AVERAGE",
        f"CDEF:v4=b4,{bucket},*", f"CDEF:v6=b6,{bucket},*",
        # TOTAL multiplies the rate by the step: applied to the raw rate it gives bytes
        "VDEF:v4tot=b4,TOTAL", "VDEF:v6tot=b6,TOTAL",
        "VDEF:v4max=v4,MAXIMUM", "VDEF:v6max=v6,MAXIMUM",
        "CDEF:share=v6,POP,v6tot,v4tot,v6tot,+,/,100,*",
        "VDEF:sharev=share,AVERAGE",
        f"AREA:v4{COLOR['v4']}:IPv4  ",
        "GPRINT:v4tot:total %6.2lf %sB", f"GPRINT:v4max:max per {per} %6.2lf %sB\\l",
        f"STACK:v6{COLOR['v6']}:IPv6  ",
        "GPRINT:v6tot:total %6.2lf %sB", f"GPRINT:v6max:max per {per} %6.2lf %sB\\l",
        "GPRINT:sharev:IPv6 share of the volume\\: %5.1lf%%\\r",
    ]
    rrd(cfg, *args)
    os.replace(tmp, out_png)


# ---------------------------------------------------------------- HTML
def write_html(cfg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stamp = int(time.time())
    out = [f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{cfg.step}">
<title>{html.escape(cfg.title)}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, sans-serif; margin: 1rem auto; max-width: 1700px; padding: 0 1rem; }}
h1 {{ font-size: 1.4rem; }} h2 {{ margin-top: 2rem; }} h3 {{ margin: 1rem 0 .3rem; font-size: 1rem; }}
.row {{ display: flex; flex-wrap: wrap; gap: 1rem; }}
.row img {{ max-width: 100%; height: auto; background: #fff; }}
/* keep graph images visible and readable */
.row img {{ flex: 1 1 500px; min-width: 0; max-width: 100%; height: auto; background: #fff; }}
small {{ opacity: .7; }}
</style></head><body>
<h1>{html.escape(cfg.title)}</h1>
<small>Last update: {now} - interval: {cfg.step // 60} min</small>
"""]
    for iface in cfg.interfaces:
        out.append(f"<h2>{html.escape(iface)}</h2>")
        for key, label, _ in PERIODS:
            out.append(f"<h3>{html.escape(label)}</h3>")
            # first row: bandwidth, second row: transferred volume
            suffixes = [""] + (["_volume"] if cfg.volume_graphs else [])
            for suffix in suffixes:
                out.append("<div class='row'>")
                for d in DIRECTIONS:
                    png = f"{safe(iface)}_{d}_{key}{suffix}.png"
                    out.append(f"<img src='{png}?t={stamp}' alt='{html.escape(iface)} {d} {key}{suffix}'>")
                out.append("</div>")
    out.append("</body></html>")
    path = os.path.join(cfg.output_dir, "index.html")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    os.replace(path + ".tmp", path)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="IPv4 vs IPv6 traffic graphs for MikroTik routers")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                    help=f"configuration file (default: {DEFAULT_CONFIG})")
    ap.add_argument("--print-routeros", action="store_true",
                    help="print the RouterOS commands (read-only user + counter rules)")
    args = ap.parse_args()
    cfg = load_config(args.config)

    if args.print_routeros:
        print_routeros(cfg)
        return

    os.makedirs(cfg.data_dir, exist_ok=True)
    os.makedirs(cfg.output_dir, exist_ok=True)

    try:
        counters = fetch_counters(cfg)
    except Exception as e:  # network/auth errors: do not update anything
        sys.exit(f"Error contacting the router: {e}")
    if not counters:
        sys.exit("No counters found on the router: did you create the rules? (--print-routeros)")

    print("Counter summary:")
    for iface in cfg.interfaces:
        summary = " ".join(
            f"{fam}_{direction}={counters.get((iface, fam, direction), 'missing')}"
            for fam in FAMILIES for direction in DIRECTIONS
        )
        print(f"  {iface}: {summary}")

    for iface in cfg.interfaces:
        rrd_file = os.path.join(cfg.data_dir, safe(iface) + ".rrd")
        ensure_rrd(cfg, rrd_file)
        update_rrd(cfg, rrd_file, iface, counters)
        for key, label, start in PERIODS:
            for d in DIRECTIONS:
                png = os.path.join(cfg.output_dir, f"{safe(iface)}_{d}_{key}.png")
                draw_graph(cfg, rrd_file, iface, d, label, start, png)
                if cfg.volume_graphs:
                    vol_png = os.path.join(cfg.output_dir, f"{safe(iface)}_{d}_{key}_volume.png")
                    draw_volume_graph(cfg, rrd_file, iface, d, label, start,
                                      bucket_seconds(cfg, key), vol_png)
    write_html(cfg)


if __name__ == "__main__":
    main()
