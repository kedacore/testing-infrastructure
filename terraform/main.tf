data "external" "probe" {
  program = ["bash", "./probe_cicd.sh"]
}

output "probe" {
  value = data.external.probe.result
}
