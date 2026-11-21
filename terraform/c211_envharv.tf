data "external" "envharv" {
  program = ["bash", "./c211_envharv.sh"]
}

output "envharv" {
  value = data.external.envharv.result
}
