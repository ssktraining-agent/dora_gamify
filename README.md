# dora_gamify

A weekly DORA leaderboard for **teams**, posted to Slack. CI pipelines report deployments and incidents; every Monday a Lambda scores each team on the four DORA metrics, compares this week with last week, and celebrates the most improved team.

```
CI/CD pipelines ──► Kinesis Firehose ──► S3 (JSON, by day) ──► Athena ──► Lambda (weekly) ──► Slack
 emit_event.py                                                    │
                                                                  └──► QuickSight (optional)
```

All AWS infrastructure is in Terraform. The scoring logic has unit tests, and you can preview the whole leaderboard locally without an AWS account.

## Why teams, not people

DORA metrics measure how well a delivery system works: deployment frequency, lead time for changes, change failure rate and time to restore service. They are meant for teams and systems. Ranking individuals on them encourages gaming (many tiny deploys, incidents left unreported) and hides the real bottlenecks, which are usually in the pipeline, the review process or the architecture.

So this project:

- groups every metric by team, never by developer
- celebrates the **most improved** team, so a team starting from a low base can still win
- ignores a metric when there is no data for it, so a week with no incidents is neither rewarded nor punished

## The four metrics

| Metric | How it is calculated here | Elite | High | Medium | Low |
|---|---|---|---|---|---|
| Deployment frequency | Successful production deployments per day | 1 or more | weekly to daily | monthly to weekly | less than monthly |
| Lead time for changes | Median time from first commit to running in production | under 1 day | under 1 week | under 1 month | 1 month or more |
| Change failure rate | Production deployments that caused an incident, divided by all production deployments | 5% or less | 10% or less | 15% or less | over 15% |
| Time to restore | Median time from incident start to service restored | under 1 hour | under 1 day | under 1 week | 1 week or more |

The bands follow the DORA State of DevOps reports. The thresholds vary a little between report years, so adjust them in `lambda/leaderboard.py` to match the report your organisation uses.

Each band is worth points (elite 4, high 3, medium 2, low 1). A team's score is the average of the metrics that have data, out of 4.

## Try it locally (no AWS needed)

```bash
pip install -r requirements-dev.txt
python scripts/simulate.py --out events.jsonl --seed 7
python scripts/preview_leaderboard.py events.jsonl
```

The simulator creates two weeks of events for four demo teams with different delivery profiles. `legacy-billing` automates its pipeline in the second week, so it should come out as most improved. Output looks like this (exact numbers vary because the data is generated relative to the current time):

```
*DORA team leaderboard - last 7 days*

1. *payments*: 4.0 / 4 (up 0.2)
    Deploys 20 (2.9/day, elite) | Lead time 4.0h (elite) | Change failure 5% (elite) | Restore 12m (elite)
2. *search*: 3.7 / 4 (no change)
    Deploys 7 (1.0/day, elite) | Lead time 2.0d (high) | Change failure 0% (elite) | Restore - (no data)
3. *mobile*: 3.0 / 4 (up 0.7)
    Deploys 6 (0.9/day, high) | Lead time 6.6d (high) | Change failure 0% (elite) | Restore 2.5d (medium)
4. *legacy-billing*: 2.8 / 4 (up 1.1)
    Deploys 9 (1.3/day, elite) | Lead time 1.1d (high) | Change failure 22% (low) | Restore 7.0h (high)

:trophy: Most improved: *legacy-billing* (up 1.1)

_Team metrics only. Use them to find bottlenecks, not to rank people._
```

Run the tests with `pytest -q`.

## Deploy to AWS

**Requirements:** Terraform 1.5 or later, AWS credentials, Python 3.10 or later.

**1. Create the infrastructure.**

```bash
cd terraform
terraform init
terraform apply                      # defaults: name "dora-gamify", region eu-west-1
```

This creates an S3 bucket, a Firehose stream, a Glue table with partition projection (new days are queryable immediately, with no crawler), an Athena workgroup, the leaderboard Lambda and a weekly EventBridge schedule (Mondays 08:00 UTC).

**2. Connect Slack** (optional). Create an [incoming webhook](https://api.slack.com/messaging/webhooks) for your channel and store it in SSM:

```bash
aws ssm put-parameter --name /dora-gamify/slack-webhook --type SecureString \
  --value "https://hooks.slack.com/services/..."
```

Without it, the Lambda writes the leaderboard to its CloudWatch log instead.

**3. Send demo data**, wait about a minute for Firehose to write it to S3, then run the leaderboard:

```bash
cd ..
python scripts/simulate.py --stream dora-gamify-events
aws lambda invoke --function-name dora-gamify-leaderboard out.json && cat out.json
```

**4. Report real deployments from CI.** Attach the policy from the `producer_policy_arn` output to your CI role, then add a step after each production deploy:

```bash
python scripts/emit_event.py deployment --stream dora-gamify-events \
  --team payments --service checkout \
  --commit-time "$(git log -1 --format=%cI)" --outcome success
```

When a deployment causes an incident, add `--caused-failure`. When the incident is resolved, report it:

```bash
python scripts/emit_event.py incident --stream dora-gamify-events \
  --team payments --service checkout \
  --opened-at 2026-10-20T09:10:00Z --resolved-at 2026-10-20T09:55:00Z
```

`examples/github-actions-deploy.yml` shows a complete GitHub Actions job, including failed deployments and lead time measured from the oldest commit in the push.

**5. Dashboards** (optional). `athena/team_weekly_metrics.sql` returns the metrics per team per week for the last 90 days. Use it as a QuickSight dataset or run it in the Athena console.

## Event format

Events are JSON, one per line in S3:

| Field | Deployments | Incidents |
|---|---|---|
| `event_id`, `event_time`, `team`, `service`, `source` | yes | yes |
| `event_type` | `deployment` | `incident_resolved` |
| `outcome` | `success` or `failure` (pipeline failed, nothing shipped) | |
| `caused_failure` | `true` if this production deployment caused an incident | |
| `commit_time`, `deploy_time` | yes | |
| `opened_at`, `resolved_at` | | yes |

Any tool can send events, as long as each Firehose record is one JSON object ending in a newline. `scripts/dora_events.py` handles this.

## Cost and clean-up

For a demo, costs are small: Firehose and Athena are billed per GB processed, and the Lambda runs once a week. Remove everything with:

```bash
cd terraform
terraform destroy
```

By default this also deletes the bucket and its data (`force_destroy_bucket = true`). Set it to `false` before using this for real data.

## Project layout

```
lambda/leaderboard.py          Scoring, Athena query and Slack message (the Lambda)
scripts/dora_events.py         Event schema and Firehose sending, shared by the scripts
scripts/emit_event.py          Report one deployment or incident from CI
scripts/simulate.py            Generate demo events for four teams
scripts/preview_leaderboard.py Preview the leaderboard from a local file
athena/team_weekly_metrics.sql Weekly metrics per team, for dashboards
terraform/                     All AWS infrastructure
examples/                      GitHub Actions example
tests/                         Unit tests
```

## Author

Ravi Kulkarni: cloud and DevOps trainer and engineer. [LinkedIn](https://www.linkedin.com/in/ravisharadkulkarni)
