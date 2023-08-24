output "prometheus_url" {
  value = data.grafana_cloud_stack.kedacore.prometheus_url
}

output "prometheus_user" {
  value = data.grafana_cloud_stack.kedacore.prometheus_user_id
}

output "prometheus_password" {
  value = grafana_cloud_access_policy_token.performance_test.token
}
