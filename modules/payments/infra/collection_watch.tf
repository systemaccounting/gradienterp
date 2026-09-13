# The collection watch: a charge succeeded, so a webhook is now OWED.
#
# `charge_saved_method` returns when the processor accepts the card. The books move only when
# `charge.succeeded` arrives and `ingest_stripe` settles the invoice — and if that delivery never
# happens, nothing anywhere fails. `ingest_stripe` went three months without an invocation because
# its endpoint was registered in test mode while charges ran live, and it was found by running a
# charge on purpose.
#
# The trigger is a DELAYED MESSAGE, and the message is the whole pending record: nothing is written
# when a charge succeeds, there is nothing for the webhook to delete, and nothing to scan. A
# collection that settles normally means the delayed copy arrives to find the invoice already paid.
#
# A table cannot do this. Streams fire on write immediately, and TTL — the only delayed removal — is
# documented as "typically within 48 hours", a cleanup mechanism rather than a timer. It would still
# need a timer beside it, and the only ones left are a poll or a scheduler entry per charge.

resource "aws_sqs_queue" "collection_watch_dlq" {
  name                      = "${local.prefix}-collection-watch-dlq"
  tags                      = { "gerp:layer" = "operational" }
  message_retention_seconds = 1209600 # 14d, the maximum — a lost watch is worth keeping to look at
}

resource "aws_sqs_queue" "collection_watch" {
  name = "${local.prefix}-collection-watch"
  tags = { "gerp:layer" = "operational" }

  # Long enough that a redrive is a real retry rather than a second concurrent check of the same
  # invoice; the consumer only reads an invoice and prints, so it finishes in well under this.
  visibility_timeout_seconds = 120
  message_retention_seconds  = 86400

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.collection_watch_dlq.arn
    # Three tries. The consumer raising means the invoice read failed, which is transient or
    # permanent; either way a fourth attempt says nothing a human reading the DLQ would not.
    maxReceiveCount = 3
  })
}

# The group is the lambda module's now (every function owns one, created before the first invoke);
# automation's subscription filter attaches to it BY NAME, which is why it exists before the apply.
moved {
  from = aws_cloudwatch_log_group.check_collection
  to   = module.fn["check_collection"].aws_cloudwatch_log_group.this
}

resource "aws_lambda_event_source_mapping" "collection_watch" {
  event_source_arn = aws_sqs_queue.collection_watch.arn
  function_name    = module.fn["check_collection"].arn

  # One at a time. A batch would mean one unreadable invoice re-driving its healthy neighbours,
  # and the volume here is one message per collection — there is nothing to amortise.
  batch_size = 1
}

resource "aws_iam_role_policy" "collection_watch" {
  name = "${local.prefix}-collection-watch"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # send: charge_saved_method enqueueing the watch. receive/delete: the event source mapping
        # reading it. Both roles are this one role, which is why they are one statement.
        Effect = "Allow"
        Action = ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.collection_watch.arn
      },
    ]
  })
}
