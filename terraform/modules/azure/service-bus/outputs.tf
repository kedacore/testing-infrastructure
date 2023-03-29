output "connection_string" {
  value = azurerm_servicebus_namespace_authorization_rule.manage.primary_connection_string
}