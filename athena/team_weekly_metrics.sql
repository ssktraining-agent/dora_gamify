-- DORA metrics per team per week, for QuickSight or ad hoc analysis in Athena.
-- Run in the dora-gamify workgroup against the dora_gamify database.
-- Change the dt filter to cover the period you want.

WITH ev AS (
  SELECT team, event_type, outcome, caused_failure,
         from_iso8601_timestamp(event_time)  AS event_ts,
         from_iso8601_timestamp(commit_time) AS commit_ts,
         from_iso8601_timestamp(deploy_time) AS deploy_ts,
         from_iso8601_timestamp(opened_at)   AS opened_ts,
         from_iso8601_timestamp(resolved_at) AS resolved_ts
  FROM dora_events
  WHERE dt >= CAST(current_date - INTERVAL '90' DAY AS varchar)  -- 'YYYY-MM-DD'
)
SELECT team,
       date_trunc('week', event_ts) AS week,
       count_if(event_type = 'deployment' AND outcome = 'success') AS deployments,
       round(approx_percentile(CASE WHEN event_type = 'deployment' AND outcome = 'success'
                                    THEN date_diff('second', commit_ts, deploy_ts) END, 0.5) / 3600.0, 1)
         AS median_lead_time_hours,
       round(100.0 * count_if(event_type = 'deployment' AND outcome = 'success' AND caused_failure)
             / nullif(count_if(event_type = 'deployment' AND outcome = 'success'), 0), 1)
         AS change_failure_rate_pct,
       round(approx_percentile(CASE WHEN event_type = 'incident_resolved'
                                    THEN date_diff('second', opened_ts, resolved_ts) END, 0.5) / 3600.0, 1)
         AS median_time_to_restore_hours
FROM ev
GROUP BY team, date_trunc('week', event_ts)
ORDER BY week DESC, team;
