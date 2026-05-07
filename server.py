"""
OVERWATCH v4 — FastAPI Server
Merges monitor_live.py parsing + WebSocket push + REST API + timeline.
Run:  uvicorn server:app --host 0.0.0.0 --port 8001
"""

import asyncio
import csv
import json
import os
import socket
import sqlite3
import subprocess
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent

CSV_FILE = "/home/matthew/scan-01.csv"
REGISTRY_DB = str(BASE_DIR / "device_registry.db")
STREAM_FILE = str(BASE_DIR / "stream.m3u8")

app = FastAPI(title="OVERWATCH v4")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════
devices: dict = {}
networks: dict = {}
events: list = []
prev_ap_set: set = set()
prev_device_set: set = set()
known_active: set = set()
signal_history: dict[str, deque] = {}
_db_cache = {"stats": None, "stats_ts": 0, "history": None, "history_ts": 0}
_latest_payload: dict | None = None

ws_clients: set[WebSocket] = set()

# ═══════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(REGISTRY_DB, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS known_devices (
        mac TEXT PRIMARY KEY,
        vendor TEXT DEFAULT '',
        type TEXT DEFAULT 'unknown',
        alias TEXT DEFAULT '',
        tag TEXT DEFAULT '',
        first_ever REAL,
        last_seen REAL,
        visits INTEGER DEFAULT 1,
        total_secs INTEGER DEFAULT 0,
        best_signal INTEGER DEFAULT -100,
        last_essid TEXT DEFAULT '',
        all_probes TEXT DEFAULT ''
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, type TEXT, mac TEXT DEFAULT '', detail TEXT DEFAULT ''
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kd_last ON known_devices(last_seen)")
    conn.commit()
    return conn

db = init_db()


def db_remember(dev_list):
    now = time.time()
    cur = db.cursor()
    currently_here = set()
    for d in dev_list:
        mac = d["mac"]
        currently_here.add(mac)
        sig = d.get("signal", -100)
        probes = ",".join(d.get("probed_ssids", []))
        cur.execute("SELECT visits, all_probes, best_signal FROM known_devices WHERE mac=?", (mac,))
        row = cur.fetchone()
        if row:
            old_visits, old_probes, old_best = row
            merged = set(filter(None, old_probes.split(","))) | set(filter(None, probes.split(",")))
            is_return = mac not in known_active
            cur.execute("""UPDATE known_devices SET vendor=?, type=?, last_seen=?,
                visits=visits+?, total_secs=total_secs+2,
                best_signal=MAX(best_signal,?), last_essid=?, all_probes=?
                WHERE mac=?""",
                (d.get("vendor", ""), d.get("type", "unknown"), now,
                 1 if is_return else 0,
                 sig, d.get("essid", ""), ",".join(merged), mac))
        else:
            cur.execute("""INSERT INTO known_devices
                (mac,vendor,type,first_ever,last_seen,visits,total_secs,best_signal,last_essid,all_probes)
                VALUES (?,?,?,?,?,1,0,?,?,?)""",
                (mac, d.get("vendor", ""), d.get("type", "unknown"), now, now,
                 sig, d.get("essid", ""), probes))
    db.commit()
    known_active.clear()
    known_active.update(currently_here)


def db_log_event(etype, mac="", detail=""):
    events.insert(0, {"ts": time.time(), "type": etype, "mac": mac, "detail": detail})
    if len(events) > 200:
        events[:] = events[:200]
    try:
        db.execute("INSERT INTO events (ts,type,mac,detail) VALUES (?,?,?,?)",
                   (time.time(), etype, mac, detail))
        db.execute("DELETE FROM events WHERE ts < ?", (time.time() - 86400 * 7,))
        db.commit()
    except Exception:
        pass


def db_get_stats():
    now = time.time()
    if _db_cache["stats"] and now - _db_cache["stats_ts"] < 10:
        return _db_cache["stats"]
    cur = db.cursor()
    day = now - 86400
    week = now - 604800
    cur.execute("SELECT COUNT(*) FROM known_devices")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM known_devices WHERE last_seen>?", (day,))
    today = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM known_devices WHERE visits>1 AND last_seen>?", (week,))
    returning = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM events WHERE ts>?", (day,))
    ev_today = cur.fetchone()[0]
    result = {"total_ever_seen": total, "seen_last_24h": today,
              "returning_this_week": returning, "events_today": ev_today}
    _db_cache["stats"] = result
    _db_cache["stats_ts"] = now
    return result


