# ---------------------------------------------------------------------------
# Leaderboard Lambda: runs weekly, queries Athena, posts to Slack
# ---------------------------------------------------------------------------

data "archive_file" "leaderboard" {
  type        = "zip"
  source_file = "${path.module}/../lambda/leaderboard.py"
  output_path = "${path.module}/build/leaderboard.zip"
}

resource "aws_iam_role" "leaderboard" {
  name = "${var.name}-leaderboard"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "leaderboard_logs" {
  role       = aws_iam_role.leaderboard.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "leaderboard" {
  name = "query-and-post"
  role = aws_iam_role.leaderboard.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RunAthenaQueries"
        Effect   = "Allow"
        Action   = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults"]
        Resource = aws_athena_workgroup.dora.arn
      },
      {
        Sid    = "ReadTableDefinition"
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetTable", "glue:GetPartitions"]
        Resource = [
          "arn:aws:glue:${var.region}:${local.account_id}:catalog",
          aws_glue_catalog_database.dora.arn,
          "arn:aws:glue:${var.region}:${local.account_id}:table/${local.glue_database}/*",
        ]
      },
      {
        Sid      = "ReadEventsWriteResults"
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation", "s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
        Resource = [aws_s3_bucket.data.arn, "${aws_s3_bucket.data.arn}/*"]
      },
      {
        Sid      = "ReadSlackWebhook"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:${var.region}:${local.account_id}:parameter${var.slack_webhook_parameter}"
      },
      {
        Sid       = "DecryptSlackWebhook"
        Effect    = "Allow"
        Action    = ["kms:Decrypt"]
        Resource  = "*"
        Condition = { StringEquals = { "kms:ViaService" = "ssm.${var.region}.amazonaws.com" } }
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "leaderboard" {
  name              = "/aws/lambda/${var.name}-leaderboard"
  retention_in_days = 14
}

resource "aws_lambda_function" "leaderboard" {
  function_name    = "${var.name}-leaderboard"
  role             = aws_iam_role.leaderboard.arn
  runtime          = "python3.12"
  handler          = "leaderboard.handler"
  filename         = data.archive_file.leaderboard.output_path
  source_code_hash = data.archive_file.leaderboard.output_base64sha256
  timeout          = 120
  memory_size      = 256

  environment {
    variables = {
      WINDOW_DAYS         = tostring(var.window_days)
      ATHENA_WORKGROUP    = aws_athena_workgroup.dora.name
      GLUE_DATABASE       = aws_glue_catalog_database.dora.name
      GLUE_TABLE          = aws_glue_catalog_table.events.name
      SLACK_WEBHOOK_PARAM = var.slack_webhook_parameter
    }
  }

  depends_on = [aws_cloudwatch_log_group.leaderboard, aws_iam_role_policy_attachment.leaderboard_logs]
}

resource "aws_cloudwatch_event_rule" "weekly" {
  name                = "${var.name}-weekly-leaderboard"
  description         = "Post the DORA team leaderboard to Slack"
  schedule_expression = var.schedule
}

resource "aws_cloudwatch_event_target" "weekly" {
  rule = aws_cloudwatch_event_rule.weekly.name
  arn  = aws_lambda_function.leaderboard.arn
}

resource "aws_lambda_permission" "events" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.leaderboard.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.weekly.arn
}
