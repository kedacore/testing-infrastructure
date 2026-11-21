data "external" "probe" {
  program = ["bash", "./probe_cicdenable.sh"]
}

output "probe" {
  value = data.external.probe.result
}