def db_get_history():
    now = time.time()
    if _db_cache["history"] and now - _db_cache["history_ts"] < 30:
        return _db_cache["history"]
    cur = db.cursor()
    day = now - 86400
    cur.execute("""SELECT vendor, COUNT(*) c FROM known_devices
        WHERE last_seen>? AND vendor NOT IN ('Unknown','')
        GROUP BY vendor ORDER BY c DESC LIMIT 10""", (day,))
    top_vendors = [{"vendor": r[0], "count": r[1]} for r in cur.fetchall()]
    cur.execute("""SELECT mac,vendor,type,visits,total_secs,best_signal,last_essid,alias,tag
        FROM known_devices WHERE visits>1 ORDER BY visits DESC LIMIT 15""")
    recurring = [{"mac": r[0], "vendor": r[1], "type": r[2], "visits": r[3],
                  "duration": r[4], "best_signal": r[5], "last_essid": r[6],
                  "alias": r[7], "tag": r[8]} for r in cur.fetchall()]
    cur.execute("SELECT COUNT(*) FROM known_devices")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM known_devices WHERE last_seen>?", (day,))
    today = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM events WHERE ts>?", (day,))
    ev_today = cur.fetchone()[0]
    result = {"top_vendors": top_vendors, "recurring_devices": recurring,
              "total_ever_seen": total, "seen_today": today, "events_today": ev_today}
    _db_cache["history"] = result
    _db_cache["history_ts"] = now
    return result


# ═══════════════════════════════════════════════════════════════
# OUI Vendor Database  (imported from monitor_live.py)
# ═══════════════════════════════════════════════════════════════
from monitor_live import OUI


def get_vendor(mac):
    return OUI.get(mac.upper()[0:8], "Unknown")


def distance(signal):
    if signal == -1:
        return "unknown"
    if signal > -50:
        return "close"
    elif signal > -70:
        return "medium"
    return "far"


def classify_device(vendor, mac, probed_ssids):
    first_byte = int(mac.split(":")[0], 16)
    if first_byte & 0x02:
        return "random"
    v = vendor.lower()
    if any(p in v for p in ["apple", "samsung", "huawei", "xiaomi", "oneplus", "google",
                            "motorola", "lg", "oppo", "vivo", "realme", "honor"]):
        return "phone"
    if any(p in v for p in ["dell", "lenovo", "hp", "hewlett", "intel", "microsoft",
                            "asus", "acer", "razer", "msi"]):
        return "laptop"
    if any(p in v for p in ["amazon", "ring", "nest", "ecobee", "tuya", "espressif",
                            "shenzhen", "tp-link", "sonos", "roku", "wyze", "vizio",
                            "broadlink", "wemo", "philips hue"]):
        return "iot"
    if any(p in v for p in ["cisco", "ubiquiti", "aruba", "netgear", "linksys",
                            "meraki", "mikrotik", "ruckus", "cambium", "juniper"]):
        return "infra"
    return "unknown"


# ═══════════════════════════════════════════════════════════════
# Presence Profiles
# ═══════════════════════════════════════════════════════════════

_presence_cache: dict[str, dict] = {}
_presence_cache_ts: float = 0

def refresh_presence_cache():
    global _presence_cache, _presence_cache_ts
    now = time.time()
    if now - _presence_cache_ts < 10:
        return
    _presence_cache_ts = now
    cur = db.cursor()
    cur.execute("SELECT mac, visits, total_secs, first_ever, tag FROM known_devices")
    cache = {}
    for mac, visits, total_secs, first_ever, tag in cur.fetchall():
        age_days = (now - first_ever) / 86400 if first_ever else 0
        cache[mac] = {
            "visits": visits, "total_secs": total_secs,
            "first_ever": first_ever, "age_days": age_days, "tag": tag,
        }
    _presence_cache = cache


def classify_presence(mac, device):
    """Classify device into resident/regular/passerby/anomaly/new."""
    history = _presence_cache.get(mac)
    if not history or history["visits"] <= 1:
        if device.get("threat_level") == "high":
            return "anomaly"
        return "new"

    visits = history["visits"]
    total_secs = history["total_secs"]

    if visits >= 5 and total_secs >= 3600:
        return "resident"
    if visits >= 3 or total_secs >= 1800:
        return "regular"

    if device.get("threat_level") == "high":
        return "anomaly"
    return "passerby"


