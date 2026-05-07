# 🎯 OVERWATCH - WiFi Surveillance System

Real-time WiFi monitoring and device tracking system using a RT5572 adapter in monitor mode.

## 🏗️ System Architecture

```
📡 RT5572 WiFi Adapter (wlan1) [Monitor Mode]
    │
    ▼
📥 airodump-ng - Captures WiFi packets, scans channels 1-14
    │
    ▼
⚙️  monitor_live.py - Parses CSV, identifies vendors, tracks devices
    │
    ▼
🌐 FastAPI Server (port 8001) - REST API + WebSocket
    │
    ▼
💻 Web Dashboard - Live network map & device tracking
```

## 📊 Features

- **Network Detection**: Monitors all nearby WiFi networks (SSIDs, security, signal strength)
- **Device Tracking**: Tracks devices by MAC address with vendor identification
- **Proximity Detection**: Signal strength analysis for distance estimation
- **Event Logging**: Arrival/departure events stored in SQLite
- **Live Dashboard**: Real-time visualization of networks and devices
- **REST API**: Query devices, networks, and timeline events
- **WebSocket**: Live updates pushed to connected clients

## 🔍 What It Sees

### Networks (Access Points)
- SSID names
- MAC addresses (BSSIDs)
- Channel & frequency
- Security type (WPA2, WPA3, Open)
- Signal strength (dBm)

### Devices (Clients)
- MAC addresses
- Vendor identification (Apple, Samsung, Google, etc.)
- Signal strength & distance estimates
- Probe requests (networks they're searching for)
- Arrival/departure timestamps
- Movement tracking

## 🚀 Installation

### Requirements
- Raspberry Pi (or Linux system)
- RT5572 WiFi adapter (or compatible monitor-mode adapter)
- Python 3.7+
- aircrack-ng suite

### Setup

1. **Install dependencies:**
```bash
sudo apt-get update
sudo apt-get install aircrack-ng python3-pip
pip3 install -r requirements.txt
```

2. **Enable monitor mode:**
```bash
sudo airmon-ng check kill
sudo airmon-ng start wlan1
```

3. **Start airodump-ng:**
```bash
sudo airodump-ng wlan1 --write scan-01 --output-format csv --write-interval 1
```

4. **Link the CSV:**
```bash
ln -sf scan-01-01.csv scan-01.csv
```

5. **Start the parser:**
```bash
python3 monitor_live.py &
```

6. **Start the API server:**
```bash
python3 server.py
```

7. **Access the dashboard:**
   - Local: Open `dashboard/dashboard.html`
   - Remote: Navigate to `http://<PI_IP>:8001/dashboard.html`

## 📁 Project Structure

```
overwatch-wifi-monitor/
├── README.md              # This file
├── requirements.txt       # Python dependencies
├── monitor_live.py        # CSV parser & device tracker
├── server.py              # FastAPI backend
├── api.py                 # API helper functions
└── dashboard/
    ├── dashboard.html     # Main web UI
    ├── app.js             # Dashboard JavaScript
    └── style.css          # Dashboard styles
```

## 🌐 API Endpoints

### REST API
- `GET /devices` - List all networks and devices
- `GET /api/timeline` - Get event timeline
- `GET /docs` - Interactive API documentation (Swagger)

### WebSocket
- `WS /ws` - Live updates stream

### Example Usage
```bash
# Get device count
curl http://localhost:8001/devices | jq '.count'

# Get active devices
curl http://localhost:8001/devices | jq '.devices[] | select(.active==true)'

# Recent events
curl http://localhost:8001/api/timeline | jq '.events[:10]'

# Daily summary JSON
curl http://localhost:8001/api/daily-summary?hours=24 | jq
```

## Frankie Daily Review

For a daily AI-friendly review, run:

```bash
python3 overwatch_daily.py
```

This creates a private report in `reports/` with scanner health clues, event counts versus the previous window, close-signal alerts, packet bursts, new AP volume, top vendors, strongest devices, recurring devices, and next recommended checks.

Reports redact MAC addresses by default and `reports/` is ignored by Git. Use `--full-mac` only for a private local review.

Useful commands:

```bash
python3 overwatch_daily.py --hours 24 --json
python3 overwatch_daily.py --hours 72
python3 overwatch_daily.py --db /path/to/device_registry.db
```

## Configuration

Runtime paths can be set with environment variables:

```bash
export OVERWATCH_CSV_FILE=/home/matthew/scan-01.csv
export OVERWATCH_DB=/home/matthew/overwatch-wifi-monitor/device_registry.db
export OVERWATCH_EVENT_RETENTION_DAYS=30
export OVERWATCH_REPORT_DIR=/home/matthew/overwatch-wifi-monitor/reports
```

Dashboard API base is automatic when served from FastAPI. If opening `dashboard.html` directly from disk, set `ow_api_base` in browser localStorage or use the default Pi URL fallback.

## 💡 Use Cases

### 🏡 Home Security
- Alert when unknown devices appear
- Track family arrivals/departures
- Detect unexpected visitors

### 📊 Business Intelligence
- Foot traffic analysis
- Visitor dwell time measurement
- Return visitor identification
- Peak hours analysis

### 🔍 Network Reconnaissance
- Map nearby WiFi networks
- RF spectrum analysis
- Channel overlap detection
- Rogue AP identification

### 🎯 Proximity Marketing
- Detect known customers nearby
- Track repeat visitors
- Measure campaign engagement

## 🛠️ Management Commands

### Check Status
```bash
ps aux | grep -E "airodump|monitor_live"
curl http://localhost:8001/devices | jq '.count, .active_count'
```

### Restart Scanning
```bash
# Stop everything
sudo pkill airodump-ng
pkill -f monitor_live.py

# Restart (follow installation steps 3-6)
```

## 🔐 Privacy & Legal

**Important:** This system captures WiFi probe requests and MAC addresses.

- **Legal:** Generally legal for monitoring your own property in most jurisdictions
- **Ethics:** Consider disclosure if tracking customers/visitors
- **MAC randomization:** Modern phones use randomized MACs (iOS 14+, Android 10+)
- **Data retention:** Configure appropriate retention policies for your use case

**Recommendation:** Add privacy policy and data retention limits if used commercially.

## ⚡ Performance Tips

- **Reduce scan rate:** Use `--write-interval 5` for lower CPU usage
- **Specific channels:** Scan only Ch 1,6,11 instead of channel hopping
- **Berlin filter:** `--berlin 30` ignores weak signals
- **Database maintenance:** Periodically clean old events

## 🤝 Contributing

Contributions welcome! Please open an issue or submit a pull request.

## 📄 License

MIT License - See LICENSE file for details

---

**Status:** Fully operational  
**Author:** Matthew  
**Version:** 4.0
