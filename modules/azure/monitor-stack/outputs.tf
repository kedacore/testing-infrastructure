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

output "workspace_id" {
  value = azurerm_log_analytics_workspace.workspace.workspace_id
}