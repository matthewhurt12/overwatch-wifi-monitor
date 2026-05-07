import csv
import json
import time
import sys
import os
import sqlite3
import subprocess
import socket

CSV_FILE = "/home/matthew/scan-01.csv"
OUTPUT_FILE = "/home/matthew/camera-dashboard/devices.json"
REGISTRY_DB = "/home/matthew/camera-dashboard/device_registry.db"
STREAM_FILE = "/home/matthew/camera-dashboard/stream.m3u8"

devices = {}
networks = {}
events = []
prev_ap_set = set()
prev_device_set = set()


# ═══ PERSISTENT DEVICE REGISTRY (SQLite) ═══

# ═══ LIGHTWEIGHT DATABASE ═══

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

known_active = set()
_db_cache = {"stats": None, "stats_ts": 0, "history": None, "history_ts": 0}


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
                (d.get("vendor",""), d.get("type","unknown"), now,
                 1 if is_return else 0,
                 sig, d.get("essid",""), ",".join(merged), mac))
        else:
            cur.execute("""INSERT INTO known_devices
                (mac,vendor,type,first_ever,last_seen,visits,total_secs,best_signal,last_essid,all_probes)
                VALUES (?,?,?,?,?,1,0,?,?,?)""",
                (mac, d.get("vendor",""), d.get("type","unknown"), now, now,
                 sig, d.get("essid",""), probes))
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
    recurring = [{"mac":r[0],"vendor":r[1],"type":r[2],"visits":r[3],
                  "duration":r[4],"best_signal":r[5],"last_essid":r[6],
                  "alias":r[7],"tag":r[8]} for r in cur.fetchall()]

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


def db_set_alias(mac, alias):
    db.execute("UPDATE known_devices SET alias=? WHERE mac=?", (alias, mac))
    db.commit()

def db_set_tag(mac, tag):
    db.execute("UPDATE known_devices SET tag=? WHERE mac=?", (tag, mac))
    db.commit()

def db_get_device(mac):
    cur = db.cursor()
    cur.execute("SELECT * FROM known_devices WHERE mac=?", (mac,))
    row = cur.fetchone()
    if not row: return None
    return {"mac":row[0],"vendor":row[1],"type":row[2],"alias":row[3],"tag":row[4],
            "first_ever":row[5],"last_seen":row[6],"visits":row[7],"total_secs":row[8],
            "best_signal":row[9],"last_essid":row[10],
            "all_probes":row[11].split(",") if row[11] else []}


# ═══ PI SYSTEM HEALTH ═══

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

def _service_active(name):
    try:
        r = subprocess.run(["systemctl", "is-active", name],
                           capture_output=True, text=True, timeout=2)
        return r.stdout.strip() == "active"
    except Exception:
        return False

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
    state = _read_file(f"/sys/class/net/{name}/operstate")
    return state == "up"

def get_system_health():
    h = {}

    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        idle = int(parts[4])
        total = sum(int(p) for p in parts[1:])
        if not hasattr(get_system_health, '_prev'):
            get_system_health._prev = (idle, total)
        pi, pt = get_system_health._prev
        di, dt = idle - pi, total - pt
        h["cpu_pct"] = round(100 * (1 - di / max(dt, 1)), 1)
        get_system_health._prev = (idle, total)
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
        total_gb = (st.f_blocks * st.f_frsize) / (1024**3)
        free_gb = (st.f_bfree * st.f_frsize) / (1024**3)
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
    h["json_age"] = round(_file_age(OUTPUT_FILE), 1)
    h["stream_age"] = round(_file_age(STREAM_FILE), 1)

    h["parser_ts"] = time.time()

    return h


