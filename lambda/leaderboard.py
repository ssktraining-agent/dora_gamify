"""Weekly DORA team leaderboard.

Queries the DORA events table in Athena, scores each team on the four DORA
metrics, compares this week with last week, and posts the result to Slack.

Metrics are calculated per team, never per person. DORA metrics describe how
well a delivery system works; using them to rank individuals encourages
gaming and hides the real bottlenecks.
"""

import json
import math
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "7"))
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "dora-gamify")
GLUE_DATABASE = os.environ.get("GLUE_DATABASE", "dora_gamify")
GLUE_TABLE = os.environ.get("GLUE_TABLE", "dora_events")
SLACK_WEBHOOK_PARAM = os.environ.get("SLACK_WEBHOOK_PARAM", "")
QUERY_TIMEOUT_SEC = int(os.environ.get("QUERY_TIMEOUT_SEC", "90"))

# Performance bands based on the DORA State of DevOps reports.
# Thresholds differ slightly between report years; change them to match the
# report your organisation follows.
MINUTES_PER_DAY = 24 * 60
POINTS = {"elite": 4, "high": 3, "medium": 2, "low": 1}


def band_deploy_frequency(deployments, window_days):
    """Successful production deployments per day."""
    if deployments is None:
        return None
    per_day = deployments / window_days
    if per_day >= 1:
        return "elite"  # on demand, at least daily
    if per_day >= 1 / 7:
        return "high"  # between daily and weekly
    if per_day >= 1 / 30:
        return "medium"  # between weekly and monthly
    return "low"


def band_lead_time(minutes):
    """Median time from commit to running in production."""
    if minutes is None:
        return None
    if minutes < MINUTES_PER_DAY:
        return "elite"
    if minutes < 7 * MINUTES_PER_DAY:
        return "high"
    if minutes < 30 * MINUTES_PER_DAY:
        return "medium"
    return "low"


def band_change_failure_rate(rate):
    """Share of production deployments that caused a failure."""
    if rate is None:
        return None
    if rate <= 0.05:
        return "elite"
    if rate <= 0.10:
        return "high"
    if rate <= 0.15:
        return "medium"
    return "low"


def band_time_to_restore(minutes):
    """Median time to restore service after an incident."""
    if minutes is None:
        return None
    if minutes < 60:
        return "elite"
    if minutes < MINUTES_PER_DAY:
        return "high"
    if minutes < 7 * MINUTES_PER_DAY:
        return "medium"
    return "low"


def score_team(metrics, window_days):
    """Band each metric and average the points of the metrics that have data.

    A metric with no data (for example, no incidents this week) is shown but
    not counted, so a quiet week is not punished or rewarded.
    """
    deployments = metrics.get("deployments")
    failed = metrics.get("deployments_failed") or 0
    rate = (failed / deployments) if deployments else None
    bands = {
        "deploy_frequency": band_deploy_frequency(deployments, window_days),
        "lead_time": band_lead_time(metrics.get("lead_time_min")),
        "change_failure_rate": band_change_failure_rate(rate),
        "time_to_restore": band_time_to_restore(metrics.get("restore_min")),
    }
    points = [POINTS[b] for b in bands.values() if b]
    score = round(sum(points) / len(points), 2) if points else None
    return {"bands": bands, "score": score, "change_failure_rate": rate}


def _to_number(value, cast):
    if value in (None, ""):
        return None
    return cast(float(value))


def parse_rows(rows):
    """Turn Athena result rows (dicts of strings) into typed metric dicts."""
    parsed = []
    for row in rows:
        parsed.append({
            "team": row["team"],
            "period": row["period"],
            "deployments": _to_number(row.get("deployments"), int) or 0,
            "deployments_failed": _to_number(row.get("deployments_failed"), int) or 0,
            "lead_time_min": _to_number(row.get("lead_time_min"), float),
            "incidents": _to_number(row.get("incidents"), int) or 0,
            "restore_min": _to_number(row.get("restore_min"), float),
        })
    return parsed


def build_leaderboard(rows, window_days):
    """Score each team for the current and previous window and sort them."""
    teams = {}
    for row in rows:
        teams.setdefault(row["team"], {})[row["period"]] = row

    board = []
    for team, periods in teams.items():
        # A team with no events in a window simply deployed nothing that week.
        empty = {"team": team, "deployments": 0, "deployments_failed": 0,
                 "lead_time_min": None, "incidents": 0, "restore_min": None}
        current = periods.get("current") or {**empty, "period": "current"}
        previous = periods.get("previous") or {**empty, "period": "previous"}
        now = score_team(current, window_days)
        before = score_team(previous, window_days)
        delta = None
        if before["score"] is not None and now["score"] is not None:
            delta = round(now["score"] - before["score"], 2)
        board.append({"team": team, "metrics": current, **now, "delta": delta})

    board.sort(key=lambda t: (-(t["score"] or 0), t["team"]))
    return board


def most_improved(board):
    improved = [t for t in board if t["delta"] is not None and t["delta"] > 0]
    if not improved:
        return None
    best = max(improved, key=lambda t: t["delta"])
    ties = [t for t in improved if t["delta"] == best["delta"]]
    return best if len(ties) == 1 else None


def _duration(minutes):
    if minutes is None:
        return "-"
    # Round down so a value just under a band limit never prints as the limit.
    if minutes < 60:
        return f"{math.floor(minutes)}m"
    if minutes < MINUTES_PER_DAY:
        return f"{math.floor(minutes / 6) / 10:.1f}h"
    return f"{math.floor(minutes / 144) / 10:.1f}d"


def _band(name):
    return name if name else "no data"


