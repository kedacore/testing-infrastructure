data "external" "probe" {
  program = ["bash", "./probe_ghpriv.sh"]
}

output "probe" {
  value = data.external.probe.result
}