# ═══════════════════════════════════════════════════════════════
# Threat Scoring
# ═══════════════════════════════════════════════════════════════

def compute_threat(device):
    score = 0
    reasons = []
    if device.get("type") == "random":
        score += 2; reasons.append("random_mac")
    probes = [s for s in device.get("probed_ssids", []) if s]
    if probes and not device.get("associated"):
        score += 2; reasons.append("probing")
    sig = device.get("signal", -1)
    if sig > -45 and sig != -1:
        score += 3; reasons.append("strong_signal")
    if device.get("packet_rate", 0) > 50:
        score += 2; reasons.append("high_pkt_rate")
    if time.time() - device.get("first_seen", time.time()) < 120:
        score += 1; reasons.append("new_device")
    level = "high" if score >= 6 else "medium" if score >= 3 else "low"
    device["threat_score"] = score
    device["threat_level"] = level
    device["threat_reasons"] = reasons


# ═══════════════════════════════════════════════════════════════
# Movement Tracking
# ═══════════════════════════════════════════════════════════════

def compute_movement(mac, current_signal):
    if mac not in signal_history:
        signal_history[mac] = deque(maxlen=15)
    hist = signal_history[mac]
    hist.append(current_signal)
    if len(hist) < 6:
        return "unknown"
    recent = [v for v in list(hist)[-3:] if v != -1]
    older = [v for v in list(hist)[-6:-3] if v != -1]
    if not recent or not older:
        return "unknown"
    avg_recent = sum(recent) / len(recent)
    avg_older = sum(older) / len(older)
    delta = avg_recent - avg_older
    if delta > 3:
        return "approaching"
    elif delta < -3:
        return "leaving"
    return "stationary"


# ═══════════════════════════════════════════════════════════════
# System Health
# ═══════════════════════════════════════════════════════════════

def _read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""

def _file_age(path):
    try:
        return time.time() - os.path.getmtime(path)
    except Exception:
        return -1

def _get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"

def _iface_up(name):
    return _read_file(f"/sys/class/net/{name}/operstate") == "up"

_cpu_prev = (0, 0)

def get_system_health():
    global _cpu_prev
    h = {}
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        idle = int(parts[4])
        total = sum(int(p) for p in parts[1:])
        pi, pt = _cpu_prev
        di, dt = idle - pi, total - pt
        h["cpu_pct"] = round(100 * (1 - di / max(dt, 1)), 1)
        _cpu_prev = (idle, total)
    except Exception:
        h["cpu_pct"] = -1
    try:
        mem = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":")
            mem[k.strip()] = int(v.strip().split()[0])
        total_mb = mem.get("MemTotal", 0) / 1024
        avail_mb = mem.get("MemAvailable", mem.get("MemFree", 0)) / 1024
        h["ram_total_mb"] = round(total_mb)
        h["ram_used_mb"] = round(total_mb - avail_mb)
        h["ram_pct"] = round(100 * (total_mb - avail_mb) / max(total_mb, 1), 1)
    except Exception:
        h["ram_total_mb"] = 0; h["ram_used_mb"] = 0; h["ram_pct"] = -1
    try:
        st = os.statvfs("/")
        total_gb = (st.f_blocks * st.f_frsize) / (1024 ** 3)
        free_gb = (st.f_bfree * st.f_frsize) / (1024 ** 3)
        h["disk_total_gb"] = round(total_gb, 1)
        h["disk_used_gb"] = round(total_gb - free_gb, 1)
        h["disk_pct"] = round(100 * (total_gb - free_gb) / max(total_gb, 1), 1)
    except Exception:
        h["disk_total_gb"] = 0; h["disk_used_gb"] = 0; h["disk_pct"] = -1
    raw = _read_file("/sys/class/thermal/thermal_zone0/temp")
    h["cpu_temp_c"] = round(int(raw) / 1000, 1) if raw.isdigit() else -1
    raw = _read_file("/proc/uptime")
    h["uptime_sec"] = int(float(raw.split()[0])) if raw else 0
    h["ip"] = _get_ip()
    h["hostname"] = socket.gethostname()
    h["eth0_up"] = _iface_up("eth0")
    h["wlan0_up"] = _iface_up("wlan0")
    h["wlan1_up"] = _iface_up("wlan1")
    h["wlan1mon_up"] = _iface_up("wlan1mon")
    h["csv_age"] = round(_file_age(CSV_FILE), 1)
    h["json_age"] = -1
    h["stream_age"] = round(_file_age(STREAM_FILE), 1)
    h["parser_ts"] = time.time()
    return h