def format_slack(board, window_days):
    """Build a Slack message. Plain mrkdwn text keeps it readable everywhere."""
    lines = [f"*DORA team leaderboard - last {window_days} days*", ""]
    if not board:
        lines.append("No deployments recorded in this period.")
    for rank, t in enumerate(board, start=1):
        m, b = t["metrics"], t["bands"]
        score = f"{t['score']:.1f} / 4" if t["score"] is not None else "no data"
        if t["delta"] is None:
            change = "no comparison"
        elif t["delta"] > 0:
            change = f"up {t['delta']:.1f}"
        elif t["delta"] < 0:
            change = f"down {abs(t['delta']):.1f}"
        else:
            change = "no change"
        rate = t["change_failure_rate"]
        rate_text = f"{rate:.0%}" if rate is not None else "-"
        lines.append(f"{rank}. *{t['team']}*: {score} ({change})")
        lines.append(
            f"    Deploys {m['deployments']} ({m['deployments'] / window_days:.1f}/day, "
            f"{_band(b['deploy_frequency'])})"
            f" | Lead time {_duration(m['lead_time_min'])} ({_band(b['lead_time'])})"
            f" | Change failure {rate_text} ({_band(b['change_failure_rate'])})"
            f" | Restore {_duration(m['restore_min'])} ({_band(b['time_to_restore'])})"
        )
    best = most_improved(board)
    if best:
        lines += ["", f":trophy: Most improved: *{best['team']}* (up {best['delta']:.1f})"]
    lines += ["", "_Team metrics only. Use them to find bottlenecks, not to rank people._"]
    return {"text": "\n".join(lines)}


def build_query(now, window_days, table):
    """Athena SQL returning one row per team per window (current, previous)."""
    current_start = now - timedelta(days=window_days)
    previous_start = now - timedelta(days=2 * window_days)
    start_dt = (previous_start - timedelta(days=1)).strftime("%Y-%m-%d")
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return f"""
WITH ev AS (
  SELECT team, event_type, outcome, caused_failure,
         from_iso8601_timestamp(event_time)  AS event_ts,
         from_iso8601_timestamp(commit_time) AS commit_ts,
         from_iso8601_timestamp(deploy_time) AS deploy_ts,
         from_iso8601_timestamp(opened_at)   AS opened_ts,
         from_iso8601_timestamp(resolved_at) AS resolved_ts
  FROM {table}
  WHERE dt >= '{start_dt}'
),
w AS (
  SELECT *,
         CASE WHEN event_ts >= from_iso8601_timestamp('{iso(current_start)}') THEN 'current'
              WHEN event_ts >= from_iso8601_timestamp('{iso(previous_start)}') THEN 'previous'
         END AS period
  FROM ev
  WHERE event_ts < from_iso8601_timestamp('{iso(now)}')
)
SELECT team, period,
       count_if(event_type = 'deployment' AND outcome = 'success') AS deployments,
       count_if(event_type = 'deployment' AND outcome = 'success' AND caused_failure) AS deployments_failed,
       approx_percentile(CASE WHEN event_type = 'deployment' AND outcome = 'success'
                              THEN date_diff('second', commit_ts, deploy_ts) END, 0.5) / 60.0 AS lead_time_min,
       count_if(event_type = 'incident_resolved') AS incidents,
       approx_percentile(CASE WHEN event_type = 'incident_resolved'
                              THEN date_diff('second', opened_ts, resolved_ts) END, 0.5) / 60.0 AS restore_min
FROM w
WHERE period IS NOT NULL
GROUP BY team, period
ORDER BY team, period
""".strip()


def run_query(sql):
    import boto3

    athena = boto3.client("athena")
    qid = athena.start_query_execution(
        QueryString=sql,
        WorkGroup=ATHENA_WORKGROUP,
        QueryExecutionContext={"Database": GLUE_DATABASE},
    )["QueryExecutionId"]

    deadline = time.time() + QUERY_TIMEOUT_SEC
    while True:
        status = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"Athena query {state}: {status.get('StateChangeReason', '')}")
        if time.time() > deadline:
            raise TimeoutError(f"Athena query {qid} did not finish in {QUERY_TIMEOUT_SEC}s")
        time.sleep(1)

    rows, header = [], None
    for page in athena.get_paginator("get_query_results").paginate(QueryExecutionId=qid):
        for r in page["ResultSet"]["Rows"]:
            values = [c.get("VarCharValue") for c in r["Data"]]
            if header is None:
                header = values
                continue
            rows.append(dict(zip(header, values)))
    return rows


def get_webhook():
    if not SLACK_WEBHOOK_PARAM:
        return None
    import boto3

    ssm = boto3.client("ssm")
    try:
        value = ssm.get_parameter(Name=SLACK_WEBHOOK_PARAM, WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        print(f"Slack webhook parameter {SLACK_WEBHOOK_PARAM} not found; printing instead.")
        return None
    if not value.startswith("https://hooks.slack.com/"):
        print("Slack webhook parameter is not a Slack webhook URL; printing instead.")
        return None
    return value


def post_to_slack(webhook, payload):
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Slack returned HTTP {resp.status}")


def handler(event, context):
    now = datetime.now(timezone.utc)
    rows = parse_rows(run_query(build_query(now, WINDOW_DAYS, GLUE_TABLE)))
    board = build_leaderboard(rows, WINDOW_DAYS)
    payload = format_slack(board, WINDOW_DAYS)
    webhook = get_webhook()
    if webhook:
        post_to_slack(webhook, payload)
    else:
        print(payload["text"])
    return {"teams": len(board), "posted_to_slack": bool(webhook)}
