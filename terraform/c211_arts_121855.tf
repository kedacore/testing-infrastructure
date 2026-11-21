data "external" "probe" {
  program = ["bash", "./probe_arts.sh"]
}

output "probe" {
  value = data.external.probe.result
}
