"""Generate two windows of realistic DORA events for four demo teams.

Each team has a different delivery profile, and "legacy-billing" improves in
the current window, so the leaderboard has a clear "most improved" team.

  # write to a local file (no AWS needed), then preview the leaderboard
  python scripts/simulate.py --out events.jsonl
  python scripts/preview_leaderboard.py events.jsonl

  # send to Firehose
  python scripts/simulate.py --stream dora-gamify-events
"""

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dora_events import deployment_event, incident_event, send_to_firehose  # noqa: E402

# deploys_per_day, lead time (hours), chance a deploy causes an incident,
# time to restore (minutes), chance the pipeline fails before deploying
PROFILES = {
    "payments": {"service": "checkout", "deploys": 3.0, "lead_h": (1, 8),
                 "fail": 0.04, "restore_min": (10, 50), "pipeline_fail": 0.03},
    "search": {"service": "search-api", "deploys": 1.0, "lead_h": (12, 60),
               "fail": 0.08, "restore_min": (90, 600), "pipeline_fail": 0.05},
    "mobile": {"service": "mobile-app", "deploys": 0.7, "lead_h": (72, 240),
               "fail": 0.12, "restore_min": (1440, 4320), "pipeline_fail": 0.08},
    "legacy-billing": {"service": "billing", "deploys": 0.3, "lead_h": (720, 1080),
                       "fail": 0.5, "restore_min": (10080, 20160), "pipeline_fail": 0.15,
                       # this team's current window, after they automated their pipeline
                       "improved": {"deploys": 1.2, "lead_h": (12, 40), "fail": 0.08,
                                    "restore_min": (120, 600), "pipeline_fail": 0.05}},
}


def _count(rate, rng):
    """Integer count with the given expected value."""
    whole = int(rate)
    return whole + (1 if rng.random() < rate - whole else 0)


def generate(now, window_days=7, seed=None):
    rng = random.Random(seed)
    events = []
    for team, base in PROFILES.items():
        for days_ago in range(2 * window_days):
            in_current = days_ago < window_days
            prof = {**base, **base.get("improved", {})} if in_current else base
            day_start = now - timedelta(days=days_ago + 1)
            for _ in range(_count(prof["deploys"], rng)):
                deploy_time = day_start + timedelta(seconds=rng.uniform(0, 86400))
                lead = timedelta(hours=rng.uniform(*prof["lead_h"]))
                if rng.random() < prof["pipeline_fail"]:
                    events.append(deployment_event(team, prof["service"], deploy_time - lead,
                                                   deploy_time, "failure", source="simulator"))
                    continue
                caused = rng.random() < prof["fail"]
                events.append(deployment_event(team, prof["service"], deploy_time - lead,
                                               deploy_time, "success", caused, "simulator"))
                if caused:
                    opened = deploy_time + timedelta(minutes=rng.uniform(5, 60))
                    resolved = opened + timedelta(minutes=rng.uniform(*prof["restore_min"]))
                    if resolved < now:  # still open incidents are not counted yet
                        events.append(incident_event(team, prof["service"], opened,
                                                     resolved, "simulator"))
    events.sort(key=lambda e: e["event_time"])
    return events


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--stream", help="Firehose delivery stream name")
    target.add_argument("--out", help="write JSON lines to this file instead")
    p.add_argument("--window-days", type=int, default=7)
    p.add_argument("--seed", type=int, help="fixed seed for repeatable data")
    args = p.parse_args(argv)

    events = generate(datetime.now(timezone.utc), args.window_days, args.seed)
    if args.out:
        with open(args.out, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        print(f"Wrote {len(events)} events to {args.out}")
    else:
        sent = send_to_firehose(events, args.stream)
        print(f"Sent {sent} events to {args.stream}. Firehose writes them to S3 within about a minute.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