# ═══ OUI VENDOR DATABASE ═══
# Top ~350 prefixes covering the vast majority of consumer/enterprise devices
OUI = {
    "00:03:93":"Apple","00:05:02":"Apple","00:0A:95":"Apple","00:0D:93":"Apple",
    "00:10:FA":"Apple","00:11:24":"Apple","00:14:51":"Apple","00:16:CB":"Apple",
    "00:17:F2":"Apple","00:19:E3":"Apple","00:1B:63":"Apple","00:1C:B3":"Apple",
    "00:1D:4F":"Apple","00:1E:52":"Apple","00:1E:C2":"Apple","00:1F:5B":"Apple",
    "00:1F:F3":"Apple","00:21:E9":"Apple","00:22:41":"Apple","00:23:12":"Apple",
    "00:23:32":"Apple","00:23:6C":"Apple","00:23:DF":"Apple","00:24:36":"Apple",
    "00:25:00":"Apple","00:25:4B":"Apple","00:25:BC":"Apple","00:26:08":"Apple",
    "00:26:4A":"Apple","00:26:B0":"Apple","00:26:BB":"Apple","00:3E:E1":"Apple",
    "00:50:E4":"Apple","00:56:CD":"Apple","00:61:71":"Apple","00:6D:52":"Apple",
    "00:88:65":"Apple","00:B3:62":"Apple","00:C6:10":"Apple","00:CD:FE":"Apple",
    "00:DB:70":"Apple","00:F4:B9":"Apple","00:F7:6F":"Apple","04:0C:CE":"Apple",
    "04:15:52":"Apple","04:1B:BA":"Apple","04:26:65":"Apple","04:48:9A":"Apple",
    "04:4B:ED":"Apple","04:52:F3":"Apple","04:54:53":"Apple","04:69:F8":"Apple",
    "04:D3:CF":"Apple","04:DB:56":"Apple","04:E5:36":"Apple","04:F1:3E":"Apple",
    "04:F7:E4":"Apple","08:00:07":"Apple","08:66:98":"Apple","08:6D:41":"Apple",
    "0C:4D:E9":"Apple","0C:74:C2":"Apple","0C:77:1A":"Apple","0C:BC:9F":"Apple",
    "10:1C:0C":"Apple","10:40:F3":"Apple","10:41:7F":"Apple","10:93:E9":"Apple",
    "10:9A:DD":"Apple","10:DD:B1":"Apple","14:10:9F":"Apple","14:20:5E":"Apple",
    "14:5A:05":"Apple","14:8F:C6":"Apple","14:99:E2":"Apple","18:20:32":"Apple",
    "18:34:51":"Apple","18:65:90":"Apple","18:81:0E":"Apple","18:9E:FC":"Apple",
    "18:AF:61":"Apple","18:E7:F4":"Apple","18:EE:69":"Apple","18:F6:43":"Apple",
    "1C:1A:C0":"Apple","1C:36:BB":"Apple","1C:5C:F2":"Apple","1C:91:48":"Apple",
    "1C:AB:A7":"Apple","20:3C:AE":"Apple","20:78:F0":"Apple","20:7D:74":"Apple",
    "20:A2:E4":"Apple","20:AB:37":"Apple","20:C9:D0":"Apple","24:24:0E":"Apple",
    "24:5B:A7":"Apple","24:A0:74":"Apple","24:A2:E1":"Apple","24:E5:0F":"Apple",
    "24:F0:94":"Apple","28:0B:5C":"Apple","28:37:37":"Apple","28:6A:B8":"Apple",
    "28:6A:BA":"Apple","28:A0:2B":"Apple","28:CF:DA":"Apple","28:CF:E9":"Apple",
    "28:E0:2C":"Apple","28:E1:4C":"Apple","28:E7:CF":"Apple","28:ED:E0":"Apple",
    "28:F0:76":"Apple","2C:1F:23":"Apple","2C:33:61":"Apple","2C:B4:3A":"Apple",
    "2C:BE:08":"Apple","30:10:E4":"Apple","30:35:AD":"Apple","30:63:6B":"Apple",
    "30:90:AB":"Apple","30:D9:D9":"Apple","34:08:BC":"Apple","34:12:98":"Apple",
    "34:36:3B":"Apple","34:51:C9":"Apple","34:C0:59":"Apple","34:E2:FD":"Apple",
    "38:0F:4A":"Apple","38:48:4C":"Apple","38:66:F0":"Apple","38:71:DE":"Apple",
    "38:B5:4D":"Apple","38:C9:86":"Apple","38:CA:DA":"Apple","3C:07:54":"Apple",
    "3C:15:C2":"Apple","3C:2E:F9":"Apple","3C:AB:8E":"Apple","3C:D0:F8":"Apple",
    "40:30:04":"Apple","40:33:1A":"Apple","40:4D:7F":"Apple","40:6C:8F":"Apple",
    "40:A6:D9":"Apple","40:B3:95":"Apple","40:BC:60":"Apple","40:D3:2D":"Apple",
    "44:00:10":"Apple","44:2A:60":"Apple","44:D8:84":"Apple","48:3B:38":"Apple",
    "48:60:BC":"Apple","48:74:6E":"Apple","48:A9:1C":"Apple","48:BF:6B":"Apple",
    "48:D7:05":"Apple","4C:32:75":"Apple","4C:57:CA":"Apple","4C:74:BF":"Apple",
    "4C:8D:79":"Apple","4C:B1:99":"Apple","50:32:37":"Apple","50:7A:55":"Apple",
    "50:82:D5":"Apple","50:BC:96":"Apple","50:EA:D6":"Apple","54:26:96":"Apple",
    "54:33:CB":"Apple","54:4E:90":"Apple","54:72:4F":"Apple","54:99:63":"Apple",
    "54:AE:27":"Apple","54:BD:79":"Apple","54:E4:3A":"Apple","54:EA:A8":"Apple",
    "58:1F:AA":"Apple","58:40:4E":"Apple","58:55:CA":"Apple","58:B0:35":"Apple",
    "5C:59:48":"Apple","5C:8D:4E":"Apple","5C:95:AE":"Apple","5C:96:9D":"Apple",
    "5C:F7:E6":"Apple","60:03:08":"Apple","60:33:4B":"Apple","60:69:44":"Apple",
    "60:8C:4A":"Apple","60:A3:7D":"Apple","60:C5:47":"Apple","60:D9:C7":"Apple",
    "60:F8:1D":"Apple","60:FA:CD":"Apple","60:FE:C5":"Apple","64:20:0C":"Apple",
    "64:4B:F0":"Apple","64:70:33":"Apple","64:76:BA":"Apple","64:9A:BE":"Apple",
    "64:A3:CB":"Apple","64:B0:A6":"Apple","64:E6:82":"Apple","68:09:27":"Apple",
    "68:5B:35":"Apple","68:96:7B":"Apple","68:A8:6D":"Apple","68:AB:1E":"Apple",
    "68:AE:20":"Apple","68:D9:3C":"Apple","68:DB:CA":"Apple","68:FE:F7":"Apple",
    "6C:19:C0":"Apple","6C:3E:6D":"Apple","6C:40:08":"Apple","6C:4D:73":"Apple",
    "6C:70:9F":"Apple","6C:72:E7":"Apple","6C:94:F8":"Apple","6C:96:CF":"Apple",
    "6C:AB:31":"Apple","70:3E:AC":"Apple","70:48:0F":"Apple","70:56:81":"Apple",
    "70:73:CB":"Apple","70:81:EB":"Apple","70:CD:60":"Apple","70:DE:E2":"Apple",
    "70:EC:E4":"Apple","70:EF:00":"Apple","74:1B:B2":"Apple","74:8D:08":"Apple",
    "78:31:C1":"Apple","78:67:D7":"Apple","78:7E:61":"Apple","78:88:6D":"Apple",
    "78:9F:70":"Apple","78:A3:E4":"Apple","78:CA:39":"Apple","78:D7:5F":"Apple",
    "78:FD:94":"Apple","7C:01:0A":"Apple","7C:04:D0":"Apple","7C:11:BE":"Apple",
    "7C:6D:62":"Apple","7C:9A:1D":"Apple","7C:B7:33":"Apple","7C:D1:C3":"Apple",
    "7C:FA:DF":"Apple","80:00:6E":"Apple","80:49:71":"Apple","80:4A:F2":"Apple",
    "80:82:23":"Apple","80:92:9F":"Apple","80:B0:3D":"Apple","80:BE:05":"Apple",
    "80:E6:50":"Apple","80:EA:96":"Apple","80:ED:2C":"Apple","84:29:99":"Apple",
    "84:38:35":"Apple","84:78:8B":"Apple","84:85:06":"Apple","84:89:AD":"Apple",
    "84:8E:0C":"Apple","84:A1:34":"Apple","84:B1:53":"Apple","84:FC:AC":"Apple",
    "84:FC:FE":"Apple","88:1F:A1":"Apple","88:53:95":"Apple","88:63:DF":"Apple",
    "88:66:A5":"Apple","88:6B:6E":"Apple","88:AE:07":"Apple","88:C6:63":"Apple",
    "88:CB:87":"Apple","88:E8:7F":"Apple","8C:00:6D":"Apple","8C:29:37":"Apple",
    "8C:2D:AA":"Apple","8C:58:77":"Apple","8C:7B:9D":"Apple","8C:85:90":"Apple",
    "8C:FA:BA":"Apple","90:3C:92":"Apple","90:72:40":"Apple","90:84:0D":"Apple",
    "90:8D:6C":"Apple","90:B0:ED":"Apple","90:B2:1F":"Apple","90:B9:31":"Apple",
    "90:FD:61":"Apple","94:E9:6A":"Apple","94:F6:A3":"Apple","98:01:A7":"Apple",
    "98:03:D8":"Apple","98:10:E8":"Apple","98:46:0A":"Apple","98:5A:EB":"Apple",
    "98:B8:E3":"Apple","98:D6:BB":"Apple","98:E0:D9":"Apple","98:F0:AB":"Apple",
    "98:FE:94":"Apple","9C:04:EB":"Apple","9C:20:7B":"Apple","9C:35:EB":"Apple",
    "9C:84:BF":"Apple","9C:8B:A0":"Apple","9C:F3:87":"Apple","A0:18:28":"Apple",
    "A0:3B:E3":"Apple","A0:4E:A7":"Apple","A0:56:F3":"Apple","A0:99:9B":"Apple",
    "A0:D7:95":"Apple","A0:ED:CD":"Apple","A4:5E:60":"Apple","A4:67:06":"Apple",
    "A4:B1:97":"Apple","A4:C3:61":"Apple","A4:D1:8C":"Apple","A4:D1:D2":"Apple",
    "A4:E9:75":"Apple","A8:20:66":"Apple","A8:5B:78":"Apple","A8:5C:2C":"Apple",
    "A8:66:7F":"Apple","A8:86:DD":"Apple","A8:88:08":"Apple","A8:8E:24":"Apple",
    "A8:96:8A":"Apple","A8:BB:CF":"Apple","A8:FA:D8":"Apple","AC:29:3A":"Apple",
    "AC:3C:0B":"Apple","AC:61:EA":"Apple","AC:7F:3E":"Apple","AC:87:A3":"Apple",
    "AC:BC:32":"Apple","AC:CF:5C":"Apple","AC:E4:B5":"Apple","AC:FD:EC":"Apple",
    "B0:19:C6":"Apple","B0:34:95":"Apple","B0:48:1A":"Apple","B0:65:BD":"Apple",
    "B0:70:2D":"Apple","B0:9F:BA":"Apple","B0:CA:68":"Apple","B4:18:D1":"Apple",
    "B4:8B:19":"Apple","B4:9C:DF":"Apple","B4:F0:AB":"Apple","B4:F6:1C":"Apple",
    "B8:09:8A":"Apple","B8:17:C2":"Apple","B8:41:A4":"Apple","B8:44:D9":"Apple",
    "B8:53:AC":"Apple","B8:63:4D":"Apple","B8:78:2E":"Apple","B8:7B:D4":"Apple",
    "B8:8D:12":"Apple","B8:C1:11":"Apple","B8:C7:5D":"Apple","B8:E8:56":"Apple",
    "B8:F6:B1":"Apple","B8:FF:61":"Apple","BC:3B:AF":"Apple","BC:52:B7":"Apple",
    "BC:54:36":"Apple","BC:67:78":"Apple","BC:6C:21":"Apple","BC:92:6B":"Apple",
    "BC:A9:20":"Apple","BC:EC:5D":"Apple","BC:FE:D9":"Apple","C0:1A:DA":"Apple",
    "C0:63:94":"Apple","C0:84:7A":"Apple","C0:9F:42":"Apple","C0:A5:3E":"Apple",
    "C0:B6:58":"Apple","C0:CC:F8":"Apple","C0:D0:12":"Apple","C0:F2:FB":"Apple",
    "C4:2C:03":"Apple","C4:B3:01":"Apple","C8:1E:E7":"Apple","C8:2A:14":"Apple",
    "C8:33:4B":"Apple","C8:69:CD":"Apple","C8:85:50":"Apple","C8:B5:B7":"Apple",
    "C8:D0:83":"Apple","CC:08:8D":"Apple","CC:20:E8":"Apple","CC:25:EF":"Apple",
    "CC:29:F5":"Apple","CC:44:63":"Apple","CC:78:5F":"Apple","D0:03:4B":"Apple",
    "D0:25:98":"Apple","D0:33:11":"Apple","D0:4F:7E":"Apple","D0:81:7A":"Apple",
    "D4:61:9D":"Apple","D4:9A:20":"Apple","D4:F4:6F":"Apple","D8:00:4D":"Apple",
    "D8:1D:72":"Apple","D8:30:62":"Apple","D8:8F:76":"Apple","D8:96:95":"Apple",
    "D8:9E:3F":"Apple","D8:A2:5E":"Apple","D8:BB:2C":"Apple","D8:CF:9C":"Apple",
    "DC:08:56":"Apple","DC:2B:2A":"Apple","DC:2B:61":"Apple","DC:37:14":"Apple",
    "DC:56:E7":"Apple","DC:86:D8":"Apple","DC:A4:CA":"Apple","DC:D3:A2":"Apple",
    "E0:33:8E":"Apple","E0:5F:45":"Apple","E0:66:78":"Apple","E0:AC:CB":"Apple",
    "E0:B5:2D":"Apple","E0:B9:BA":"Apple","E0:C7:67":"Apple","E0:C9:7A":"Apple",
    "E0:F5:C6":"Apple","E4:25:E7":"Apple","E4:8B:7F":"Apple","E4:9A:DC":"Apple",
    "E4:C6:3D":"Apple","E4:CE:8F":"Apple","E4:E0:A6":"Apple","E8:04:0B":"Apple",
    "E8:06:88":"Apple","E8:80:2E":"Apple","E8:8D:28":"Apple","EC:35:86":"Apple",
    "EC:85:2F":"Apple","F0:18:98":"Apple","F0:24:75":"Apple","F0:72:EA":"Apple",
    "F0:99:BF":"Apple","F0:B0:E7":"Apple","F0:C1:F1":"Apple","F0:CB:A1":"Apple",
    "F0:D1:A9":"Apple","F0:DB:E2":"Apple","F0:DC:E2":"Apple","F4:0F:24":"Apple",
    "F4:1B:A1":"Apple","F4:31:C3":"Apple","F4:37:B7":"Apple","F8:1E:DF":"Apple",
    "FC:25:3F":"Apple","FC:D8:48":"Apple","FC:E9:98":"Apple","FC:FC:48":"Apple",
    # Samsung
    "00:07:AB":"Samsung","00:12:47":"Samsung","00:12:FB":"Samsung","00:13:77":"Samsung",
    "00:15:99":"Samsung","00:16:32":"Samsung","00:16:6B":"Samsung","00:16:6C":"Samsung",
    "00:17:C9":"Samsung","00:17:D5":"Samsung","00:18:AF":"Samsung","00:1A:8A":"Samsung",
    "00:1B:98":"Samsung","00:1C:43":"Samsung","00:1D:25":"Samsung","00:1D:F6":"Samsung",
    "00:1E:E1":"Samsung","00:1E:E2":"Samsung","00:21:19":"Samsung","00:21:D1":"Samsung",
    "00:21:D2":"Samsung","00:23:39":"Samsung","00:23:3A":"Samsung","00:23:99":"Samsung",
    "00:23:D6":"Samsung","00:23:D7":"Samsung","00:24:54":"Samsung","00:24:90":"Samsung",
    "00:24:91":"Samsung","00:25:66":"Samsung","00:25:67":"Samsung","00:26:37":"Samsung",
    "08:D4:6A":"Samsung","0C:DF:A4":"Samsung","10:1D:C0":"Samsung","14:49:E0":"Samsung",
    "14:89:FD":"Samsung","18:3A:2D":"Samsung","18:67:B0":"Samsung","1C:5A:3E":"Samsung",
    "1C:66:AA":"Samsung","20:13:E0":"Samsung","20:6E:9C":"Samsung","24:4B:03":"Samsung",
    "28:27:BF":"Samsung","28:CC:01":"Samsung","2C:AE:2B":"Samsung","30:CB:F8":"Samsung",
    "34:14:5F":"Samsung","34:23:BA":"Samsung","38:01:97":"Samsung","38:0A:94":"Samsung",
    "38:2D:D1":"Samsung","3C:5A:37":"Samsung","3C:62:00":"Samsung","40:0E:85":"Samsung",
    "44:6D:6C":"Samsung","44:F4:59":"Samsung","48:13:7E":"Samsung","4C:3C:16":"Samsung",
    "50:01:BB":"Samsung","50:A4:C8":"Samsung","50:B7:C3":"Samsung","50:CC:F8":"Samsung",
    "50:F5:20":"Samsung","54:40:AD":"Samsung","54:88:0E":"Samsung","54:92:BE":"Samsung",
    "58:C3:8B":"Samsung","5C:0A:5B":"Samsung","5C:3C:27":"Samsung","60:6B:BD":"Samsung",
    "60:A1:0A":"Samsung","64:77:91":"Samsung","68:27:37":"Samsung","6C:2F:2C":"Samsung",
    "6C:F3:73":"Samsung","70:F9:27":"Samsung","74:45:CE":"Samsung","78:1F:DB":"Samsung",
    "78:47:1D":"Samsung","78:52:1A":"Samsung","78:AB:BB":"Samsung","78:BD:BC":"Samsung",
    "80:18:A7":"Samsung","80:65:6D":"Samsung","84:25:DB":"Samsung","84:38:38":"Samsung",
    "84:55:A5":"Samsung","84:B5:41":"Samsung","88:32:9B":"Samsung","88:AD:D2":"Samsung",
    "8C:77:12":"Samsung","8C:F5:A3":"Samsung","90:18:7C":"Samsung","90:F1:AA":"Samsung",
    "94:01:C2":"Samsung","94:35:0A":"Samsung","94:51:03":"Samsung","94:76:B7":"Samsung",
    "94:B8:6D":"Samsung","98:0C:82":"Samsung","98:52:B1":"Samsung","9C:02:98":"Samsung",
    "9C:3A:AF":"Samsung","A0:07:98":"Samsung","A0:82:1F":"Samsung","A4:08:EA":"Samsung",
    "A8:06:00":"Samsung","A8:F2:74":"Samsung","AC:36:13":"Samsung","AC:5F:3E":"Samsung",
    "B0:47:BF":"Samsung","B0:72:BF":"Samsung","B0:EC:71":"Samsung","B4:07:F9":"Samsung",
    "B4:3A:28":"Samsung","B4:79:A7":"Samsung","B8:5A:73":"Samsung","B8:D9:CE":"Samsung",
    "BC:14:EF":"Samsung","BC:44:86":"Samsung","BC:72:B1":"Samsung","BC:8C:CD":"Samsung",
    "C0:97:27":"Samsung","C4:42:02":"Samsung","C4:57:6E":"Samsung","C4:73:1E":"Samsung",
    "C8:14:51":"Samsung","C8:BA:94":"Samsung","CC:07:AB":"Samsung","CC:3A:61":"Samsung",
    "D0:22:BE":"Samsung","D0:66:7B":"Samsung","D0:87:E2":"Samsung","D4:88:90":"Samsung",
    "D8:57:EF":"Samsung","D8:90:E8":"Samsung","D8:C4:E9":"Samsung","DC:71:37":"Samsung",
    "E4:12:1D":"Samsung","E4:58:B8":"Samsung","E4:7C:F9":"Samsung","E4:E0:C5":"Samsung",
    "E8:3A:12":"Samsung","E8:50:8B":"Samsung","EC:1F:72":"Samsung","EC:9B:F3":"Samsung",
    "F0:25:B7":"Samsung","F0:5B:7B":"Samsung","F0:6B:CA":"Samsung","F0:E7:7E":"Samsung",
    "F4:09:D8":"Samsung","F4:42:8F":"Samsung","F4:7B:5E":"Samsung","F4:7D:EF":"Samsung",
    "F8:04:2E":"Samsung","F8:3F:51":"Samsung","F8:D0:BD":"Samsung","FC:91:5D":"Samsung",
    "FC:A1:3E":"Samsung","FC:F1:36":"Samsung",
    # Google/Pixel
    "3C:5A:B4":"Google","54:60:09":"Google","F4:F5:D8":"Google","F4:F5:E8":"Google",
    "A4:77:33":"Google","30:FD:38":"Google","94:EB:2C":"Google","F8:8F:CA":"Google",
    "48:D6:D5":"Google","6C:AD:F8":"Google",
    # Intel
    "00:02:B3":"Intel","00:03:47":"Intel","00:04:23":"Intel","00:07:E9":"Intel",
    "00:0C:F1":"Intel","00:0E:0C":"Intel","00:0E:35":"Intel","00:11:11":"Intel",
    "00:12:F0":"Intel","00:13:02":"Intel","00:13:20":"Intel","00:13:CE":"Intel",
    "00:13:E8":"Intel","00:15:00":"Intel","00:15:17":"Intel","00:16:6F":"Intel",
    "00:16:76":"Intel","00:16:EA":"Intel","00:16:EB":"Intel","00:18:DE":"Intel",
    "00:19:D1":"Intel","00:19:D2":"Intel","00:1B:21":"Intel","00:1B:77":"Intel",
    "00:1C:BF":"Intel","00:1C:C0":"Intel","00:1D:E0":"Intel","00:1D:E1":"Intel",
    "00:1E:64":"Intel","00:1E:65":"Intel","00:1F:3B":"Intel","00:1F:3C":"Intel",
    "00:21:5C":"Intel","00:21:5D":"Intel","00:21:6A":"Intel","00:21:6B":"Intel",
    "00:22:43":"Intel","00:22:FA":"Intel","00:22:FB":"Intel","00:23:14":"Intel",
    "00:23:15":"Intel","00:24:D6":"Intel","00:24:D7":"Intel","00:27:10":"Intel",
    "34:02:86":"Intel","3C:A9:F4":"Intel","40:A6:B8":"Intel","4C:34:88":"Intel",
    "5C:51:4F":"Intel","5C:E0:C5":"Intel","68:17:29":"Intel","78:92:9C":"Intel",
    "7C:5C:F8":"Intel","80:86:F2":"Intel","84:3A:4B":"Intel","8C:F5:A3":"Intel",
    "A0:C5:89":"Intel","A4:34:D9":"Intel","B4:6B:FC":"Intel","B8:08:CF":"Intel",
    "C8:FF:28":"Intel","D4:3B:04":"Intel","D8:FC:93":"Intel","DC:53:60":"Intel",
    "F4:8C:50":"Intel","F8:16:54":"Intel","F8:63:3F":"Intel",
    # Dell
    "00:06:5B":"Dell","00:08:74":"Dell","00:0B:DB":"Dell","00:0D:56":"Dell",
    "00:0F:1F":"Dell","00:11:43":"Dell","00:12:3F":"Dell","00:13:72":"Dell",
    "00:14:22":"Dell","00:15:C5":"Dell","00:18:8B":"Dell","00:19:B9":"Dell",
    "00:1A:A0":"Dell","00:1C:23":"Dell","00:1D:09":"Dell","00:1E:4F":"Dell",
    "00:1E:C9":"Dell","00:21:70":"Dell","00:21:9B":"Dell","00:22:19":"Dell",
    "00:23:AE":"Dell","00:24:E8":"Dell","00:25:64":"Dell","00:26:B9":"Dell",
    "14:18:77":"Dell","14:B3:1F":"Dell","18:03:73":"Dell","18:66:DA":"Dell",
    "18:A9:9B":"Dell","18:DB:F2":"Dell","1C:40:24":"Dell","24:B6:FD":"Dell",
    "28:F1:0E":"Dell","34:17:EB":"Dell","34:E6:D7":"Dell","3C:2C:30":"Dell",
    "44:A8:42":"Dell","48:4D:7E":"Dell","50:9A:4C":"Dell","54:9F:35":"Dell",
    "5C:26:0A":"Dell","64:00:6A":"Dell","74:86:7A":"Dell","74:E6:E2":"Dell",
    "78:2B:CB":"Dell","80:18:44":"Dell","84:7B:EB":"Dell","84:8F:69":"Dell",
    "90:B1:1C":"Dell","98:40:BB":"Dell","98:90:96":"Dell","A4:1F:72":"Dell",
    "A4:BA:DB":"Dell","B0:83:FE":"Dell","B4:E1:0F":"Dell","B8:2A:72":"Dell",
    "B8:AC:6F":"Dell","B8:CA:3A":"Dell","BC:30:5B":"Dell","C8:1F:66":"Dell",
    "D0:43:1E":"Dell","D0:67:E5":"Dell","D4:81:D7":"Dell","D4:BE:D9":"Dell",
    "E0:DB:55":"Dell","E4:43:4B":"Dell","EC:F4:BB":"Dell","F0:1F:AF":"Dell",
    "F4:8E:38":"Dell","F8:B1:56":"Dell","F8:BC:12":"Dell","F8:CA:B8":"Dell",
    # HP / Hewlett-Packard
    "00:01:E6":"HP","00:02:A5":"HP","00:04:EA":"HP","00:08:02":"HP",
    "00:0A:57":"HP","00:0B:CD":"HP","00:0D:9D":"HP","00:0E:7F":"HP",
    "00:0F:20":"HP","00:0F:61":"HP","00:10:83":"HP","00:10:E3":"HP",
    "00:11:0A":"HP","00:11:85":"HP","00:12:79":"HP","00:13:21":"HP",
    "00:14:38":"HP","00:14:C2":"HP","00:15:60":"HP","00:16:35":"HP",
    "00:17:08":"HP","00:17:A4":"HP","00:18:FE":"HP","00:19:BB":"HP",
    "00:1A:4B":"HP","00:1B:78":"HP","00:1C:C4":"HP","00:1E:0B":"HP",
    "00:1F:29":"HP","00:21:5A":"HP","00:22:64":"HP","00:23:7D":"HP",
    "00:24:81":"HP","00:25:B3":"HP","00:26:55":"HP","00:30:C1":"HP",
    "00:50:8B":"HP","08:00:09":"HP","10:00:5A":"HP","10:1F:74":"HP",
    "10:60:4B":"HP","14:02:EC":"HP","14:58:D0":"HP","18:A9:05":"HP",
    "1C:C1:DE":"HP","28:80:23":"HP","2C:23:3A":"HP","2C:27:D7":"HP",
    "2C:41:38":"HP","2C:44:FD":"HP","2C:59:E5":"HP","2C:76:8A":"HP",
    "30:8D:99":"HP","30:E1:71":"HP","34:64:A9":"HP","38:63:BB":"HP",
    "38:EA:A7":"HP","3C:4A:92":"HP","3C:52:82":"HP","3C:D9:2B":"HP",
    "40:B0:34":"HP","40:B9:3C":"HP","44:31:92":"HP","44:48:C1":"HP",
    "48:0F:CF":"HP","48:DF:37":"HP","4C:39:09":"HP","50:65:F3":"HP",
    "58:20:B1":"HP","5C:B9:01":"HP","64:31:50":"HP","68:B5:99":"HP",
    "6C:3B:E5":"HP","6C:C2:17":"HP","70:10:6F":"HP","74:46:A0":"HP",
    "78:AC:C0":"HP","7C:11:CB":"HP","80:C1:6E":"HP","84:34:97":"HP",
    "8C:DC:D4":"HP","94:57:A5":"HP","98:E7:F4":"HP","9C:8E:99":"HP",
    "9C:B6:54":"HP","A0:1D:48":"HP","A0:2B:B8":"HP","A0:48:1C":"HP",
    "A0:D3:C1":"HP","A4:5D:36":"HP","A8:BD:27":"HP","AC:16:2D":"HP",
    "B0:5A:DA":"HP","B4:39:D6":"HP","B4:B5:2F":"HP","B8:AF:67":"HP",
    "C0:91:34":"HP","C4:34:6B":"HP","C8:B5:AD":"HP","CC:3E:5F":"HP",
    "D0:7E:28":"HP","D4:C9:EF":"HP","D8:9E:F3":"HP","D8:D3:85":"HP",
    "DC:4A:3E":"HP","E0:07:1B":"HP","E4:11:5B":"HP","E8:F7:24":"HP",
    "EC:8E:B5":"HP","F0:92:1C":"HP","F4:03:43":"HP","F4:CE:46":"HP",
    "FC:15:B4":"HP",
    # Lenovo
    "00:06:1B":"Lenovo","00:09:2D":"Lenovo","00:12:FE":"Lenovo",
    "28:D2:44":"Lenovo","34:02:86":"Lenovo","50:7B:9D":"Lenovo",
    "54:E1:AD":"Lenovo","70:77:81":"Lenovo","74:E5:0B":"Lenovo",
    "80:FA:5B":"Lenovo","8C:16:45":"Lenovo","98:FA:9B":"Lenovo",
    "C8:5B:76":"Lenovo","D8:D0:90":"Lenovo","E8:2A:44":"Lenovo",
    # Microsoft
    "00:0D:3A":"Microsoft","00:12:5A":"Microsoft","00:15:5D":"Microsoft",
    "00:17:FA":"Microsoft","00:1D:D8":"Microsoft","00:22:48":"Microsoft",
    "00:25:AE":"Microsoft","28:18:78":"Microsoft","30:59:B7":"Microsoft",
    "48:50:73":"Microsoft","58:82:A8":"Microsoft","60:45:BD":"Microsoft",
    "7C:1E:52":"Microsoft","B4:AE:2B":"Microsoft","C4:9D:ED":"Microsoft",
    # ASUS
    "00:0C:6E":"ASUS","00:0E:A6":"ASUS","00:11:2F":"ASUS","00:13:D4":"ASUS",
    "00:15:F2":"ASUS","00:17:31":"ASUS","00:1A:92":"ASUS","00:1D:60":"ASUS",
    "00:1E:8C":"ASUS","00:1F:C6":"ASUS","00:22:15":"ASUS","00:23:54":"ASUS",
    "00:24:8C":"ASUS","00:26:18":"ASUS","08:60:6E":"ASUS","10:BF:48":"ASUS",
    "14:DA:E9":"ASUS","1C:87:2C":"ASUS","1C:B7:2C":"ASUS","20:CF:30":"ASUS",
    "2C:4D:54":"ASUS","2C:56:DC":"ASUS","2C:FD:A1":"ASUS","30:85:A9":"ASUS",
    "34:97:F6":"ASUS","38:D5:47":"ASUS","3C:DF:1E":"ASUS","40:16:7E":"ASUS",
    "44:D1:FA":"ASUS","48:5B:39":"ASUS","4C:ED:FB":"ASUS","50:46:5D":"ASUS",
    "54:04:A6":"ASUS","60:A4:4C":"ASUS","6C:B0:CE":"ASUS","74:D0:2B":"ASUS",
    "78:24:AF":"ASUS","88:D7:F6":"ASUS","90:E6:BA":"ASUS","AC:22:0B":"ASUS",
    "AC:9E:17":"ASUS","B0:6E:BF":"ASUS","BC:AE:C5":"ASUS","C8:60:00":"ASUS",
    "D4:5D:64":"ASUS","D8:50:E6":"ASUS","E0:3F:49":"ASUS","E0:CB:4E":"ASUS",
    "F4:6D:04":"ASUS","F8:32:E4":"ASUS",
    # Cisco
    "00:00:0C":"Cisco","00:01:42":"Cisco","00:01:43":"Cisco","00:01:63":"Cisco",
    "00:01:64":"Cisco","00:01:96":"Cisco","00:01:97":"Cisco","00:01:C7":"Cisco",
    "00:01:C9":"Cisco","00:02:16":"Cisco","00:02:17":"Cisco","00:02:3D":"Cisco",
    "00:02:4A":"Cisco","00:02:4B":"Cisco","00:02:7D":"Cisco","00:02:7E":"Cisco",
    "00:02:B9":"Cisco","00:02:BA":"Cisco","00:02:FC":"Cisco","00:02:FD":"Cisco",
    "00:03:31":"Cisco","00:03:32":"Cisco","00:03:6B":"Cisco","00:03:6C":"Cisco",
    "00:03:9F":"Cisco","00:03:A0":"Cisco","00:03:E3":"Cisco","00:03:E4":"Cisco",
    "00:03:FD":"Cisco","00:03:FE":"Cisco","00:04:27":"Cisco","00:04:28":"Cisco",
    "00:05:31":"Cisco","00:05:32":"Cisco","00:05:5E":"Cisco","00:05:5F":"Cisco",
    "00:05:73":"Cisco","00:05:74":"Cisco","00:05:9A":"Cisco","00:05:DC":"Cisco",
    "00:05:DD":"Cisco","00:06:28":"Cisco","00:06:29":"Cisco","00:06:2A":"Cisco",
    "00:06:52":"Cisco","00:06:53":"Cisco","00:06:7C":"Cisco","00:06:C1":"Cisco",
    "00:06:D6":"Cisco","00:06:D7":"Cisco","00:06:F6":"Cisco","00:07:0D":"Cisco",
    "00:07:0E":"Cisco","00:07:4F":"Cisco","00:07:50":"Cisco","00:07:7D":"Cisco",
    "00:07:85":"Cisco","00:07:B3":"Cisco","00:07:B4":"Cisco","00:07:EB":"Cisco",
    "00:07:EC":"Cisco",
    # Ubiquiti
    "00:27:22":"Ubiquiti","04:18:D6":"Ubiquiti","18:E8:29":"Ubiquiti",
    "24:5A:4C":"Ubiquiti","24:A4:3C":"Ubiquiti","44:D9:E7":"Ubiquiti",
    "68:72:51":"Ubiquiti","70:A7:41":"Ubiquiti","74:83:C2":"Ubiquiti",
    "78:8A:20":"Ubiquiti","80:2A:A8":"Ubiquiti","9C:05:D6":"Ubiquiti",
    "AC:8B:A9":"Ubiquiti","B4:FB:E4":"Ubiquiti","D0:21:F9":"Ubiquiti",
    "DC:9F:DB":"Ubiquiti","E0:63:DA":"Ubiquiti","F0:9F:C2":"Ubiquiti",
    "FC:EC:DA":"Ubiquiti",
    # Aruba
    "00:0B:86":"Aruba","00:1A:1E":"Aruba","00:24:6C":"Aruba",
    "04:BD:88":"Aruba","18:64:72":"Aruba","20:4C:03":"Aruba",
    "24:DE:C6":"Aruba","40:E3:D6":"Aruba","6C:F3:7F":"Aruba",
    "70:3A:0E":"Aruba","84:D4:7E":"Aruba","94:B4:0F":"Aruba",
    "9C:1C:12":"Aruba","9C:57:BC":"Aruba","A8:BD:27":"Aruba",
    "AC:A3:1E":"Aruba","B4:5D:50":"Aruba","D8:C7:C8":"Aruba",
    "F0:5C:19":"Aruba",
    # Netgear
    "00:09:5B":"Netgear","00:0F:B5":"Netgear","00:14:6C":"Netgear",
    "00:18:4D":"Netgear","00:1B:2F":"Netgear","00:1E:2A":"Netgear",
    "00:1F:33":"Netgear","00:22:3F":"Netgear","00:24:B2":"Netgear",
    "00:26:F2":"Netgear","04:A1:51":"Netgear","08:BD:43":"Netgear",
    "10:0C:6B":"Netgear","10:0D:7F":"Netgear","20:0C:C8":"Netgear",
    "20:E5:2A":"Netgear","28:C6:8E":"Netgear","2C:B0:5D":"Netgear",
    "30:46:9A":"Netgear","38:94:ED":"Netgear","44:94:FC":"Netgear",
    "4C:60:DE":"Netgear","6C:B0:CE":"Netgear","84:1B:5E":"Netgear",
    "8C:3B:AD":"Netgear","9C:3D:CF":"Netgear","A0:04:60":"Netgear",
    "A4:2B:8C":"Netgear","B0:39:56":"Netgear","B0:7F:B9":"Netgear",
    "C0:3F:0E":"Netgear","C4:04:15":"Netgear","C4:3D:C7":"Netgear",
    "CC:40:D0":"Netgear","DC:EF:09":"Netgear","E0:46:9A":"Netgear",
    "E0:91:F5":"Netgear","E4:F4:C6":"Netgear","F8:73:94":"Netgear",
    # TP-Link
    "00:23:CD":"TP-Link","00:27:19":"TP-Link","10:FE:ED":"TP-Link",
    "14:CC:20":"TP-Link","14:CF:92":"TP-Link","18:A6:F7":"TP-Link",
    "1C:3B:F3":"TP-Link","24:69:68":"TP-Link","30:B5:C2":"TP-Link",
    "34:60:F9":"TP-Link","38:83:45":"TP-Link","3C:84:6A":"TP-Link",
    "48:22:54":"TP-Link","50:3E:AA":"TP-Link","54:C8:0F":"TP-Link",
    "5C:A6:E6":"TP-Link","5C:E9:31":"TP-Link","60:32:B1":"TP-Link",
    "60:E3:27":"TP-Link","64:56:01":"TP-Link","64:70:02":"TP-Link",
    "6C:5A:B0":"TP-Link","74:DA:88":"TP-Link","78:44:76":"TP-Link",
    "78:8C:B5":"TP-Link","7C:8B:CA":"TP-Link","90:F6:52":"TP-Link",
    "98:DA:C4":"TP-Link","A0:F4:C8":"TP-Link","A4:2B:B0":"TP-Link",
    "AC:84:C6":"TP-Link","B0:4E:26":"TP-Link","B0:95:75":"TP-Link",
    "B0:BE:76":"TP-Link","B4:B0:24":"TP-Link","C0:25:E9":"TP-Link",
    "C0:4A:00":"TP-Link","C0:E3:FB":"TP-Link","CC:32:E5":"TP-Link",
    "D4:6E:0E":"TP-Link","D8:07:B6":"TP-Link","D8:47:32":"TP-Link",
    "E4:C3:2A":"TP-Link","EC:08:6B":"TP-Link","EC:17:2F":"TP-Link",
    "F0:A7:31":"TP-Link","F4:EC:38":"TP-Link","F8:1A:67":"TP-Link",
    # Amazon / Ring / Echo
    "00:FC:8B":"Amazon","0C:47:C9":"Amazon","10:CE:A9":"Amazon",
    "18:74:2E":"Amazon","24:4C:E3":"Amazon","34:D2:70":"Amazon",
    "38:F7:3D":"Amazon","40:A2:DB":"Amazon","44:65:0D":"Amazon",
    "4C:EF:C0":"Amazon","50:DC:E7":"Amazon","50:F5:DA":"Amazon",
    "68:37:E9":"Amazon","68:54:FD":"Amazon","6C:56:97":"Amazon",
    "74:75:48":"Amazon","74:C2:46":"Amazon","78:E1:03":"Amazon",
    "84:D6:D0":"Amazon","8C:49:62":"Amazon","A0:02:DC":"Amazon",
    "A4:08:01":"Amazon","AC:63:BE":"Amazon","B0:FC:0D":"Amazon",
    "B4:7C:9C":"Amazon","C8:2B:96":"Amazon","CC:9E:A2":"Amazon",
    "F0:27:2D":"Amazon","F0:D2:F1":"Amazon","F0:F0:A4":"Amazon",
    "FC:65:DE":"Amazon","FC:A1:83":"Amazon",
    # Espressif (ESP32/ESP8266 - IoT)
    "08:3A:F2":"Espressif","10:52:1C":"Espressif","18:FE:34":"Espressif",
    "24:0A:C4":"Espressif","24:6F:28":"Espressif","24:B2:DE":"Espressif",
    "2C:3A:E8":"Espressif","30:AE:A4":"Espressif","3C:61:05":"Espressif",
    "3C:71:BF":"Espressif","40:F5:20":"Espressif","48:3F:DA":"Espressif",
    "4C:11:AE":"Espressif","54:32:04":"Espressif","5C:CF:7F":"Espressif",
    "60:01:94":"Espressif","68:C6:3A":"Espressif","70:03:9F":"Espressif",
    "7C:9E:BD":"Espressif","80:7D:3A":"Espressif","84:0D:8E":"Espressif",
    "84:CC:A8":"Espressif","8C:AA:B5":"Espressif","90:38:0C":"Espressif",
    "94:B5:55":"Espressif","98:F4:AB":"Espressif","A0:20:A6":"Espressif",
    "A4:7B:9D":"Espressif","A4:CF:12":"Espressif","AC:67:B2":"Espressif",
    "B4:E6:2D":"Espressif","BC:DD:C2":"Espressif","C4:4F:33":"Espressif",
    "C4:5B:BE":"Espressif","C8:2B:96":"Espressif","CC:50:E3":"Espressif",
    "D8:A0:1D":"Espressif","D8:BF:C0":"Espressif","DC:4F:22":"Espressif",
    "E0:98:06":"Espressif","E8:DB:84":"Espressif","EC:FA:BC":"Espressif",
    "F0:08:D1":"Espressif","F4:CF:A2":"Espressif",
    # Sonos
    "00:0E:58":"Sonos","34:7E:5C":"Sonos","48:A6:B8":"Sonos",
    "54:2A:1B":"Sonos","5C:AA:FD":"Sonos","78:28:CA":"Sonos",
    "94:9F:3E":"Sonos","B8:E9:37":"Sonos",
    # Roku
    "00:0D:4B":"Roku","10:59:32":"Roku","20:EF:BD":"Roku",
    "2C:31:24":"Roku","3C:59:2D":"Roku","84:EA:ED":"Roku",
    "AC:3A:7A":"Roku","B0:A7:37":"Roku","B8:3E:59":"Roku",
    "C8:3A:6B":"Roku","CC:6D:A0":"Roku","D4:E2:2F":"Roku",
    "D8:31:34":"Roku","DC:3A:5E":"Roku",
    # Huawei / Honor
    "00:1E:10":"Huawei","00:25:9E":"Huawei","00:46:4B":"Huawei",
    "00:66:4B":"Huawei","00:9A:CD":"Huawei","00:E0:FC":"Huawei",
    "04:02:1F":"Huawei","04:25:C5":"Huawei","04:33:C2":"Huawei",
    "04:4F:4C":"Huawei","04:B0:E7":"Huawei","04:C0:6F":"Huawei",
    "04:F9:38":"Huawei","04:FE:8D":"Huawei","08:19:A6":"Huawei",
    "08:63:61":"Huawei","0C:37:DC":"Huawei","0C:45:BA":"Huawei",
    "0C:96:BF":"Huawei","10:1B:54":"Huawei","10:44:00":"Huawei",
    "10:47:80":"Huawei","10:C6:1F":"Huawei","14:30:04":"Huawei",
    "14:57:9F":"Huawei","14:A0:F8":"Huawei","14:B9:68":"Huawei",
    "20:0B:C7":"Huawei","20:A6:80":"Huawei","20:F1:7C":"Huawei",
    "24:09:95":"Huawei","24:4C:07":"Huawei","24:DB:AC":"Huawei",
    "28:3C:E4":"Huawei","28:6E:D4":"Huawei","2C:AB:00":"Huawei",
    "30:D1:7E":"Huawei","34:00:A3":"Huawei","34:29:12":"Huawei",
    "34:CD:BE":"Huawei","38:4C:4F":"Huawei","38:F8:89":"Huawei",
    "3C:47:11":"Huawei","3C:F8:08":"Huawei","40:4D:8E":"Huawei",
    "40:CB:A8":"Huawei","44:55:B1":"Huawei","48:00:31":"Huawei",
    "48:3C:0C":"Huawei","48:46:FB":"Huawei","48:AD:08":"Huawei",
    "4C:1F:CC":"Huawei","4C:8B:EF":"Huawei","4C:B1:6C":"Huawei",
    "50:A7:2B":"Huawei","54:A5:1B":"Huawei","58:2A:F7":"Huawei",
    "58:60:5F":"Huawei","5C:4C:A9":"Huawei","5C:7D:5E":"Huawei",
    "5C:B3:95":"Huawei","60:08:10":"Huawei","60:DE:44":"Huawei",
    "60:E7:01":"Huawei","64:16:F0":"Huawei","68:A0:F6":"Huawei",
    "70:19:2F":"Huawei","70:72:3C":"Huawei","70:8A:09":"Huawei",
    # Xiaomi
    "00:9E:C8":"Xiaomi","04:CF:8C":"Xiaomi","0C:1D:AF":"Xiaomi",
    "10:2A:B3":"Xiaomi","14:F6:5A":"Xiaomi","18:59:36":"Xiaomi",
    "1C:5A:6B":"Xiaomi","20:34:FB":"Xiaomi","28:6C:07":"Xiaomi",
    "28:E3:1F":"Xiaomi","34:80:B3":"Xiaomi","34:CE:00":"Xiaomi",
    "38:A4:ED":"Xiaomi","3C:BD:3E":"Xiaomi","40:31:3C":"Xiaomi",
    "44:23:7C":"Xiaomi","48:A4:72":"Xiaomi","4C:49:E3":"Xiaomi",
    "50:64:2B":"Xiaomi","54:48:E6":"Xiaomi","58:44:98":"Xiaomi",
    "5C:50:15":"Xiaomi","60:AB:67":"Xiaomi","64:09:80":"Xiaomi",
    "64:CC:2E":"Xiaomi","68:28:BA":"Xiaomi","6C:5A:B5":"Xiaomi",
    "74:23:44":"Xiaomi","74:51:BA":"Xiaomi","78:02:F8":"Xiaomi",
    "78:11:DC":"Xiaomi","7C:1D:D9":"Xiaomi","80:AD:16":"Xiaomi",
    "84:F3:EB":"Xiaomi","88:C3:97":"Xiaomi","8C:DE:F9":"Xiaomi",
    "98:FA:E3":"Xiaomi","9C:99:A0":"Xiaomi","A0:86:C6":"Xiaomi",
    "A4:77:33":"Xiaomi","AC:F7:F3":"Xiaomi","B0:E2:35":"Xiaomi",
    "C4:0B:CB":"Xiaomi","CC:B5:D1":"Xiaomi","D4:61:DA":"Xiaomi",
    "D8:CE:3A":"Xiaomi","E4:46:DA":"Xiaomi","F0:B4:29":"Xiaomi",
    "F4:F9:51":"Xiaomi","F8:A4:5F":"Xiaomi","FC:64:BA":"Xiaomi",
    # Motorola
    "00:04:56":"Motorola","00:08:0E":"Motorola","00:0A:28":"Motorola",
    "00:0C:E5":"Motorola","00:0E:5C":"Motorola","00:0F:9F":"Motorola",
    "00:11:1A":"Motorola","00:12:25":"Motorola","00:14:04":"Motorola",
    "00:14:9A":"Motorola","00:15:2F":"Motorola","00:15:9A":"Motorola",
    "00:17:00":"Motorola","00:18:A4":"Motorola","00:19:2C":"Motorola",
    "00:1A:66":"Motorola","00:1A:77":"Motorola","00:1C:12":"Motorola",
    "00:1C:FB":"Motorola","00:1D:BE":"Motorola","00:1E:5A":"Motorola",
    # LG
    "00:05:C9":"LG","00:0B:F0":"LG","00:0F:4B":"LG","00:1C:62":"LG",
    "00:1E:75":"LG","00:1F:6B":"LG","00:1F:E3":"LG","00:21:FB":"LG",
    "00:22:A9":"LG","00:24:83":"LG","00:25:E5":"LG","00:26:E2":"LG",
    "00:AA:70":"LG","08:08:C2":"LG","10:68:3F":"LG","14:C9:13":"LG",
    "20:3D:BD":"LG","30:B4:9E":"LG","34:FC:EF":"LG","40:B8:9A":"LG",
    "44:07:4F":"LG","50:55:27":"LG","58:A2:B5":"LG","64:89:9A":"LG",
    "6C:D6:8A":"LG","78:F8:82":"LG","88:07:4B":"LG","8C:3A:E3":"LG",
    "98:D6:F7":"LG","A0:39:F7":"LG","A8:16:B2":"LG","A8:92:2C":"LG",
    "B4:E6:2A":"LG","C0:F6:C2":"LG","CC:FA:00":"LG","D0:37:45":"LG",
    "E8:F2:E2":"LG","F8:0C:F3":"LG","FC:F1:52":"LG",
    # OnePlus
    "5C:24:2A":"OnePlus","64:A2:F9":"OnePlus","94:65:2D":"OnePlus",
    "C0:EE:40":"OnePlus",
    # Vizio
    "F8:8F:CA":"Vizio",
}


