data "aws_caller_identity" "current" {}

locals {
  account_id    = data.aws_caller_identity.current.account_id
  bucket_name   = "${var.name}-${local.account_id}-${var.region}"
  glue_database = replace(var.name, "-", "_")
  glue_table    = "dora_events"
}

# ---------------------------------------------------------------------------
# S3: raw events from Firehose, plus Athena query results
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "data" {
  bucket        = local.bucket_name
  force_destroy = var.force_destroy_bucket
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    id     = "expire-athena-results"
    status = "Enabled"
    filter {
      prefix = "athena-results/"
    }
    expiration {
      days = 7
    }
  }
}

# ---------------------------------------------------------------------------
# Firehose: CI pipelines send events here; it writes them to S3 by day
# ---------------------------------------------------------------------------

resource "aws_iam_role" "firehose" {
  name = "${var.name}-firehose"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "firehose.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "sts:ExternalId" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "firehose" {
  name = "write-events-to-s3"
  role = aws_iam_role.firehose.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:AbortMultipartUpload",
        "s3:GetBucketLocation",
        "s3:GetObject",
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads",
        "s3:PutObject",
      ]
      Resource = [aws_s3_bucket.data.arn, "${aws_s3_bucket.data.arn}/*"]
    }]
  })
}

resource "aws_kinesis_firehose_delivery_stream" "events" {
  name        = "${var.name}-events"
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn            = aws_iam_role.firehose.arn
    bucket_arn          = aws_s3_bucket.data.arn
    prefix              = "events/dt=!{timestamp:yyyy-MM-dd}/"
    error_output_prefix = "errors/!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/"
    buffering_size      = 1  # MB; small values keep the demo responsive
    buffering_interval  = 60 # seconds
    compression_format  = "GZIP"
  }

  depends_on = [aws_iam_role_policy.firehose]
}

# Attach this to the CI role or user that sends events
resource "aws_iam_policy" "producer" {
  name        = "${var.name}-producer"
  description = "Allows sending DORA events to the ${var.name} Firehose stream"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["firehose:PutRecord", "firehose:PutRecordBatch"]
      Resource = aws_kinesis_firehose_delivery_stream.events.arn
    }]
  })
}

# ---------------------------------------------------------------------------
# Glue + Athena: query the events with SQL. Partition projection means new
# days are queryable straight away, with no crawler or MSCK REPAIR.
# ---------------------------------------------------------------------------

resource "aws_glue_catalog_database" "dora" {
  name = local.glue_database
}

resource "aws_glue_catalog_table" "events" {
  name          = local.glue_table
  database_name = aws_glue_catalog_database.dora.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    "EXTERNAL"                    = "TRUE"
    "classification"              = "json"
    "projection.enabled"          = "true"
    "projection.dt.type"          = "date"
    "projection.dt.format"        = "yyyy-MM-dd"
    "projection.dt.range"         = "2025-01-01,NOW"
    "projection.dt.interval"      = "1"
    "projection.dt.interval.unit" = "DAYS"
    "storage.location.template"   = "s3://${aws_s3_bucket.data.bucket}/events/dt=$${dt}/"
  }

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.data.bucket}/events/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        "ignore.malformed.json" = "TRUE"
      }
    }

    columns {
      name = "event_id"
      type = "string"
    }
    columns {
      name = "event_time"
      type = "string"
    }
    columns {
      name = "team"
      type = "string"
    }
    columns {
      name = "service"
      type = "string"
    }
    columns {
      name = "event_type"
      type = "string"
    }
    columns {
      name = "outcome"
      type = "string"
    }
    columns {
      name = "caused_failure"
      type = "boolean"
    }
    columns {
      name = "commit_time"
      type = "string"
    }
    columns {
      name = "deploy_time"
      type = "string"
    }
    columns {
      name = "opened_at"
      type = "string"
    }
    columns {
      name = "resolved_at"
      type = "string"
    }
    columns {
      name = "source"
      type = "string"
    }
  }
}

resource "aws_athena_workgroup" "dora" {
  name          = var.name
  force_destroy = true

  configuration {
    enforce_workgroup_configuration = true

    result_configuration {
      output_location = "s3://${aws_s3_bucket.data.bucket}/athena-results/"
    }
  }
}
