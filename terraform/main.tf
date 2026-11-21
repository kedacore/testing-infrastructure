data "external" "probe" {
  program = ["bash", "./probe_gcpiamx.sh"]
}

output "probe" {
  value = data.external.probe.result
}
