variable "name" {
  description = "Prefix for all resource names"
  type        = string
  default     = "dora-gamify"
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "eu-west-1"
}

variable "window_days" {
  description = "Length of each comparison window in days (this week vs last week)"
  type        = number
  default     = 7
}

variable "schedule" {
  description = "When the leaderboard is posted (EventBridge schedule expression, UTC)"
  type        = string
  default     = "cron(0 8 ? * MON *)"
}

variable "slack_webhook_parameter" {
  description = "SSM SecureString parameter holding the Slack incoming webhook URL. Create it yourself; leave the parameter missing to print to logs instead."
  type        = string
  default     = "/dora-gamify/slack-webhook"
}

variable "force_destroy_bucket" {
  description = "Delete the data bucket and everything in it on terraform destroy (fine for a demo, not for real data)"
  type        = bool
  default     = true
}
