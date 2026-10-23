"""Send one DORA event to Firehose. Call it from any CI/CD pipeline.

Deployment (run after the production deploy step):

  python scripts/emit_event.py deployment --stream dora-gamify-events \
      --team payments --service checkout \
      --commit-time "$(git log -1 --format=%cI)" --outcome success

Incident resolved (run from your incident tool or by hand):

  python scripts/emit_event.py incident --stream dora-gamify-events \
      --team payments --service checkout \
      --opened-at 2026-10-20T09:10:00Z --resolved-at 2026-10-20T09:55:00Z

Add --print to show the event without sending it.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dora_events import deployment_event, incident_event, send_to_firehose  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="kind", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--stream", help="Firehose delivery stream name")
    common.add_argument("--team", required=True)
    common.add_argument("--service", required=True)
    common.add_argument("--source", default="ci")
    common.add_argument("--print", action="store_true", help="print the event, do not send it")

    d = sub.add_parser("deployment", parents=[common])
    d.add_argument("--commit-time", required=True, help="ISO 8601 time of the first commit in the change")
    d.add_argument("--deploy-time", help="ISO 8601 time of the deploy (default: now)")
    d.add_argument("--outcome", choices=["success", "failure"], default="success")
    d.add_argument("--caused-failure", action="store_true", help="this deployment caused an incident")

    i = sub.add_parser("incident", parents=[common])
    i.add_argument("--opened-at", required=True)
    i.add_argument("--resolved-at", help="default: now")

    args = p.parse_args(argv)
    now = datetime.now(timezone.utc)
    if args.kind == "deployment":
        event = deployment_event(args.team, args.service, args.commit_time,
                                 args.deploy_time or now, args.outcome,
                                 args.caused_failure, args.source)
    else:
        event = incident_event(args.team, args.service, args.opened_at,
                               args.resolved_at or now, args.source)

    if args.print:
        print(json.dumps(event, indent=2))
        return 0
    if not args.stream:
        p.error("--stream is required unless --print is used")
    send_to_firehose([event], args.stream)
    print(f"Sent {event['event_type']} event {event['event_id']} to {args.stream}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
