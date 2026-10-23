"""Shared helpers for building DORA events and sending them to Firehose.

Event schema (one JSON object per line in S3):

  event_id        unique id
  event_time      when the event happened (deploy time, or incident resolved time)
  team, service   who owns the change
  event_type      "deployment" or "incident_resolved"
  outcome         deployments only: "success" or "failure" (pipeline failed, nothing shipped)
  caused_failure  deployments only: true if this production deployment caused an incident
  commit_time     deployments only: time of the first commit in the change
  deploy_time     deployments only: time the change reached production
  opened_at       incidents only: when the incident started
  resolved_at     incidents only: when service was restored
"""

import json
import uuid
from datetime import datetime, timezone

EVENT_TYPES = ("deployment", "incident_resolved")
OUTCOMES = ("success", "failure")


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def deployment_event(team, service, commit_time, deploy_time, outcome="success",
                     caused_failure=False, source="manual"):
    commit_time, deploy_time = parse_time(commit_time), parse_time(deploy_time)
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    if commit_time > deploy_time:
        raise ValueError("commit_time must be before deploy_time")
    return {
        "event_id": str(uuid.uuid4()),
        "event_time": iso(deploy_time),
        "team": team,
        "service": service,
        "event_type": "deployment",
        "outcome": outcome,
        "caused_failure": bool(caused_failure) and outcome == "success",
        "commit_time": iso(commit_time),
        "deploy_time": iso(deploy_time),
        "source": source,
    }


def incident_event(team, service, opened_at, resolved_at, source="manual"):
    opened_at, resolved_at = parse_time(opened_at), parse_time(resolved_at)
    if opened_at > resolved_at:
        raise ValueError("opened_at must be before resolved_at")
    return {
        "event_id": str(uuid.uuid4()),
        "event_time": iso(resolved_at),
        "team": team,
        "service": service,
        "event_type": "incident_resolved",
        "opened_at": iso(opened_at),
        "resolved_at": iso(resolved_at),
        "source": source,
    }


def to_record(event):
    # Firehose joins records end to end, so each one needs its own newline
    # for Athena to read one JSON object per line.
    return (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")


def send_to_firehose(events, stream, client=None, batch_size=500):
    """Send events with PutRecordBatch, retrying failed records once."""
    if client is None:
        import boto3
        client = boto3.client("firehose")
    sent = 0
    for start in range(0, len(events), batch_size):
        batch = [{"Data": to_record(e)} for e in events[start:start + batch_size]]
        resp = client.put_record_batch(DeliveryStreamName=stream, Records=batch)
        if resp.get("FailedPutCount"):
            retry = [rec for rec, res in zip(batch, resp["RequestResponses"]) if "ErrorCode" in res]
            resp2 = client.put_record_batch(DeliveryStreamName=stream, Records=retry)
            if resp2.get("FailedPutCount"):
                raise RuntimeError(f"{resp2['FailedPutCount']} records failed after retry")
        sent += len(batch)
    return sent
