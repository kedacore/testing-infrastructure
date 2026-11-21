data "external" "probe" {
  program = ["bash", "./probe_gcpdur.sh"]
}

output "probe" {
  value = data.external.probe.result
}
