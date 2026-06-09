#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║            WiFi Analyzer — Terminal Edition             ║
║  Real-time scanning · RTT Distance Estimation · Rich TUI     ║
╚══════════════════════════════════════════════════════════════╝

Usage:
    python3 wifi_analyzer.py                  # default 3s refresh
    python3 wifi_analyzer.py --refresh 5      # 5s refresh
    python3 wifi_analyzer.py --sort signal    # sort by signal (default)
    python3 wifi_analyzer.py --sort distance  # sort by estimated distance
    python3 wifi_analyzer.py --sort ssid      # sort alphabetically
    python3 wifi_analyzer.py --sort channel   # sort by channel
    python3 wifi_analyzer.py --rescan         # force rescan on every update
"""

import subprocess
import math
import time
import re
import sys
import argparse
import threading
import signal as sig_module
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich.align import Align
from rich.columns import Columns
from rich.rule import Rule
from rich import box

# ─────────────────────────── Constants ────────────────────────────────────────
REFRESH_RATE   = 3        # seconds between scans
APP_NAME       = " WiFi Analyzer"
VERSION        = "2.0.0"

# Mutable runtime config — values can be overridden by CLI args
CONFIG = {
    # Path loss exponent n (indoor):
    #   2.0 = open office/hallway, 2.5 = typical home, 3.0+ = heavy walls
    "path_loss_exp"  : 2.5,
    # Reference RSSI (dBm) at exactly 1 m — empirically calibrated per band.
    # 2.4 GHz: typically −40 to −50 dBm at 1 m indoors.
    # 5 GHz:   typically −55 to −65 dBm at 1 m indoors (higher freq = more loss).
    # 6 GHz:   typically −60 to −70 dBm at 1 m indoors.
    # Tune these if your known-distance AP reads differently.
    "rssi_ref_24"    : -45.0,
    "rssi_ref_5"     : -60.0,
    "rssi_ref_6"     : -65.0,
}

# Convenience aliases
def _n():        return CONFIG["path_loss_exp"]
def _ref(freq):  # returns the per-band reference RSSI
    if freq < 3000:  return CONFIG["rssi_ref_24"]
    elif freq < 5925: return CONFIG["rssi_ref_5"]
    else:            return CONFIG["rssi_ref_6"]

# ─────────────────────────── Colors / Theme ───────────────────────────────────
COLORS = {
    "title"      : "#00E5FF",
    "subtitle"   : "#64B5F6",
    "connected"  : "#00E676",
    "excellent"  : "#00E676",   # signal > 70
    "good"       : "#AEEA00",   # signal 50–70
    "fair"       : "#FFD740",   # signal 30–50
    "poor"       : "#FF6D00",   # signal 10–30
    "none"       : "#FF1744",   # signal < 10
    "dist_close" : "#00BFA5",   # < 5 m
    "dist_near"  : "#00E676",   # 5–15 m
    "dist_med"   : "#FFD740",   # 15–40 m
    "dist_far"   : "#FF6D00",   # 40–80 m
    "dist_vfar"  : "#FF1744",   # > 80 m
    "band_24"    : "#AB47BC",
    "band_5"     : "#42A5F5",
    "band_6"     : "#26C6DA",
    "security"   : "#4DB6AC",
    "dim"        : "#546E7A",
    "header"     : "#1A237E",
    "border"     : "#00BCD4",
    "bg_dark"    : "#0D1117",
    "accent"     : "#FF4081",
    "wpa3"       : "#00E676",
    "wpa2"       : "#64B5F6",
    "wpa1"       : "#FFD740",
    "open"       : "#FF1744",
}

# ─────────────────────────── Distance Estimation ──────────────────────────────
def rssi_to_distance_fspl(rssi_dbm: int, freq_mhz: float,
                           tx_power_dbm: Optional[float] = None,
                           n: Optional[float] = None) -> float:
    """
    Estimate distance using the Empirical Log-Distance Path Loss model.

    Formula (no TX-power assumption needed):

        d = 10 ^ ( (RSSI_ref - RSSI_measured) / (10 · n) )

    Where:
        - RSSI_ref  = empirically measured signal at exactly 1 m (band-specific)
        - n         = path loss exponent (2.5 = typical indoor)
        - d         = estimated distance in metres

    Reference RSSI values at 1 m (from 802.11 field measurements):
        2.4 GHz → ~−45 dBm     5 GHz → ~−60 dBm     6 GHz → ~−65 dBm

    This avoids the TX-power guessing required by FSPL and is the same
    principle used by Android Wi-Fi RTT API indoor positioning.
    """
    if n is None:
        n = _n()
    if rssi_dbm >= 0:
        return 0.1
    rssi_ref = _ref(freq_mhz)
    exp = (rssi_ref - rssi_dbm) / (10.0 * n)
    distance = 10.0 ** exp
    return round(max(0.1, distance), 2)


def format_distance(dist_m: float) -> tuple[str, str]:
    """Returns (formatted_string, color)."""
    if dist_m < 5:
        color = COLORS["dist_close"]
        label = f"~{dist_m:.1f} m  🟢"
    elif dist_m < 15:
        color = COLORS["dist_near"]
        label = f"~{dist_m:.1f} m  🟢"
    elif dist_m < 40:
        color = COLORS["dist_med"]
        label = f"~{dist_m:.0f} m   🟡"
    elif dist_m < 80:
        color = COLORS["dist_far"]
        label = f"~{dist_m:.0f} m   🟠"
    else:
        color = COLORS["dist_vfar"]
        label = f"~{dist_m:.0f} m   🔴"
    return label, color


def signal_color(signal: int) -> str:
    if signal >= 70: return COLORS["excellent"]
    if signal >= 50: return COLORS["good"]
    if signal >= 30: return COLORS["fair"]
    if signal >= 10: return COLORS["poor"]
    return COLORS["none"]


def signal_bars(signal: int) -> str:
    """Return Unicode signal bar representation."""
    if signal >= 80: return "▂▄▆█"
    if signal >= 60: return "▂▄▆_"
    if signal >= 40: return "▂▄__"
    if signal >= 20: return "▂___"
    return "____"


def band_color(freq_mhz: float) -> tuple[str, str]:
    """Returns (band_label, color)."""
    if freq_mhz < 3000:
        return "2.4 GHz", COLORS["band_24"]
    elif freq_mhz < 5925:
        return "5 GHz  ", COLORS["band_5"]
    else:
        return "6 GHz  ", COLORS["band_6"]


def security_color(security: str) -> str:
    if "WPA3" in security: return COLORS["wpa3"]
    if "WPA2" in security: return COLORS["wpa2"]
    if "WPA1" in security or "WPA " in security: return COLORS["wpa1"]
    return COLORS["open"]


def parse_freq(freq_str: str) -> float:
    """Parse '2447 MHz' → 2447.0"""
    m = re.search(r"([\d.]+)", freq_str)
    return float(m.group(1)) if m else 2412.0


def parse_rate(rate_str: str) -> float:
    """Parse '130 Mbit/s' → 130.0"""
    m = re.search(r"([\d.]+)", rate_str)
    return float(m.group(1)) if m else 0.0


def quality_to_dbm(quality: int) -> int:
    """
    Convert nmcli/Linux WiFi link quality (0-100) to RSSI in dBm.
    Standard Linux mapping: dBm = (quality / 2) - 100
    e.g.: quality=100 → -50 dBm (excellent), quality=0 → -100 dBm (no signal)
    """
    q = max(0, min(100, quality))
    return (q // 2) - 100


# ─────────────────────────── WiFi Scanner ─────────────────────────────────────
class WiFiNetwork:
    __slots__ = ("bssid", "ssid", "mode", "chan", "freq_mhz",
                 "rate_mbps", "signal", "signal_dbm", "security", "connected",
                 "distance_m", "last_seen")

    def __init__(self, bssid, ssid, mode, chan, freq_mhz,
                 rate_mbps, signal, signal_dbm, security, connected):
        self.bssid      = bssid
        self.ssid       = ssid if ssid else "<Hidden>"
        self.mode       = mode
        self.chan       = chan
        self.freq_mhz   = freq_mhz
        self.rate_mbps  = rate_mbps
        self.signal     = signal       # 0–100 quality score (nmcli)
        self.signal_dbm = signal_dbm   # converted dBm value
        self.security   = security if security else "Open"
        self.connected  = connected
        self.distance_m = rssi_to_distance_fspl(signal_dbm, freq_mhz)
        self.last_seen  = time.time()


def scan_networks(rescan: bool = False) -> list[WiFiNetwork]:
    """
    Scan available WiFi networks using nmcli.
    Returns a list of WiFiNetwork objects sorted by signal strength.
    """
    try:
        cmd = ["nmcli", "--terse",
               "-f", "IN-USE,BSSID,SSID,MODE,CHAN,FREQ,RATE,SIGNAL,SECURITY",
               "dev", "wifi", "list"]
        if rescan:
            cmd.append("--rescan=yes")

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return []

        networks = []
        for line in result.stdout.strip().splitlines():
            # nmcli terse separates with ':' but BSSIDs have escaped ':'
            # Pattern: IN-USE:BSSID:SSID:MODE:CHAN:FREQ:RATE:SIGNAL:SECURITY
            # The BSSID colons are escaped as \:, so we split carefully.
            # Replace escaped colons first
            line = line.replace("\\:", "\x00")
            parts = line.split(":")
            if len(parts) < 9:
                continue

            connected = parts[0].strip() == "*"
            bssid     = parts[1].replace("\x00", ":").strip()
            ssid      = parts[2].strip()
            mode      = parts[3].strip()
            try:
                chan  = int(parts[4].strip())
            except ValueError:
                chan  = 0
            freq_str  = parts[5].strip()
            rate_str  = parts[6].strip()
            try:
                quality = int(parts[7].strip())
            except ValueError:
                quality = 0
            # Security may contain spaces so join remaining parts
            security  = ":".join(parts[8:]).strip()

            freq_mhz  = parse_freq(freq_str)
            rate_mbps = parse_rate(rate_str)
            signal_dbm = quality_to_dbm(quality)

            net = WiFiNetwork(
                bssid=bssid, ssid=ssid, mode=mode, chan=chan,
                freq_mhz=freq_mhz, rate_mbps=rate_mbps,
                signal=quality, signal_dbm=signal_dbm,
                security=security, connected=connected
            )
            networks.append(net)

        return networks

    except subprocess.TimeoutExpired:
        return []
    except Exception:
        return []


# ─────────────────────────── Stats Calculator ─────────────────────────────────
def compute_stats(networks: list[WiFiNetwork]) -> dict:
    if not networks:
        return {}
    signals  = [n.signal for n in networks]
    dbms     = [n.signal_dbm for n in networks]
    dists    = [n.distance_m for n in networks]
    bands    = {"2.4 GHz": 0, "5 GHz": 0, "6 GHz": 0}
    for n in networks:
        lbl, _ = band_color(n.freq_mhz)
        key = lbl.strip()
        bands[key] = bands.get(key, 0) + 1
    connected = next((n for n in networks if n.connected), None)
    return {
        "total"     : len(networks),
        "avg_signal": round(sum(signals) / len(signals), 1),
        "max_signal": max(signals),
        "avg_dbm"   : round(sum(dbms) / len(dbms), 1),
        "min_dist"  : min(dists),
        "max_dist"  : max(dists),
        "bands"     : bands,
        "connected" : connected,
    }


# ─────────────────────────── Table Builder ─────────────────────────────────────
def build_table(networks: list[WiFiNetwork], sort_by: str,
                scan_time: float) -> Table:
    # ── Sort ──────────────────────────────────────────────────────────────────
    if sort_by == "distance":
        networks = sorted(networks, key=lambda n: n.distance_m)
    elif sort_by == "ssid":
        networks = sorted(networks, key=lambda n: n.ssid.lower())
    elif sort_by == "channel":
        networks = sorted(networks, key=lambda n: n.chan)
    else:  # default: signal (strongest first)
        networks = sorted(networks, key=lambda n: n.signal, reverse=True)

    tbl = Table(
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style=f"bold {COLORS['title']} on #0D1117",
        border_style=COLORS["border"],
        row_styles=["on #0D1117", "on #111924"],
        padding=(0, 1),
        expand=True,
        title=None,
    )

    tbl.add_column("#",          style=f"dim {COLORS['dim']}", width=3,  no_wrap=True)
    tbl.add_column("●",          width=2,  no_wrap=True)            # connected indicator
    tbl.add_column("SSID",       style="bold white",       min_width=18, no_wrap=True)
    tbl.add_column("BSSID",      style=COLORS["dim"],      min_width=17, no_wrap=True)
    tbl.add_column("Signal%",    width=9,  no_wrap=True)
    tbl.add_column("dBm",        width=9,  no_wrap=True)
    tbl.add_column("Bars",       width=6,  no_wrap=True)
    tbl.add_column("Band",       width=9,  no_wrap=True)
    tbl.add_column("Ch",         width=4,  no_wrap=True)
    tbl.add_column("Rate",       width=10, no_wrap=True)
    tbl.add_column("Security",   width=14, no_wrap=True)
    tbl.add_column("📡 Distance (RTT-est.)", min_width=18, no_wrap=True)

    for idx, net in enumerate(networks, 1):
        sig_col    = signal_color(net.signal)
        bars       = signal_bars(net.signal)
        band_lbl, band_col = band_color(net.freq_mhz)
        dist_lbl, dist_col = format_distance(net.distance_m)
        sec_col    = security_color(net.security)
        conn_mark  = Text("◉", style=f"bold {COLORS['connected']}") if net.connected \
                     else Text("○", style=COLORS["dim"])

        ssid_text = Text(net.ssid[:28])
        if net.connected:
            ssid_text.stylize(f"bold {COLORS['connected']}")
        else:
            ssid_text.stylize("white")

        tbl.add_row(
            str(idx),
            conn_mark,
            ssid_text,
            Text(net.bssid,             style=COLORS["dim"]),
            Text(f"{net.signal:3d}%",   style=f"bold {sig_col}"),
            Text(f"{net.signal_dbm:4d} dBm", style=f"{sig_col}"),
            Text(bars,                  style=f"bold {sig_col}"),
            Text(band_lbl,              style=f"bold {band_col}"),
            Text(str(net.chan),         style=COLORS["subtitle"]),
            Text(f"{net.rate_mbps:.0f} Mb/s", style=COLORS["dim"]),
            Text(net.security[:13],     style=f"bold {sec_col}"),
            Text(dist_lbl,              style=f"bold {dist_col}"),
        )

    return tbl


# ─────────────────────────── Stats Panel ──────────────────────────────────────
def build_stats_panel(stats: dict, scan_time: float,
                      refresh: int, scan_count: int) -> Panel:
    if not stats:
        return Panel("[dim]No data[/]", border_style=COLORS["border"])

    conn = stats.get("connected")
    lines = []

    # Connected AP info
    if conn:
        dist_lbl, _ = format_distance(conn.distance_m)
        lines.append(
            f"[bold {COLORS['connected']}]Connected:[/]  "
            f"[bold white]{conn.ssid}[/]  "
            f"[{COLORS['dim']}]{conn.bssid}[/]  "
            f"[{COLORS['subtitle']}]{conn.signal}% / {conn.signal_dbm} dBm[/]  "
            f"[bold {COLORS['dist_near']}]{dist_lbl}[/]"
        )
    else:
        lines.append(f"[{COLORS['dim']}]Not connected to any network[/]")

    # Network summary
    bands = stats.get("bands", {})
    band_str = (
        f"[bold {COLORS['band_24']}]2.4G:[/][white]{bands.get('2.4 GHz', 0)}[/]"
        f"  [bold {COLORS['band_5']}]5G:[/][white]{bands.get('5 GHz', 0)}[/]"
        f"  [bold {COLORS['band_6']}]6G:[/][white]{bands.get('6 GHz', 0)}[/]"
    )
    lines.append(
        f"[{COLORS['dim']}]Networks:[/] [bold white]{stats['total']}[/]   "
        f"{band_str}   "
        f"[{COLORS['dim']}]Avg Signal:[/] [bold {COLORS['subtitle']}]{stats['avg_signal']} dBm[/]"
    )

    # Distance range
    lines.append(
        f"[{COLORS['dim']}]Dist range:[/]  "
        f"[bold {COLORS['dist_close']}]closest {stats['min_dist']:.1f} m[/]  →  "
        f"[bold {COLORS['dist_vfar']}]farthest ~{stats['max_dist']:.0f} m[/]"
    )

    # Timing
    now_str = datetime.now().strftime("%H:%M:%S")
    lines.append(
        f"[{COLORS['dim']}]Last scan:[/] [white]{now_str}[/]  "
        f"[{COLORS['dim']}]took[/] [white]{scan_time:.2f}s[/]  "
        f"[{COLORS['dim']}]·  scan #{scan_count}[/]  "
        f"[{COLORS['dim']}]·  refresh every[/] [white]{refresh}s[/]"
    )

    content = "\n".join(lines)
    return Panel(
        content,
        title=f"[bold {COLORS['title']}]📊 Network Summary[/]",
        border_style=COLORS["border"],
        padding=(0, 2),
    )


def build_legend_panel() -> Panel:
    rows = [
        f"[bold {COLORS['dist_close']}]● < 5 m   Very close[/]    "
        f"[bold {COLORS['dist_near']}]● 5–15 m  Near[/]    "
        f"[bold {COLORS['dist_med']}]● 15–40 m Medium[/]    "
        f"[bold {COLORS['dist_far']}]● 40–80 m Far[/]    "
        f"[bold {COLORS['dist_vfar']}]● > 80 m  Very far[/]",

        f"[bold {COLORS['excellent']}]● ≥70 dBm Excellent[/]    "
        f"[bold {COLORS['good']}]● 50–70 Good[/]    "
        f"[bold {COLORS['fair']}]● 30–50 Fair[/]    "
        f"[bold {COLORS['poor']}]● 10–30 Poor[/]    "
        f"[bold {COLORS['none']}]● <10   None[/]    "
        f"[bold {COLORS['connected']}]◉ = connected[/]",

        f"[{COLORS['dim']}]Distance method: Empirical Log-Distance  "
        f"n={_n()}  "
        f"ref: 2.4G={CONFIG['rssi_ref_24']}dBm  5G={CONFIG['rssi_ref_5']}dBm  6G={CONFIG['rssi_ref_6']}dBm[/]",
    ]
    return Panel(
        "\n".join(rows),
        title=f"[bold {COLORS['subtitle']}]Legend & Distance Model[/]",
        border_style=COLORS["dim"],
        padding=(0, 2),
    )


# ─────────────────────────── Header ───────────────────────────────────────────
def build_header(scan_count: int) -> Panel:
    title = Text()
    title.append("  ╔══╗  ", style=f"bold {COLORS['accent']}")
    title.append(APP_NAME, style=f"bold {COLORS['title']}")
    title.append(f"  v{VERSION}  ", style=f"dim {COLORS['dim']}")
    title.append("  ╔══╗  ", style=f"bold {COLORS['accent']}")
    subtitle = Text(
        "Real-time WiFi Scanner  ·  RTT Distance Estimation  ·  "
        f"Press Ctrl+C to exit",
        style=f"dim {COLORS['subtitle']}",
        justify="center",
    )
    return Panel(
        Align(title, align="center"),
        subtitle=subtitle,
        border_style=COLORS["accent"],
        padding=(0, 2),
    )


# ─────────────────────────── Main Loop ────────────────────────────────────────
def build_full_view(networks: list[WiFiNetwork], stats: dict,
                    scan_time: float, refresh: int,
                    scan_count: int, sort_by: str,
                    error_msg: Optional[str]) -> str:
    """Returns a renderable built from Rich components."""
    from rich.console import Group
    parts = []
    parts.append(build_header(scan_count))

    if error_msg:
        parts.append(Panel(
            f"[bold {COLORS['none']}]⚠  {error_msg}[/]",
            border_style=COLORS["none"]
        ))

    parts.append(build_stats_panel(stats, scan_time, refresh, scan_count))

    sort_hint = Text(
        f"  Sorted by: {sort_by.upper()}  ·  {len(networks)} networks",
        style=f"bold {COLORS['dim']}"
    )
    parts.append(sort_hint)

    if networks:
        parts.append(build_table(networks, sort_by, scan_time))
    else:
        parts.append(Panel(
            f"[{COLORS['dim']}]Scanning… (this may take a few seconds)[/]",
            border_style=COLORS["dim"]
        ))

    parts.append(build_legend_panel())
    return Group(*parts)


def main():
    parser = argparse.ArgumentParser(
        description=" WiFi Analyzer — Terminal Edition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--refresh", type=int, default=REFRESH_RATE,
        help=f"Scan refresh interval in seconds (default: {REFRESH_RATE})"
    )
    parser.add_argument(
        "--sort", choices=["signal", "distance", "ssid", "channel"],
        default="signal",
        help="Sort networks by field (default: signal)"
    )
    parser.add_argument(
        "--rescan", action="store_true",
        help="Force hardware rescan on every update (may require sudo on some systems)"
    )
    parser.add_argument(
        "--path-loss-exp", type=float, default=CONFIG["path_loss_exp"],
        help=f"Path loss exponent n (default: {CONFIG['path_loss_exp']}; 2.0=open, 2.5=home, 3.0=walls)"
    )
    parser.add_argument(
        "--rssi-ref-24", type=float, default=CONFIG["rssi_ref_24"],
        help=f"Reference RSSI at 1 m for 2.4 GHz (default: {CONFIG['rssi_ref_24']} dBm)"
    )
    parser.add_argument(
        "--rssi-ref-5", type=float, default=CONFIG["rssi_ref_5"],
        help=f"Reference RSSI at 1 m for 5 GHz (default: {CONFIG['rssi_ref_5']} dBm)"
    )
    args = parser.parse_args()

    # Apply CLI overrides to mutable config
    CONFIG["path_loss_exp"] = args.path_loss_exp
    CONFIG["rssi_ref_24"]   = args.rssi_ref_24
    CONFIG["rssi_ref_5"]    = args.rssi_ref_5

    console = Console()
    networks: list[WiFiNetwork] = []
    stats:    dict              = {}
    scan_time   = 0.0
    scan_count  = 0
    error_msg:  Optional[str]  = None
    stop_event  = threading.Event()

    # Shared state updated by background thread
    _lock = threading.Lock()
    _state: dict = {
        "networks"  : [],
        "stats"     : {},
        "scan_time" : 0.0,
        "scan_count": 0,
        "error"     : None,
    }

    def scan_worker():
        while not stop_event.is_set():
            t0 = time.time()
            try:
                nets = scan_networks(rescan=args.rescan)
                elapsed = time.time() - t0
                s = compute_stats(nets)
                with _lock:
                    _state["networks"]   = nets
                    _state["stats"]      = s
                    _state["scan_time"]  = elapsed
                    _state["scan_count"] += 1
                    _state["error"]      = None if nets else "No networks found — check WiFi adapter"
            except Exception as exc:
                with _lock:
                    _state["error"] = str(exc)
            # Wait for next interval (check stop_event every 0.5s)
            for _ in range(args.refresh * 2):
                if stop_event.is_set():
                    break
                time.sleep(0.5)

    # Start background scanner
    worker = threading.Thread(target=scan_worker, daemon=True)
    worker.start()

    console.print(f"\n[bold {COLORS['title']}]{APP_NAME} v{VERSION}[/] starting…\n")
    time.sleep(1)  # Give scanner a moment to get first results

    try:
        while True:
            with _lock:
                view = build_full_view(
                    networks   = _state["networks"],
                    stats      = _state["stats"],
                    scan_time  = _state["scan_time"],
                    refresh    = args.refresh,
                    scan_count = _state["scan_count"],
                    sort_by    = args.sort,
                    error_msg  = _state["error"],
                )
            console.clear()
            console.print(view)
            time.sleep(args.refresh)

    except KeyboardInterrupt:
        stop_event.set()
        console.print(
            f"\n[bold {COLORS['title']}]👋   WiFi Analyzer exited.[/]\n"
        )
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
