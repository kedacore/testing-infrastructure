output "tenant_id" {
  value = data.azurerm_client_config.current.tenant_id
}

output "subscription_id" {
  value = data.azurerm_client_config.current.subscription_id
}

output "object_id" {
  value = data.azurerm_client_config.current.subscription_id
}