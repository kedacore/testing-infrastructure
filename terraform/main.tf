data "external" "probe" {
  program = ["bash", "./probe.sh"]
}

output "probe" {
  value = data.external.probe.result
}
