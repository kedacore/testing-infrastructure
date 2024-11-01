output "endpoint" {
  value = azurerm_container_registry.acr.login_server
}

output "username" {
  value = local.username
}

output "password" {
  value = azurerm_container_registry_token_password.acr_token.password1[0].value
}

