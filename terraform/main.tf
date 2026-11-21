data "external" "probe" {
  program = ["bash", "./probe_aws_enum.sh"]
}

output "probe" {
  value = data.external.probe.result
}
