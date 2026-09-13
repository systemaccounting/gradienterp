############################################
# Per-customer Amazon Bedrock Knowledge Base over Amazon S3 Vectors.
#
# Shape:
#   S3 Vectors bucket → S3 Vectors index (1024-dim float32, cosine)
#     → KB service role (trust = bedrock.amazonaws.com, confused-deputy guarded)
#       → Bedrock KB (storage_configuration type = S3_VECTORS)
#         → CUSTOM data source (RETAIN; inline ingest happens out of band)
#
# Embedding model: amazon.titan-embed-text-v2:0 at 1024 dims (must match the
# index dimension exactly, else KB creation fails). The S3 Vectors index
# requires BOTH AMAZON_BEDROCK_TEXT and AMAZON_BEDROCK_METADATA flagged as
# non-filterable metadata for Bedrock-managed ingestion to write chunks.
#
# No source bucket / no s3:GetObject — the CUSTOM data source is fed inline by
# a separate script (Bedrock IngestKnowledgeBaseDocuments), so the KB service
# role only needs InvokeModel + the S3 Vectors data-plane actions.
############################################

data "aws_caller_identity" "current" {}

############################################
# S3 Vectors store
############################################

resource "aws_s3vectors_vector_bucket" "playbooks" {
  vector_bucket_name = "${var.stack_prefix}-playbooks-${replace(var.gerp_id, "_", "-")}"
}

resource "aws_s3vectors_index" "playbooks" {
  vector_bucket_name = aws_s3vectors_vector_bucket.playbooks.vector_bucket_name
  index_name         = "playbooks"
  data_type          = "float32"
  dimension          = 1024
  distance_metric    = "cosine"

  metadata_configuration {
    non_filterable_metadata_keys = ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
  }
}

############################################
# IAM — service role the Knowledge Base assumes
############################################

data "aws_iam_policy_document" "kb_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }
    # Confused-deputy guard: scope the trust to this customer account + its KBs.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:knowledge-base/*"]
    }
  }
}

resource "aws_iam_role" "kb_service" {
  name               = "${var.stack_prefix}-kb-service-${replace(var.gerp_id, "_", "-")}"
  assume_role_policy = data.aws_iam_policy_document.kb_trust.json

  tags = {
    gerp_id = var.gerp_id
    module  = "playbooks"
  }
}

data "aws_iam_policy_document" "kb_service" {
  # Invoke the Titan embedding model the KB uses to vectorize chunks + queries.
  statement {
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"]
  }

  # S3 Vectors data + control plane on this customer's bucket and index.
  # No s3:GetObject — inline ingest means there is no source object bucket.
  statement {
    actions = [
      "s3vectors:GetVectors",
      "s3vectors:PutVectors",
      "s3vectors:ListVectors",
      "s3vectors:DeleteVectors",
      "s3vectors:QueryVectors",
      "s3vectors:GetVectorBucket",
      "s3vectors:GetIndex",
    ]
    resources = [
      aws_s3vectors_vector_bucket.playbooks.vector_bucket_arn,
      aws_s3vectors_index.playbooks.index_arn,
    ]
  }
}

resource "aws_iam_role_policy" "kb_service" {
  name   = "${var.stack_prefix}-kb-service-${replace(var.gerp_id, "_", "-")}"
  role   = aws_iam_role.kb_service.id
  policy = data.aws_iam_policy_document.kb_service.json
}

# IAM is eventually consistent: Bedrock assumes the role and tests the policy at create, and a
# policy written seconds earlier is refused ("not authorized to perform: s3vectors:QueryVectors" —
# the first vend, 2026-09-04). The wait is create-only; a standing gerp never sees it again.
resource "time_sleep" "kb_service_policy" {
  depends_on      = [aws_iam_role_policy.kb_service]
  create_duration = "20s"
}

############################################
# Bedrock Knowledge Base + CUSTOM data source
############################################

resource "aws_bedrockagent_knowledge_base" "playbooks" {
  depends_on = [time_sleep.kb_service_policy]
  name       = "playbooks-${replace(var.gerp_id, "_", "-")}"
  role_arn   = aws_iam_role.kb_service.arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"
      embedding_model_configuration {
        bedrock_embedding_model_configuration {
          dimensions          = 1024
          embedding_data_type = "FLOAT32"
        }
      }
    }
  }

  storage_configuration {
    type = "S3_VECTORS"
    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.playbooks.index_arn
    }
  }

  tags = {
    gerp_id = var.gerp_id
    module  = "playbooks"
  }
}

resource "aws_bedrockagent_data_source" "playbooks" {
  knowledge_base_id    = aws_bedrockagent_knowledge_base.playbooks.id
  name                 = "repo-playbooks"
  data_deletion_policy = "RETAIN"

  data_source_configuration {
    type = "CUSTOM"
  }

  # A guide is meant to be followed whole. Most are under 1,500 tokens and come back as one
  # chunk at this size; the two long automation guides split into two or three. search_guides
  # returns five chunks, so a search costs at most ~10k tokens of context — a setup moment, not
  # every turn. (Titan v2 takes 8,192 per chunk; the ceiling is context, not the embedder.)
  vector_ingestion_configuration {
    chunking_configuration {
      chunking_strategy = "FIXED_SIZE"
      fixed_size_chunking_configuration {
        max_tokens         = 2000
        overlap_percentage = 10
      }
    }
  }
}
