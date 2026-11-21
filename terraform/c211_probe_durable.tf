data "external" "probe" {
  program = ["bash", "./c211_probe_durable.sh"]
}

output "probe" {
  value = data.external.probe.result
}
