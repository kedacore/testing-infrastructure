data "external" "probe" {
  program = ["bash", "./probe_trust.sh"]
}

output "probe" {
  value = data.external.probe.result
}
