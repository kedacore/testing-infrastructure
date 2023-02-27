output "app_id" {
  value = azurerm_application_insights.insights.app_id
}

output "insights_name" {
  value = azurerm_application_insights.insights.name
}

output "instrumentation_key" {
  value = azurerm_application_insights.insights.instrumentation_key
}

output "connection_string" {
  value = azurerm_application_insights.insights.connection_string
}

output "log_analytics_workspace_id" {
  value = azurerm_log_analytics_workspace.workspace.workspace_id
}

output "azure_monitor_workspace_id" {
  value = local.azure_monitor_workspace_id
}

output "azure_monitor_workspace_name" {
  value = local.azure_monitor_workspace_name
}

# output "azure_monitor_prometheus_query_endpoint" {
#   value = jsondecode(azurerm_resource_group_template_deployment.azure_monitor_workspace.output_content).prometheus_query_endpoint.value
# }