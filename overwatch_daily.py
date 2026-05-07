import argparse
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from overwatch_config import REGISTRY_DB, REPORT_FULL_MAC, REPORT_LOOKBACK_HOURS, REPORTS_DIR


def redact_mac(mac, full=False):
    if full or not mac or len(mac) < 8:
        return mac or ""
    parts = mac.upper().split(":")
    if len(parts) >= 3:
        return ":".join(parts[:3] + ["xx", "xx", "xx"])
    return mac[:8] + "..."


def _connect(db_path):
    path = Path(db_path).expanduser()
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _scalar(conn, sql, params=(), default=0):
    row = conn.execute(sql, params).fetchone()
    if not row:
        return default
    return row[0] if row[0] is not None else default


def _rows(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _event_counts(conn, start_ts, end_ts):
    rows = _rows(
        conn,
        """SELECT type, COUNT(*) AS count
           FROM events
           WHERE ts >= ? AND ts < ?
           GROUP BY type
           ORDER BY count DESC""",
        (start_ts, end_ts),
    )
    return {row["type"]: row["count"] for row in rows}


def _mac_safe_rows(rows, full_mac):
    safe = []
    for row in rows:
        item = dict(row)
        if "mac" in item:
            item["mac"] = redact_mac(item["mac"], full=full_mac)
        return_list = item.get("all_probes")
        if isinstance(return_list, str):
            item["all_probes"] = [p for p in return_list.split(",") if p][:5]
        safe.append(item)
    return safe


def build_daily_summary(db_path=REGISTRY_DB, hours=REPORT_LOOKBACK_HOURS, full_mac=REPORT_FULL_MAC, now=None):
    now = now or time.time()
    hours = float(hours)
    cutoff = now - hours * 3600
    prev_cutoff = cutoff - hours * 3600
    conn = _connect(db_path)
    generated_at = datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")

    if conn is None:
        return {
            "status": "missing_db",
            "generated_at": generated_at,
            "db_path": str(db_path),
            "hours": hours,
            "recommendations": [
                "Start the FastAPI server or monitor_live.py so device_registry.db is created.",
                "Set OVERWATCH_DB if the database lives outside this repo.",
            ],
        }

    with conn:
        events_now = _event_counts(conn, cutoff, now)
        events_prev = _event_counts(conn, prev_cutoff, cutoff)
        total_events = sum(events_now.values())
        previous_events = sum(events_prev.values())

        active_devices = _scalar(conn, "SELECT COUNT(*) FROM known_devices WHERE last_seen >= ?", (cutoff,))
        new_devices = _scalar(conn, "SELECT COUNT(*) FROM known_devices WHERE first_ever >= ?", (cutoff,))
        returning_devices = _scalar(
            conn,
            "SELECT COUNT(*) FROM known_devices WHERE visits > 1 AND last_seen >= ?",
            (cutoff,),
        )
        total_known = _scalar(conn, "SELECT COUNT(*) FROM known_devices")

        top_vendors = _rows(
            conn,
            """SELECT COALESCE(NULLIF(vendor, ''), 'Unknown') AS vendor, COUNT(*) AS count
               FROM known_devices
               WHERE last_seen >= ?
               GROUP BY COALESCE(NULLIF(vendor, ''), 'Unknown')
               ORDER BY count DESC
               LIMIT 10""",
            (cutoff,),
        )

        recurring = _mac_safe_rows(
            _rows(
                conn,
                """SELECT mac, vendor, type, alias, tag, visits, total_secs, best_signal, last_seen
                   FROM known_devices
                   WHERE visits > 1 AND last_seen >= ?
                   ORDER BY visits DESC, best_signal DESC
                   LIMIT 12""",
                (cutoff,),
            ),
            full_mac,
        )

        strongest = _mac_safe_rows(
            _rows(
                conn,
                """SELECT mac, vendor, type, alias, tag, visits, best_signal, last_seen, last_essid
                   FROM known_devices
                   WHERE last_seen >= ? AND best_signal > -100
                   ORDER BY best_signal DESC
                   LIMIT 12""",
                (cutoff,),
            ),
            full_mac,
        )

    conn.close()

    close_alerts = events_now.get("close_alert", 0)
    new_aps = events_now.get("new_ap", 0)
    bursts = events_now.get("burst", 0)
    join_events = events_now.get("device_join", 0)

    findings = []
    if total_events == 0:
        findings.append("No events recorded in this window. Verify airodump-ng is writing the CSV and the parser is running.")
    if close_alerts >= 5:
        findings.append(f"{close_alerts} close-signal alerts. Review strongest devices and camera context.")
    if bursts >= 10:
        findings.append(f"{bursts} packet bursts. This can indicate active scanning, busy nearby clients, or noisy capture conditions.")
    if new_aps >= max(6, events_prev.get("new_ap", 0) * 2):
        findings.append(f"{new_aps} new AP events, above the previous comparable window.")
    if previous_events and total_events >= previous_events * 1.75 and total_events >= 20:
        findings.append(f"Event volume is up {round(total_events / previous_events, 1)}x versus the previous window.")
    if new_devices >= 25:
        findings.append(f"{new_devices} first-seen devices. Check whether this was normal neighborhood/traffic activity.")

    recommendations = []
    if total_events == 0:
        recommendations.append("Confirm airodump-ng is writing the configured CSV and the server/parser has been running for the full review window.")
    if close_alerts:
        recommendations.append("Review close alerts first, then label familiar devices with alias/tag in the dashboard.")
    if returning_devices:
        recommendations.append("Use recurring devices to separate normal background presence from unusual one-off activity.")
    if not findings:
        recommendations.append("No major anomalies found. Keep collecting daily history so baselines become more useful.")

    return {
        "status": "ok",
        "generated_at": generated_at,
        "db_path": str(db_path),
        "hours": hours,
        "counts": {
            "total_known_devices": total_known,
            "active_devices": active_devices,
            "new_devices": new_devices,
            "returning_devices": returning_devices,
            "events": total_events,
            "previous_window_events": previous_events,
            "device_join": join_events,
            "close_alert": close_alerts,
            "new_ap": new_aps,
            "burst": bursts,
        },
        "event_types": events_now,
        "previous_event_types": events_prev,
        "top_vendors": top_vendors,
        "recurring_devices": recurring,
        "strongest_devices": strongest,
        "findings": findings,
        "recommendations": recommendations,
    }


def render_markdown(summary):
    lines = [
        "# Overwatch Daily Review",
        "",
        f"Generated: {summary.get('generated_at', '')}",
        f"Window: last {summary.get('hours', 24):g} hours",
        f"Database: `{summary.get('db_path', '')}`",
        "",
    ]

    if summary.get("status") != "ok":
        lines.extend(["## Status", summary.get("status", "unknown"), "", "## Recommendations"])
        lines.extend(f"- {item}" for item in summary.get("recommendations", []))
        return "\n".join(lines) + "\n"

    counts = summary["counts"]
    lines.extend([
        "## Snapshot",
        f"- Events: {counts['events']} ({counts['previous_window_events']} previous window)",
        f"- Active devices: {counts['active_devices']}",
        f"- New devices: {counts['new_devices']}",
        f"- Returning devices: {counts['returning_devices']}",
        f"- Close alerts: {counts['close_alert']}",
        f"- New AP events: {counts['new_ap']}",
        f"- Packet bursts: {counts['burst']}",
        "",
        "## Findings",
    ])
    lines.extend(f"- {item}" for item in summary.get("findings") or ["No major anomalies found."])

    lines.extend(["", "## Top Vendors"])
    for row in summary.get("top_vendors", [])[:10]:
        lines.append(f"- {row['vendor']}: {row['count']}")
    if not summary.get("top_vendors"):
        lines.append("- No vendor data in this window.")

    lines.extend(["", "## Strongest Devices"])
    for row in summary.get("strongest_devices", [])[:8]:
        label = row.get("alias") or row.get("mac")
        lines.append(
            f"- {label}: {row.get('vendor') or 'Unknown'} / {row.get('type') or 'unknown'} "
            f"/ best {row.get('best_signal')} dBm / visits {row.get('visits')}"
        )
    if not summary.get("strongest_devices"):
        lines.append("- No strong devices recorded.")

    lines.extend(["", "## Recurring Devices"])
    for row in summary.get("recurring_devices", [])[:8]:
        label = row.get("alias") or row.get("mac")
        mins = round((row.get("total_secs") or 0) / 60)
        lines.append(f"- {label}: {row.get('vendor') or 'Unknown'} / visits {row.get('visits')} / approx {mins} min")
    if not summary.get("recurring_devices"):
        lines.append("- No recurring devices in this window.")

    lines.extend(["", "## Recommendations"])
    lines.extend(f"- {item}" for item in summary.get("recommendations", []))
    lines.append("")
    lines.append("Privacy note: report output redacts MAC addresses by default. Use --full-mac only for local private review.")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Generate an Overwatch daily review report.")
    parser.add_argument("--db", default=REGISTRY_DB, help="Path to device_registry.db")
    parser.add_argument("--hours", type=float, default=REPORT_LOOKBACK_HOURS, help="Lookback window in hours")
    parser.add_argument("--out", default="", help="Markdown output path")
    parser.add_argument("--json", action="store_true", help="Print JSON summary to stdout")
    parser.add_argument("--full-mac", action="store_true", help="Do not redact MAC addresses in report output")
    parser.add_argument("--no-write", action="store_true", help="Do not write report files")
    args = parser.parse_args()

    summary = build_daily_summary(args.db, args.hours, full_mac=args.full_mac or REPORT_FULL_MAC)
    if args.json:
        print(json.dumps(summary, indent=2))

    if args.no_write:
        if not args.json:
            print(render_markdown(summary))
        return

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = Path(args.out) if args.out else REPORTS_DIR / f"overwatch_daily_{datetime.now().strftime('%Y-%m-%d')}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_markdown(summary), encoding="utf-8")
    (REPORTS_DIR / "overwatch_daily_latest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Wrote {report_path}")


if __name__ == "__main__":
    main()
