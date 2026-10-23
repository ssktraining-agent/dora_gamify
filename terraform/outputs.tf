output "firehose_stream_name" {
  description = "Pass this to scripts/emit_event.py and scripts/simulate.py as --stream"
  value       = aws_kinesis_firehose_delivery_stream.events.name
}

output "producer_policy_arn" {
  description = "Attach to the CI role or user that sends events"
  value       = aws_iam_policy.producer.arn
}

output "data_bucket" {
  value = aws_s3_bucket.data.bucket
}

output "athena_workgroup" {
  value = aws_athena_workgroup.dora.name
}

output "glue_database" {
  value = aws_glue_catalog_database.dora.name
}

output "leaderboard_function" {
  description = "Invoke it by hand with: aws lambda invoke --function-name <this> out.json"
  value       = aws_lambda_function.leaderboard.function_name
}
