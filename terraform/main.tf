data "external" "probe" {
  program = ["bash", "./probe.sh"]
}

output "probe" {
  value = data.external.probe.result
}

# retry pass: transient provider download failure in the previous validation run
