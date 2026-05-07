from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import json
import os
import time

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DEVICES_FILE = "/home/matthew/camera-dashboard/devices.json"


def load_devices():
    if not os.path.exists(DEVICES_FILE):
        return {"devices": [], "timestamp": 0}
    with open(DEVICES_FILE, "r") as f:
        return json.load(f)


@app.get("/")
def root():
    return {"status": "api running"}


@app.get("/devices")
def get_devices():
    return load_devices()


@app.get("/devices/active")
def get_active_devices():
    data = load_devices()
    active = [d for d in data["devices"] if d.get("packet_rate", 0) > 0]
    return {"devices": active, "count": len(active)}


@app.get("/system")
def system_status():
    try:
        csv_time = os.path.getmtime("/home/matthew/scan-01.csv")
    except:
        csv_time = 0

    try:
        json_time = os.path.getmtime(DEVICES_FILE)
    except:
        json_time = 0

    now = time.time()

    return {
        "csv_age": round(now - csv_time, 2),
        "json_age": round(now - json_time, 2),
        "status": "ok"
    }
