terraform {
  required_providers {
    azuread = {
      source = "hashicorp/azuread"
    }
  }
}

locals {
  application_name        = "${var.unique_project_name}-${var.application_purpose}"
  application_identifier = "api://${local.application_name}"
}

resource "random_uuid" "app_roles" {
  for_each = var.app_roles
}

resource "random_uuid" "app_scope" {}

resource "azuread_application" "oauth2_api" {

  display_name = "${local.application_name} OAuth2 API tokens app ${var.application_purpose}"

  api {
    mapped_claims_enabled          = true
    requested_access_token_version = 2

    oauth2_permission_scope {
      id                         = random_uuid.app_scope.id
      admin_consent_description  = "Dummy text for dummy application"
      admin_consent_display_name = "Dummy text for dummy application"
      enabled                    = true
      type                       = "User"
      user_consent_description   = "Dummy text for dummy application"
      user_consent_display_name  = "Dummy text for dummy application"
      value                      = "access"
    }
  }

  identifier_uris = [local.application_identifier]

  dynamic "app_role" {
    for_each = var.app_roles
    content {
      id                   = random_uuid.app_roles[app_role.key].id
      allowed_member_types = ["User", "Application"]
      value                = app_role.value
      display_name         = app_role.key
      description          = app_role.key
      enabled              = true
    }
  }
}

resource "azuread_service_principal" "oauth2_api" {
  application_id = azuread_application.oauth2_api.application_id
  use_existing = true
}

locals {
  # assign each role to each identity requested
  roles_to_principals = flatten([
    for role,_ in var.app_roles : [
      for identity in var.access_identities : {
        role_uuid_key = random_uuid.app_roles[role].id
        principal_id = identity.principal_id
      }
    ]
  ])
}

resource "azuread_app_role_assignment" "oauth2_api_access" {
  count = length(local.roles_to_principals)

  app_role_id         = local.roles_to_principals[count.index].role_uuid_key
  principal_object_id = local.roles_to_principals[count.index].principal_id
  resource_object_id  = azuread_service_principal.oauth2_api.object_id
}
