provider "azurerm" {
  features {}
}

data "azuread_application" "keda_app" {
  display_name = var.keda_sp_name
}

data "azuread_service_principal" "keda_sp" {
  application_id = data.azuread_application.keda_app.application_id
}

resource "azuread_application_password" "keda_app_secret" {
  display_name          = "e2e tests secret"
  application_object_id = data.azuread_application.keda_app.id
}

resource "azuread_application" "keda_identity_1" {
  display_name = "keda-e2e-test-identity-1"
}

resource "azuread_application" "keda_identity_2" {
  display_name = "keda-e2e-test-identity-2"
}

resource "azuread_service_principal" "keda_identity_1_sp" {
  application_id = azuread_application.keda_identity_1.application_id
}

resource "azuread_service_principal" "keda_identity_2_sp" {
  application_id = azuread_application.keda_identity_2.application_id
}