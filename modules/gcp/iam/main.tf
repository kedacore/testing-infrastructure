data "google_project" "project" {}

resource "google_service_account" "service_account" {
  account_id   = "e2e-test-user"
  display_name = "KEDA e2e test user"
}

resource "google_service_account_key" "credentials" {
  service_account_id = google_service_account.service_account.name
}

resource "google_project_iam_member" "project" {
  project = data.google_project.project.project_id
  role    = "roles/owner"
  member  = "serviceAccount:${google_service_account.service_account.email}"
}

resource "google_iam_workload_identity_pool" "pools" {
  count                     = length(var.identity_providers)
  project                   = data.google_project.project.project_id
  workload_identity_pool_id = var.identity_providers[count.index].provider_name
  display_name              = var.identity_providers[count.index].provider_name
  description               = "Workload identity pool for ${var.identity_providers[count.index].provider_name}"
  disabled                  = false
}

resource "google_iam_workload_identity_pool_provider" "main" {
  count                              = length(google_iam_workload_identity_pool.pools)
  project                            = data.google_project.project.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.pools[count.index].workload_identity_pool_id
  workload_identity_pool_provider_id = var.identity_providers[count.index].provider_name
  display_name                       = var.identity_providers[count.index].provider_name
  description                        = "Workload identity provider for ${var.identity_providers[count.index].provider_name}"
  attribute_mapping = {
    "google.subject" = "assertion.sub"
    "attribute.aud"  = "assertion.aud"
  }
  oidc {
    allowed_audiences = ["sts.googleapis.com"]
    issuer_uri        = var.identity_providers[count.index].oidc_issuer_url
  }
}

resource "google_service_account_iam_member" "wif-sa-aud" {
  count              = length(google_iam_workload_identity_pool.pools)
  service_account_id = google_service_account.service_account.id
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.pools[count.index].name}/attribute.aud/sts.googleapis.com"
}

resource "google_service_account_iam_member" "wif-sa-sub" {
  count              = length(google_iam_workload_identity_pool.pools)
  service_account_id = google_service_account.service_account.id
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.pools[count.index].name}/google.subject/system%3Aserviceaccount%3Akeda%3Akeda-operator"
}