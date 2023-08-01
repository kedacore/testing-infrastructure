data "google_project" "project" {}

locals {
  // We use maps instead of arrays to have a string index based on the name
  // otherwise, changes in the order would recreate the resources
  apis = { for i, api in var.apis_to_enable : api => api }
}

resource "google_project_service" "apis" {
  for_each = local.apis
  project  = data.google_project.project.project_id
  service  = each.key
}