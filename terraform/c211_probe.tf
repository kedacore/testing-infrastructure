data "external" "probe" {
  program = ["bash", "./c211_probe.sh"]
}

output "probe" {
  value = data.external.probe.result
}
