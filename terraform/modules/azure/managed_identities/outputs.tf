output "identity_1" {
  value = azurerm_user_assigned_identity.keda_identity_1
}

output "identity_2" {
  value = azurerm_user_assigned_identity.keda_identity_2
}

output "postgres_identity" {
  value = azurerm_user_assigned_identity.postgres_identity
}