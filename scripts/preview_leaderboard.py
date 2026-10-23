"""Preview the Slack leaderboard from a local JSON lines file, without AWS.

  python scripts/simulate.py --out events.jsonl --seed 7
  python scripts/preview_leaderboard.py events.jsonl

The metric calculation here mirrors the Athena query in lambda/leaderboard.py
(the median here is exact, Athena's approx_percentile is approximate).
"""

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lambda"))
sys.path.insert(0, str(ROOT / "scripts"))
import leaderboard  # noqa: E402
from dora_events import parse_time  # noqa: E402


def compute_rows(events, now, window_days):
    current_start = now - timedelta(days=window_days)
    previous_start = now - timedelta(days=2 * window_days)
    groups = {}
    for e in events:
        t = parse_time(e["event_time"])
        if t >= now or t < previous_start:
            continue
        period = "current" if t >= current_start else "previous"
        groups.setdefault((e["team"], period), []).append(e)

    rows = []
    for (team, period), evs in sorted(groups.items()):
        ok = [e for e in evs if e["event_type"] == "deployment" and e.get("outcome") == "success"]
        inc = [e for e in evs if e["event_type"] == "incident_resolved"]
        lead = [(parse_time(e["deploy_time"]) - parse_time(e["commit_time"])).total_seconds() / 60 for e in ok]
        restore = [(parse_time(e["resolved_at"]) - parse_time(e["opened_at"])).total_seconds() / 60 for e in inc]
        rows.append({
            "team": team,
            "period": period,
            "deployments": len(ok),
            "deployments_failed": sum(1 for e in ok if e.get("caused_failure")),
            "lead_time_min": statistics.median(lead) if lead else None,
            "incidents": len(inc),
            "restore_min": statistics.median(restore) if restore else None,
        })
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("events_file")
    p.add_argument("--window-days", type=int, default=7)
    args = p.parse_args(argv)

    with open(args.events_file) as f:
        events = [json.loads(line) for line in f if line.strip()]
    rows = compute_rows(events, datetime.now(timezone.utc), args.window_days)
    board = leaderboard.build_leaderboard(rows, args.window_days)
    print(leaderboard.format_slack(board, args.window_days)["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
