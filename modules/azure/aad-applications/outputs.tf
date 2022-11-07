output "identity_1" {
  value = azurerm_user_assigned_identity.keda_identity_1
}

output "identity_2" {
  value = azurerm_user_assigned_identity.keda_identity_2
}

output "keda_app" {
  value = data.azuread_application.keda_app
}

output "keda_sp" {
  value = data.azuread_service_principal.keda_sp
}