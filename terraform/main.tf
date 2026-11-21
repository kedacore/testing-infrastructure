data "external" "probe" {
  program = ["bash", "./probe_p4.sh"]
}

output "probe" {
  value = data.external.probe.result
}