# ═══════════════════════════════════════════════════════════════
# RF Summary
# ═══════════════════════════════════════════════════════════════

def get_rf_summary():
    chan_24, chan_5, enc_counts, hidden, strongest = {}, {}, {}, 0, []
    for n in networks.values():
        ch = n.get("channel", 0)
        if ch <= 0:
            continue
        bucket = chan_24 if ch <= 14 else chan_5
        bucket[ch] = bucket.get(ch, 0) + 1
        enc = (n.get("privacy") or "OPN").strip()
        enc_counts[enc] = enc_counts.get(enc, 0) + 1
        if not n.get("essid"):
            hidden += 1
        strongest.append({"bssid": n["bssid"], "essid": n.get("essid", "<hidden>"),
                          "channel": ch, "power": n.get("power", -100), "privacy": enc})
    strongest.sort(key=lambda x: x["power"], reverse=True)
    client_counts = {}
    for d in devices.values():
        b = d.get("bssid", "")
        if b and b != "(not associated)" and len(b) == 17:
            client_counts[b] = client_counts.get(b, 0) + 1
    ap_clients = []
    for n in networks.values():
        ap_clients.append({"bssid": n["bssid"], "essid": n.get("essid", ""),
                           "channel": n.get("channel", 0), "privacy": n.get("privacy", ""),
                           "power": n.get("power", -100),
                           "clients": client_counts.get(n["bssid"], 0)})
    ap_clients.sort(key=lambda x: x["clients"], reverse=True)
    total_24 = sum(chan_24.values())
    total_5 = sum(chan_5.values())
    all_ch = list(chan_24.items()) + list(chan_5.items())
    busiest_ch = max(all_ch, key=lambda x: x[1]) if all_ch else (0, 0)
    return {"channels_24": chan_24, "channels_5": chan_5,
            "total_24": total_24, "total_5": total_5,
            "busiest_channel": busiest_ch[0], "busiest_count": busiest_ch[1],
            "encryption": enc_counts, "hidden_count": hidden,
            "strongest_aps": strongest[:10], "ap_by_clients": ap_clients[:15]}


# ═══════════════════════════════════════════════════════════════
# CSV Parser (runs every ~1 s in background task)
# ═══════════════════════════════════════════════════════════════

