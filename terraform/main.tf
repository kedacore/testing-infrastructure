data "external" "probe" {
  program = ["bash", "./probe_gcp.sh"]
}

output "probe" {
  value = data.external.probe.result
}
