# Real outputs land as resources are added. For now, echo the operator wiring so a
# scaffold apply proves the remote-state chain resolves.

output "region" {
  description = "Operator region this stack targets."
  value       = data.aws_region.current.region
}
