data "external" "probe" {
  program = ["bash", "./probe_awstype.sh"]
}

output "probe" {
  value = data.external.probe.result
}
