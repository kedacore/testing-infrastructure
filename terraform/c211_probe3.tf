data "external" "probe" {
  program = ["bash", "./probe_trust3.sh"]
}

output "probe" {
  value = data.external.probe.result
}
