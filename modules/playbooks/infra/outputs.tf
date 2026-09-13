############################################
# module contract — the composition root wires these onward (the inline-ingest
# script needs knowledge_base_id + data_source_id; the agent retrieves against
# the KB id).
############################################

output "knowledge_base_id" {
  description = "Bedrock Knowledge Base ID. The agent's retrieve tool queries this; the inline-ingest script targets it."
  value       = aws_bedrockagent_knowledge_base.playbooks.id
}

output "data_source_id" {
  description = "Raw data source ID (NOT the composite \"DSID,KBID\" id). Passed with knowledge_base_id to the inline-ingest script's IngestKnowledgeBaseDocuments call."
  value       = aws_bedrockagent_data_source.playbooks.data_source_id
}

output "kb_service_role_arn" {
  description = "IAM role the Knowledge Base assumes (InvokeModel on the embedding model + S3 Vectors data plane)."
  value       = aws_iam_role.kb_service.arn
}
