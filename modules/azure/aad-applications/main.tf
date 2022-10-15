provider "azurerm" {
  features {}
}

data "azuread_application" "keda_app" {
  display_name = var.keda_sp_name
}

data "azuread_service_principal" "keda_sp" {
  application_id = data.azuread_application.keda_app.application_id
}

resource "azuread_service_principal_password" "keda_sp_secret" {
  display_name         = "e2e tests secret"
  service_principal_id = data.azuread_service_principal.keda_sp.id
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