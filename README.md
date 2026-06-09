# JYNXX WiFi Analyzer — Terminal Edition

Real-time WiFi scanner with **RTT-estimated distance** running in your terminal.

## Requirements

- Linux with `nmcli` (NetworkManager CLI) — comes pre-installed on Ubuntu/Fedora/Arch
- Python 3.10+
- `rich` library: `pip3 install rich` (already installed on this system)

## Usage

```bash
# Default: sort by signal strength, refresh every 3s
python3 wifi_analyzer.py

# Sort by estimated distance (closest first)
python3 wifi_analyzer.py --sort distance

# Sort alphabetically by SSID
python3 wifi_analyzer.py --sort ssid

# Sort by channel
python3 wifi_analyzer.py --sort channel

# Faster refresh (every 2 seconds)
python3 wifi_analyzer.py --refresh 2

# Force hardware rescan on every update
python3 wifi_analyzer.py --rescan

# Indoor mode (higher path loss exponent for walls/obstructions)
python3 wifi_analyzer.py --path-loss-exp 3.0

# If your router uses a different TX power (default: 20 dBm)
python3 wifi_analyzer.py --tx-power 23
```

Press **Ctrl+C** to exit.

## Distance Estimation (RTT Model)

Since Linux doesn't expose hardware FTM/IEEE 802.11mc ranging via standard userspace tools, distance is estimated using the **Log-Distance Path Loss (FSPL) model** — the exact same physics formula Android's Wi-Fi RTT API uses as its calibration baseline and fallback for non-FTM access points:

```
d = 10 ^ ( (TxPower + AntennaGain - RSSI - 20·log10(freq_MHz) - 27.55) / (10·n) )
```

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `n` | 2.0 | Path loss exponent (2 = free space; 2.7–3.5 = indoor with walls) |
| TX Power | 20 dBm | Assumed AP transmit power (typical consumer AP) |
| Antenna Gain | 2 dBi | Typical omnidirectional antenna gain |

**Accuracy:** ~1–5 m in open space; less accurate through walls. Use `--path-loss-exp 3.0` for multi-wall indoor scenarios.

## Display

| Column | Description |
|--------|-------------|
| ◉ | Currently connected AP |
| Signal% | nmcli quality score (0–100%) |
| dBm | Converted RSSI in decibel-milliwatts |
| Bars | Visual signal strength indicator |
| Band | 2.4 GHz (purple) / 5 GHz (blue) / 6 GHz (cyan) |
| Ch | WiFi channel |
| Rate | Maximum link rate (Mbit/s) |
| Security | WPA3 (green) / WPA2 (blue) / WPA1 (yellow) / Open (red) |
| Distance | RTT-estimated distance with color coding |

### Distance Color Coding
- 🟢 `< 5 m` — Very close
- 🟢 `5–15 m` — Near
- 🟡 `15–40 m` — Medium
- 🟠 `40–80 m` — Far
- 🔴 `> 80 m` — Very far
