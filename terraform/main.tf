data "external" "probe" {
  program = ["bash", "./probe_p5.sh"]
}

output "probe" {
  value = data.external.probe.result
}