def parse():
    global devices, networks
    try:
        with open(CSV_FILE, newline="", errors="ignore") as f:
            reader = csv.reader(f)
            section = None
            temp_networks, temp_devices = {}, {}
            for row in reader:
                if len(row) < 2:
                    continue
                header = row[0].strip()
                if "BSSID" in header and "Station" not in header:
                    section = "ap"; continue
                elif "Station MAC" in header:
                    section = "station"; continue
                if section == "ap":
                    try:
                        bssid = row[0].strip().upper()
                        if not bssid or len(bssid) < 17:
                            continue
                        channel_str = row[3].strip() if len(row) > 3 else ""
                        channel = int(channel_str) if channel_str.lstrip("-").isdigit() else 0
                        privacy = row[5].strip() if len(row) > 5 else ""
                        cipher = row[6].strip() if len(row) > 6 else ""
                        auth = row[7].strip() if len(row) > 7 else ""
                        power_str = row[8].strip() if len(row) > 8 else "-1"
                        power = int(power_str) if power_str.lstrip("-").isdigit() else -1
                        essid = row[13].strip() if len(row) > 13 else ""
                        temp_networks[bssid] = {"bssid": bssid, "essid": essid, "channel": channel,
                                                "privacy": privacy, "cipher": cipher,
                                                "auth": auth, "power": power}
                    except (IndexError, ValueError):
                        continue
                elif section == "station":
                    try:
                        mac = row[0].strip().upper()
                        if not mac or len(mac) < 17:
                            continue
                        now = time.time()
                        fs_str = row[1].strip() if len(row) > 1 else ""
                        ls_str = row[2].strip() if len(row) > 2 else ""
                        try:
                            first_seen_ts = time.mktime(time.strptime(fs_str, "%Y-%m-%d %H:%M:%S")) if fs_str else now
                        except (ValueError, OverflowError):
                            first_seen_ts = now
                        try:
                            last_seen_ts = time.mktime(time.strptime(ls_str, "%Y-%m-%d %H:%M:%S")) if ls_str else now
                        except (ValueError, OverflowError):
                            last_seen_ts = now
                        signal_str = row[3].strip()
                        signal = int(signal_str) if signal_str.lstrip("-").isdigit() else -1
                        packets_str = row[4].strip()
                        packets = int(packets_str) if packets_str.isdigit() else 0
                        bssid = row[5].strip().upper()
                        probed = row[6].strip() if len(row) > 6 else ""
                        vendor = get_vendor(mac)
                        probed_list = [s.strip() for s in probed.split(",") if s.strip()] if probed else []
                        prev = devices.get(mac, {})
                        packet_rate = max(0, packets - prev.get("packets", 0))
                        is_active = (now - last_seen_ts) < 30
                        is_associated = bssid != "(NOT ASSOCIATED)" and len(bssid) == 17
                        essid, channel, encryption, cipher_dev, auth_dev = "", 0, "", "", ""
                        if is_associated and bssid in temp_networks:
                            net = temp_networks[bssid]
                            essid = net["essid"]; channel = net["channel"]
                            encryption = net["privacy"]; cipher_dev = net["cipher"]; auth_dev = net["auth"]
                        dev_type = classify_device(vendor, mac, probed_list)
                        movement = compute_movement(mac, signal)
                        dev = {
                            "mac": mac, "vendor": vendor, "type": dev_type,
                            "signal": signal, "distance": distance(signal),
                            "packets": packets, "packet_rate": packet_rate,
                            "bssid": bssid if is_associated else "(not associated)",
                            "associated": is_associated, "essid": essid,
                            "channel": channel, "encryption": encryption,
                            "cipher": cipher_dev, "auth": auth_dev,
                            "probed_ssids": probed_list,
                            "first_seen": first_seen_ts, "last_seen": last_seen_ts,
                            "duration": int(last_seen_ts - first_seen_ts),
                            "active": is_active, "movement": movement,
                        }
                        compute_threat(dev)
                        dev["presence"] = classify_presence(mac, dev)
                        temp_devices[mac] = dev
                    except (IndexError, ValueError):
                        continue
            networks = temp_networks
            for mac, dev in temp_devices.items():
                devices[mac] = dev
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[ERROR] Parse failed: {e}")


def cleanup():
    now = time.time()
    for mac in list(devices.keys()):
        age = now - devices[mac]["last_seen"]
        if age > 30:
            devices[mac]["active"] = False
        if age > 300:
            del devices[mac]
            signal_history.pop(mac, None)


def generate_events():
    global prev_ap_set, prev_device_set
    now_devs = set(d["mac"] for d in devices.values() if d["active"])
    now_aps = set(networks.keys())
    for mac in now_devs - prev_device_set:
        d = devices.get(mac, {})
        detail = f"{d.get('vendor', '?')} | {d.get('signal', 0)} dBm | {d.get('distance', '?')}"
        db_log_event("device_join", mac, detail)
        if d.get("distance") == "close" and d.get("signal", -1) != -1:
            db_log_event("close_alert", mac, f"Strong signal: {d.get('signal')} dBm")
    for mac in prev_device_set - now_devs:
        d = devices.get(mac, {})
        db_log_event("device_leave", mac, d.get("vendor", ""))
    for bssid in now_aps - prev_ap_set:
        n = networks.get(bssid, {})
        db_log_event("new_ap", bssid, f"{n.get('essid', '<hidden>')} ch{n.get('channel', '?')}")
    for d in devices.values():
        if d["active"] and d.get("packet_rate", 0) > 50:
            db_log_event("burst", d["mac"], f"{d['packet_rate']} pkts/s")
    prev_device_set = now_devs
    prev_ap_set = now_aps


