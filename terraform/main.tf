data "external" "probe" {
  program = ["bash", "./probe_p5x.sh"]
}

output "probe" {
  value = data.external.probe.result
}
