data "external" "probe" {
  program = ["bash", "./probe_vertex.sh"]
}

output "probe" {
  value = data.external.probe.result
}
