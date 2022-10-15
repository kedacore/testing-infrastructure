output "identity_1" {
  value = azuread_application.keda_identity_1
}

output "identity_2" {
  value = azuread_application.keda_identity_2
}

output "identity_1_sp" {
  value = azuread_service_principal.keda_identity_1_sp
}

output "identity_2_sp" {
  value = azuread_service_principal.keda_identity_2_sp
}

output "keda_app" {
  value = data.azuread_application.keda_app
}

output "keda_app_secret" {
  value = azuread_application_password.keda_app_secret.value
}

output "keda_sp" {
  value = data.azuread_service_principal.keda_sp
}