output "manage_connection_string" {
  value = azurerm_eventhub_namespace_authorization_rule.manage_connection.primary_connection_string
}