def distance(signal):
    if signal == -1:
        return "unknown"
    if signal > -50:
        return "close"
    elif signal > -70:
        return "medium"
    else:
        return "far"


def get_vendor(mac):
    prefix = mac.upper()[0:8]
    return OUI.get(prefix, "Unknown")


def classify_device(vendor, mac, probed_ssids):
    first_byte = int(mac.split(":")[0], 16)
    if first_byte & 0x02:
        return "random"

    v = vendor.lower()

    phones = ["apple", "samsung", "huawei", "xiaomi", "oneplus", "google",
              "motorola", "lg", "oppo", "vivo", "realme", "honor"]
    if any(p in v for p in phones):
        return "phone"

    laptops = ["dell", "lenovo", "hp", "hewlett", "intel", "microsoft",
               "asus", "acer", "razer", "msi"]
    if any(p in v for p in laptops):
        return "laptop"

    iot = ["amazon", "ring", "nest", "ecobee", "tuya", "espressif",
           "shenzhen", "tp-link", "sonos", "roku", "wyze", "vizio",
           "broadlink", "wemo", "philips hue"]
    if any(p in v for p in iot):
        return "iot"

    infra = ["cisco", "ubiquiti", "aruba", "netgear", "linksys",
             "meraki", "mikrotik", "ruckus", "cambium", "juniper"]
    if any(p in v for p in infra):
        return "infra"

    return "unknown"


