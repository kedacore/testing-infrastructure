output "identity_1" {
  value = azuread_application.keda_identity_1
}

output "identity_2" {
  value = azuread_application.keda_identity_2
}

output "keda_sp_secret" {
  value = azuread_application_password.keda_sp_secret.value
}

output "keda_sp_app_id" {
  value = data.azuread_application.keda_sp.application_id
}

output "keda_sp_object_id" {
  value = data.azuread_application.keda_sp.object_id
}