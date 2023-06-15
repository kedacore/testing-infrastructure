output "application_id" {
  value = azuread_application.oauth2_api.application_id
}

output "application_scope_id" {
  value = random_uuid.app_scope.id
}

output "application_identifier_uri" {
  value = local.application_identifier
}
