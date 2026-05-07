# Frankie Overwatch Guide

This repo is the real Overwatch WiFi monitor. Use it for passive WiFi presence and RF-environment review around Matthew's own monitoring station.

## Start Here

1. Check the repo status:
   ```bash
   git status --short
   ```
2. Confirm the scanner CSV path:
   ```bash
   echo $OVERWATCH_CSV_FILE
   ```
   Default: `/home/matthew/scan-01.csv`
3. Start the FastAPI server:
   ```bash
   uvicorn server:app --host 0.0.0.0 --port 8001
   ```
4. Open the dashboard:
   ```text
   http://<pi-ip>:8001/dashboard/dashboard.html
   ```

## Daily Review

Use this when Matthew asks what changed, what looked unusual, or whether Overwatch needs attention:

```bash
python3 overwatch_daily.py
```

Useful variants:

```bash
python3 overwatch_daily.py --hours 24 --json
python3 overwatch_daily.py --hours 72
python3 overwatch_daily.py --db /path/to/device_registry.db
```

Reports are written to `reports/` and are intentionally ignored by Git because they may contain sensitive local presence data. MAC addresses are redacted by default in reports. Only use `--full-mac` during a private local review.

## API Calls

- `GET /devices` - live payload for dashboard
- `GET /api/timeline?hours=24` - recent events
- `GET /api/timeline/{mac}?hours=24` - one device timeline
- `GET /api/daily-summary?hours=24` - daily summary JSON
- `WS /ws` - live updates

## Environment Variables

- `OVERWATCH_CSV_FILE` - airodump-ng CSV path
- `OVERWATCH_DB` - SQLite registry path
- `OVERWATCH_DATA_DIR` - default output directory for local data
- `OVERWATCH_OUTPUT_FILE` - legacy JSON output path
- `OVERWATCH_STREAM_FILE` - optional camera stream path
- `OVERWATCH_EVENT_RETENTION_DAYS` - event retention window, default 30
- `OVERWATCH_REPORT_DIR` - daily report output directory
- `OVERWATCH_REPORT_FULL_MAC=1` - disable MAC redaction in local reports

## Matthew-Facing Summary Shape

When reporting to Matthew, keep it action-oriented:

- Is the scanner healthy?
- What changed in the last day?
- Any close-signal or burst events?
- Any new APs or unusual volume?
- Which recurring devices are normal enough to label?
- What should be checked next?