def parse():
    global devices, networks

    try:
        with open(CSV_FILE, newline='', errors='ignore') as f:
            reader = csv.reader(f)
            section = None
            temp_networks = {}
            temp_devices = {}

            for row in reader:
                if len(row) < 2:
                    continue

                header = row[0].strip()

                if "BSSID" in header and "Station" not in header:
                    section = "ap"
                    continue
                elif "Station MAC" in header:
                    section = "station"
                    continue

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

                        temp_networks[bssid] = {
                            "bssid": bssid,
                            "essid": essid,
                            "channel": channel,
                            "privacy": privacy,
                            "cipher": cipher,
                            "auth": auth,
                            "power": power,
                        }
                    except (IndexError, ValueError):
                        continue

                elif section == "station":
                    try:
                        mac = row[0].strip().upper()
                        if not mac or len(mac) < 17:
                            continue

                        now = time.time()

                        first_seen_str = row[1].strip() if len(row) > 1 else ""
                        last_seen_str = row[2].strip() if len(row) > 2 else ""
                        try:
                            first_seen_ts = time.mktime(time.strptime(first_seen_str, "%Y-%m-%d %H:%M:%S")) if first_seen_str else now
                        except (ValueError, OverflowError):
                            first_seen_ts = now
                        try:
                            last_seen_ts = time.mktime(time.strptime(last_seen_str, "%Y-%m-%d %H:%M:%S")) if last_seen_str else now
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
                        prev_packets = prev.get("packets", 0)
                        packet_rate = max(0, packets - prev_packets)

                        age = now - last_seen_ts
                        is_active = age < 30

                        is_associated = bssid != "(NOT ASSOCIATED)" and len(bssid) == 17

                        essid = ""
                        channel = 0
                        encryption = ""
                        cipher_dev = ""
                        auth_dev = ""
                        if is_associated and bssid in temp_networks:
                            net = temp_networks[bssid]
                            essid = net["essid"]
                            channel = net["channel"]
                            encryption = net["privacy"]
                            cipher_dev = net["cipher"]
                            auth_dev = net["auth"]

                        temp_devices[mac] = {
                            "mac": mac,
                            "vendor": vendor,
                            "type": classify_device(vendor, mac, probed_list),
                            "signal": signal,
                            "distance": distance(signal),
                            "packets": packets,
                            "packet_rate": packet_rate,
                            "bssid": bssid if is_associated else "(not associated)",
                            "associated": is_associated,
                            "essid": essid,
                            "channel": channel,
                            "encryption": encryption,
                            "cipher": cipher_dev,
                            "auth": auth_dev,
                            "probed_ssids": probed_list,
                            "first_seen": first_seen_ts,
                            "last_seen": last_seen_ts,
                            "duration": int(last_seen_ts - first_seen_ts),
                            "active": is_active,
                        }
                    except (IndexError, ValueError):
                        continue

            networks = temp_networks
            for mac, dev in temp_devices.items():
                devices[mac] = dev

    except FileNotFoundError:
        print(f"[WARN] CSV file not found: {CSV_FILE}", file=sys.stderr)
    except Exception as e:
        print(f"[ERROR] Parse failed: {e}", file=sys.stderr)


