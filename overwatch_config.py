import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("OVERWATCH_DATA_DIR", BASE_DIR)).expanduser()

CSV_FILE = os.environ.get("OVERWATCH_CSV_FILE", "/home/matthew/scan-01.csv")
REGISTRY_DB = os.environ.get("OVERWATCH_DB", str(DATA_DIR / "device_registry.db"))
OUTPUT_FILE = os.environ.get("OVERWATCH_OUTPUT_FILE", str(DATA_DIR / "devices.json"))
STREAM_FILE = os.environ.get("OVERWATCH_STREAM_FILE", str(DATA_DIR / "stream.m3u8"))
REPORTS_DIR = Path(os.environ.get("OVERWATCH_REPORT_DIR", str(BASE_DIR / "reports"))).expanduser()


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


EVENT_RETENTION_DAYS = _int_env("OVERWATCH_EVENT_RETENTION_DAYS", 30)
REPORT_LOOKBACK_HOURS = _int_env("OVERWATCH_REPORT_LOOKBACK_HOURS", 24)
REPORT_FULL_MAC = os.environ.get("OVERWATCH_REPORT_FULL_MAC", "").lower() in {"1", "true", "yes"}
