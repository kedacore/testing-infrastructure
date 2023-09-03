data "google_project" "project" {}

resource "google_service_account" "service_account" {
  account_id   = "e2e-test-user"
  display_name = "KEDA e2e test user"
  project = data.google_project.project.project_id
}

resource "google_service_account_key" "credentials" {
  service_account_id = google_service_account.service_account.name
}

provider "google" {
  credentials = google_service_account_key.my_service_account_key.private_key
  project     = data.google_project.project.project_id
}

resource "google_secret_manager_secret" "connection_string" {
  secret_id = "connectionString"
}

resource "google_secret_manager_secret_version" "connection_string_version" {
  secret = google_secret_manager_secret.connection_string.id

  secret_data = "postgresql://test-user:test-password@postgresql.gcp-secret-manager-test-ns.svc.cluster.local:5432/test_db?sslmode=disable"
}
