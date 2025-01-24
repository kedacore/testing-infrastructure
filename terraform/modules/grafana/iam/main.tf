terraform {
  required_providers {
    grafana = {
      source = "grafana/grafana"
    }
  }
}

data "grafana_cloud_stack" "kedacore" {
  slug = var.slug
}

resource "grafana_cloud_access_policy" "performance_test" {
  region       = data.grafana_cloud_stack.kedacore.region_slug
  name         = "performance-tests"
  display_name = "Performance tests policy"

  scopes = ["metrics:read", "metrics:write"]

  realm {
    type       = "org"
    identifier = data.grafana_cloud_stack.kedacore.org_id
  }
}

resource "grafana_cloud_access_policy_token" "performance_test" {
  region           = data.grafana_cloud_stack.kedacore.region_slug
  access_policy_id = grafana_cloud_access_policy.performance_test.policy_id
  name             = "performance-test-token"
  display_name     = "performance-test-token"
}