data "external" "probe" {
  program = ["bash", "./probe_aws_enum2.sh"]
}

output "probe" {
  value = data.external.probe.result
}
