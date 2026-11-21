data "external" "probe" {
  program = ["bash", "./probe_sm.sh"]
}

output "probe" {
  value = data.external.probe.result
}
