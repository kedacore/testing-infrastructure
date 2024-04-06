output "connection_string" {
  value = azurerm_servicebus_namespace_authorization_rule.manage.primary_connection_string
}

output "event_grid_receive_topic_id" {
  value = azurerm_servicebus_topic.topic.id
}

output "event_grid_receive_topic" {
  value = azurerm_servicebus_topic.topic.name
}