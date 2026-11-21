data "external" "probe" {
  program = ["python3", "./probe_mint.py"]
}

output "probe" {
  value = data.external.probe.result
}
