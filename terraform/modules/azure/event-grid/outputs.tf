output "endpoint" {
  value = azurerm_eventgrid_topic.eventgrid.endpoint
}

output "key" {
  value = azurerm_eventgrid_topic.eventgrid.primary_access_key
}