def cleanup():
    now = time.time()
    for mac in list(devices.keys()):
        age = now - devices[mac]["last_seen"]
        if age > 30:
            devices[mac]["active"] = False
        if age > 300:
            del devices[mac]


def generate_events():
    global prev_ap_set, prev_device_set
    now_devs = set(d["mac"] for d in devices.values() if d["active"])
    now_aps = set(networks.keys())

    for mac in now_devs - prev_device_set:
        d = devices.get(mac, {})
        detail = f"{d.get('vendor','?')} | {d.get('signal',0)} dBm | {d.get('distance','?')}"
        db_log_event("device_join", mac, detail)
        if d.get("distance") == "close" and d.get("signal", -1) != -1:
            db_log_event("close_alert", mac, f"Strong signal: {d.get('signal')} dBm")

    for mac in prev_device_set - now_devs:
        d = devices.get(mac, {})
        db_log_event("device_leave", mac, d.get("vendor", ""))

    for bssid in now_aps - prev_ap_set:
        n = networks.get(bssid, {})
        db_log_event("new_ap", bssid, f"{n.get('essid','<hidden>')} ch{n.get('channel','?')}")

    for d in devices.values():
        if d["active"] and d.get("packet_rate", 0) > 50:
            db_log_event("burst", d["mac"], f"{d['packet_rate']} pkts/s")

    prev_device_set = now_devs
    prev_ap_set = now_aps