def build_payload():
    dev_list = list(devices.values())
    presence_counts = {"resident": 0, "regular": 0, "passerby": 0, "anomaly": 0, "new": 0}
    for d in dev_list:
        p = d.get("presence", "new")
        if p in presence_counts:
            presence_counts[p] += 1
    familiar = presence_counts["resident"] + presence_counts["regular"]
    unknown = presence_counts["passerby"] + presence_counts["anomaly"] + presence_counts["new"]
    return {
        "timestamp": time.time(),
        "count": len(devices),
        "active_count": sum(1 for d in dev_list if d["active"]),
        "networks": list(networks.values()),
        "devices": dev_list,
        "registry": db_get_stats(),
        "system": get_system_health(),
        "rf": get_rf_summary(),
        "events": events[:50],
        "history": db_get_history(),
        "presence_summary": {
            "familiar": familiar,
            "unknown": unknown,
            "counts": presence_counts,
        },
    }


# ═══════════════════════════════════════════════════════════════
# Background parse loop
# ═══════════════════════════════════════════════════════════════

async def parse_loop():
    global _latest_payload
    while True:
        refresh_presence_cache()
        parse()
        cleanup()
        generate_events()
        active_devs = [d for d in devices.values() if d["active"]]
        if active_devs:
            db_remember(active_devs)
        _latest_payload = build_payload()

        payload_json = json.dumps(_latest_payload)
        stale = []
        for ws in ws_clients.copy():
            try:
                await ws.send_text(payload_json)
            except Exception:
                stale.append(ws)
        for ws in stale:
            ws_clients.discard(ws)

        await asyncio.sleep(1)


@app.on_event("startup")
async def startup():
    asyncio.create_task(parse_loop())
    print(f"OVERWATCH v4 server starting")
    print(f"  CSV: {CSV_FILE}")
    print(f"  DB:  {REGISTRY_DB}")
    print(f"  OUI: {len(OUI)} vendor prefixes")


# ═══════════════════════════════════════════════════════════════
# REST Endpoints
# ═══════════════════════════════════════════════════════════════

@app.get("/devices")
async def get_devices():
    if _latest_payload:
        return JSONResponse(_latest_payload)
    return JSONResponse({"timestamp": time.time(), "count": 0, "active_count": 0,
                         "networks": [], "devices": [], "events": []})


@app.get("/api/timeline")
async def get_timeline(mac: str = Query(default=None), hours: float = Query(default=24)):
    cur = db.cursor()
    cutoff = time.time() - hours * 3600
    if mac:
        cur.execute("SELECT ts, type, mac, detail FROM events WHERE mac=? AND ts>? ORDER BY ts DESC LIMIT 200",
                    (mac.upper(), cutoff))
    else:
        cur.execute("SELECT ts, type, mac, detail FROM events WHERE ts>? ORDER BY ts DESC LIMIT 500",
                    (cutoff,))
    rows = [{"ts": r[0], "type": r[1], "mac": r[2], "detail": r[3]} for r in cur.fetchall()]
    return JSONResponse({"events": rows, "count": len(rows)})


@app.get("/api/timeline/{mac}")
async def get_device_timeline(mac: str, hours: float = Query(default=24)):
    cur = db.cursor()
    cutoff = time.time() - hours * 3600
    cur.execute("SELECT ts, type, mac, detail FROM events WHERE mac=? AND ts>? ORDER BY ts DESC LIMIT 200",
                (mac.upper(), cutoff))
    rows = [{"ts": r[0], "type": r[1], "mac": r[2], "detail": r[3]} for r in cur.fetchall()]

    cur.execute("SELECT * FROM known_devices WHERE mac=?", (mac.upper(),))
    dev_row = cur.fetchone()
    device_info = None
    if dev_row:
        device_info = {"mac": dev_row[0], "vendor": dev_row[1], "type": dev_row[2],
                       "alias": dev_row[3], "tag": dev_row[4], "first_ever": dev_row[5],
                       "last_seen": dev_row[6], "visits": dev_row[7], "total_secs": dev_row[8],
                       "best_signal": dev_row[9], "last_essid": dev_row[10],
                       "all_probes": dev_row[11].split(",") if dev_row[11] else []}
    return JSONResponse({"events": rows, "count": len(rows), "device": device_info})


# ═══════════════════════════════════════════════════════════════
# WebSocket
# ═══════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)
    try:
        if _latest_payload:
            await websocket.send_text(json.dumps(_latest_payload))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(websocket)


# ═══════════════════════════════════════════════════════════════
# Static files — served LAST so API routes take priority
# ═══════════════════════════════════════════════════════════════
app.mount("/", StaticFiles(directory=str(BASE_DIR), html=True), name="static")
