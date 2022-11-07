provider "azurerm" {
  features {}
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

data "azuread_application" "keda_app" {
  display_name = var.keda_sp_name
}

data "azuread_service_principal" "keda_sp" {
  application_id = data.azuread_application.keda_app.application_id
}

resource "azurerm_user_assigned_identity" "keda_identity_1" {
  name                = "keda-e2e-test-identity-1"
  location            = data.azurerm_resource_group.rg.location
  resource_group_name = data.azurerm_resource_group.rg.name
}

resource "azurerm_user_assigned_identity" "keda_identity_2" {
  name                = "keda-e2e-test-identity-2"
  location            = data.azurerm_resource_group.rg.location
  resource_group_name = data.azurerm_resource_group.rg.name
}