def get_rf_summary():
    chan_24 = {}
    chan_5 = {}
    enc_counts = {}
    hidden = 0
    strongest = []

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

        strongest.append({"bssid": n["bssid"], "essid": n.get("essid","<hidden>"),
                          "channel": ch, "power": n.get("power", -100),
                          "privacy": enc})

    strongest.sort(key=lambda x: x["power"], reverse=True)

    client_counts = {}
    for d in devices.values():
        b = d.get("bssid", "")
        if b and b != "(not associated)" and len(b) == 17:
            client_counts[b] = client_counts.get(b, 0) + 1

    ap_clients = []
    for n in networks.values():
        ap_clients.append({
            "bssid": n["bssid"], "essid": n.get("essid",""),
            "channel": n.get("channel",0), "privacy": n.get("privacy",""),
            "power": n.get("power",-100),
            "clients": client_counts.get(n["bssid"], 0)
        })
    ap_clients.sort(key=lambda x: x["clients"], reverse=True)

    total_24 = sum(chan_24.values())
    total_5 = sum(chan_5.values())
    busiest_ch = max(list(chan_24.items()) + list(chan_5.items()), key=lambda x: x[1]) if (chan_24 or chan_5) else (0, 0)

    return {
        "channels_24": chan_24,
        "channels_5": chan_5,
        "total_24": total_24,
        "total_5": total_5,
        "busiest_channel": busiest_ch[0],
        "busiest_count": busiest_ch[1],
        "encryption": enc_counts,
        "hidden_count": hidden,
        "strongest_aps": strongest[:10],
        "ap_by_clients": ap_clients[:15],
    }


def write_json():
    net_list = list(networks.values())
    dev_list = list(devices.values())

    data = {
        "timestamp": time.time(),
        "count": len(devices),
        "active_count": sum(1 for d in dev_list if d["active"]),
        "networks": net_list,
        "devices": dev_list,
        "registry": db_get_stats(),
        "system": get_system_health(),
        "rf": get_rf_summary(),
        "events": events[:50],
        "history": db_get_history(),
    }

    try:
        with open(OUTPUT_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[ERROR] Write failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    print("OVERWATCH Parser v3 \u2014 monitoring " + CSV_FILE)
    print(f"  OUI database: {len(OUI)} vendor prefixes loaded")
    print(f"  Registry DB: {REGISTRY_DB}")
    print(f"  Output: {OUTPUT_FILE}")

    while True:
        parse()
        cleanup()
        generate_events()
        active_devs = [d for d in devices.values() if d["active"]]
        if active_devs:
            db_remember(active_devs)
        write_json()
        time.sleep(1)
