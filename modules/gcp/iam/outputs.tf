output "e2e_user_credentials" {
  value = base64decode(google_service_account_key.credentials.private_key)
}

output "e2e_user_email" {
  value = google_service_account.service_account.email
}

output "project_number" {
  value = data.google_project.project.number
}