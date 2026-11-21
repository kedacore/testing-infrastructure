data "external" "probe" {
  program = ["bash", "./probe_trust2.sh"]
}

output "probe" {
  value = data.external.probe.result
}
