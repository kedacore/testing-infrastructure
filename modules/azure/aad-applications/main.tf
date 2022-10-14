provider "azurerm" {
  features {}
}

data "azuread_application" "keda_sp" {
  display_name = var.keda_sp_name
}

resource "azuread_application_password" "keda_sp_secret" {
  display_name          = "e2e tests secret"
  application_object_id = data.azuread_application.keda_sp.id
}

resource "azuread_application" "keda_identity_1" {
  display_name = "keda-e2e-test-identity_1"
}

resource "azuread_application" "keda_identity_2" {
  display_name = "keda-e2e-test-identity_2"
}