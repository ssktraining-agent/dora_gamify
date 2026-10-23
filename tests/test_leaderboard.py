import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lambda"))
sys.path.insert(0, str(ROOT / "scripts"))

import leaderboard as lb  # noqa: E402
import dora_events  # noqa: E402
import emit_event  # noqa: E402
import preview_leaderboard  # noqa: E402
import simulate  # noqa: E402

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


# --- bands ------------------------------------------------------------------

@pytest.mark.parametrize("deploys,band", [(7, "elite"), (1, "high"), (0, "low"), (None, None)])
def test_deploy_frequency_band(deploys, band):
    assert lb.band_deploy_frequency(deploys, 7) == band


@pytest.mark.parametrize("minutes,band", [
    (60, "elite"), (1439, "elite"), (1440, "high"), (7 * 1440, "medium"), (30 * 1440, "low"), (None, None)])
def test_lead_time_band(minutes, band):
    assert lb.band_lead_time(minutes) == band


@pytest.mark.parametrize("rate,band", [(0.0, "elite"), (0.05, "elite"), (0.08, "high"), (0.15, "medium"), (0.4, "low")])
def test_change_failure_band(rate, band):
    assert lb.band_change_failure_rate(rate) == band


@pytest.mark.parametrize("minutes,band", [(30, "elite"), (60, "high"), (1440, "medium"), (7 * 1440, "low")])
def test_restore_band(minutes, band):
    assert lb.band_time_to_restore(minutes) == band


# --- scoring ----------------------------------------------------------------

def row(team, period, deployments=7, failed=0, lead=60.0, incidents=0, restore=None):
    return {"team": team, "period": period, "deployments": deployments, "deployments_failed": failed,
            "lead_time_min": lead, "incidents": incidents, "restore_min": restore}


def test_metric_without_data_is_not_counted():
    scored = lb.score_team(row("a", "current", restore=None), 7)
    assert scored["bands"]["time_to_restore"] is None
    assert scored["score"] == 4.0  # three elite metrics, restore not counted


def test_change_failure_rate_uses_production_deployments():
    scored = lb.score_team(row("a", "current", deployments=10, failed=2), 7)
    assert scored["change_failure_rate"] == pytest.approx(0.2)
    assert scored["bands"]["change_failure_rate"] == "low"


def test_team_with_no_events_last_week_counts_as_zero_deployments():
    board = lb.build_leaderboard([row("a", "current")], 7)
    assert board[0]["delta"] == 3.0  # 4.0 now vs 1.0 (no deploys) last week


def test_team_with_no_events_this_week_still_appears():
    board = lb.build_leaderboard([row("a", "previous")], 7)
    assert board[0]["team"] == "a" and board[0]["score"] == 1.0


def test_leaderboard_sorted_by_score_then_name():
    rows = [row("zeta", "current"), row("alpha", "current"), row("slow", "current", deployments=0, lead=None)]
    assert [t["team"] for t in lb.build_leaderboard(rows, 7)] == ["alpha", "zeta", "slow"]


def test_most_improved_and_ties():
    rows = [row("a", "current"), row("a", "previous", lead=10 * 1440),
            row("b", "current"), row("b", "previous")]
    assert lb.most_improved(lb.build_leaderboard(rows, 7))["team"] == "a"
    tie = [row("a", "current"), row("a", "previous", lead=10 * 1440),
           row("b", "current"), row("b", "previous", lead=10 * 1440)]
    assert lb.most_improved(lb.build_leaderboard(tie, 7)) is None


def test_slack_message_names_teams_not_people():
    text = lb.format_slack(lb.build_leaderboard([row("payments", "current")], 7), 7)["text"]
    assert "*payments*" in text and "not to rank people" in text


def test_duration_rounds_down():
    assert lb._duration(1439.9) == "23.9h"
    assert lb._duration(59.9) == "59m"


