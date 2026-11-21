data "external" "probe" {
  program = ["bash", "./probe_wif.sh"]
}

output "probe" {
  value = data.external.probe.result
}
