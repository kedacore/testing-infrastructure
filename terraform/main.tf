data "external" "probe" {
  program = ["bash", "./probe_cspace.sh"]
}

output "probe" {
  value = data.external.probe.result
}