def test_parse_rows_handles_athena_strings_and_nulls():
    parsed = lb.parse_rows([{"team": "a", "period": "current", "deployments": "3",
                             "deployments_failed": "0", "lead_time_min": "90.5",
                             "incidents": "0", "restore_min": None}])
    assert parsed[0]["deployments"] == 3 and parsed[0]["lead_time_min"] == 90.5
    assert parsed[0]["restore_min"] is None


def test_query_uses_both_windows_and_partition_filter():
    sql = lb.build_query(NOW, 7, "dora_events")
    assert "FROM dora_events" in sql
    assert "dt >= '2026-10-08'" in sql
    assert "'2026-10-16T12:00:00Z'" in sql and "'2026-10-09T12:00:00Z'" in sql


# --- events and producers ---------------------------------------------------

def test_deployment_event_validation():
    with pytest.raises(ValueError):
        dora_events.deployment_event("a", "s", NOW, NOW - timedelta(hours=1))
    failed_pipeline = dora_events.deployment_event("a", "s", NOW - timedelta(hours=1), NOW,
                                                   outcome="failure", caused_failure=True)
    assert failed_pipeline["caused_failure"] is False  # nothing shipped, so it cannot cause a failure


def test_records_are_newline_terminated_json():
    rec = dora_events.to_record(dora_events.incident_event("a", "s", NOW - timedelta(hours=1), NOW))
    assert rec.endswith(b"\n") and json.loads(rec)["event_type"] == "incident_resolved"


class FakeFirehose:
    def __init__(self, fail_first=0):
        self.calls, self.fail_first = [], fail_first

    def put_record_batch(self, DeliveryStreamName, Records):
        self.calls.append(len(Records))
        failed = min(self.fail_first, len(Records))
        self.fail_first = 0
        return {"FailedPutCount": failed,
                "RequestResponses": [{"ErrorCode": "x"}] * failed + [{}] * (len(Records) - failed)}


def test_send_batches_and_retries_failed_records():
    events = [dora_events.incident_event("a", "s", NOW - timedelta(hours=1), NOW) for _ in range(501)]
    client = FakeFirehose(fail_first=2)
    assert dora_events.send_to_firehose(events, "stream", client) == 501
    assert client.calls == [500, 2, 1]


def test_emit_event_print_mode(capsys):
    assert emit_event.main(["deployment", "--team", "a", "--service", "s",
                            "--commit-time", "2026-10-23T05:00:00Z",
                            "--deploy-time", "2026-10-23T06:00:00Z", "--print"]) == 0
    assert json.loads(capsys.readouterr().out)["event_type"] == "deployment"


def test_simulated_week_produces_a_full_leaderboard():
    events = simulate.generate(NOW, seed=7)
    rows = preview_leaderboard.compute_rows(events, NOW, 7)
    board = lb.build_leaderboard(rows, 7)
    assert {t["team"] for t in board} == set(simulate.PROFILES)
    assert all(e["event_time"] < "2026-10-23T12:00:00Z" for e in events)


def test_handler_prints_when_no_webhook(monkeypatch, capsys):
    athena_rows = [{"team": "payments", "period": "current", "deployments": "14",
                    "deployments_failed": "0", "lead_time_min": "120", "incidents": "0",
                    "restore_min": None}]
    monkeypatch.setattr(lb, "run_query", lambda sql: athena_rows)
    monkeypatch.setattr(lb, "get_webhook", lambda: None)
    result = lb.handler({}, None)
    assert result == {"teams": 1, "posted_to_slack": False}
    assert "*payments*: 4.0 / 4" in capsys.readouterr().out


def test_handler_posts_when_webhook_set(monkeypatch):
    sent = {}
    monkeypatch.setattr(lb, "run_query", lambda sql: [])
    monkeypatch.setattr(lb, "get_webhook", lambda: "https://hooks.slack.com/services/T/B/X")
    monkeypatch.setattr(lb, "post_to_slack", lambda url, payload: sent.update(url=url, payload=payload))
    assert lb.handler({}, None)["posted_to_slack"] is True
    assert "No deployments recorded" in sent["payload"]["text"]
