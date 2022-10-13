provider "azurerm" {
  features {}
}

locals {
  service_bus_namespace_name     = "${var.unique_project_name}-servicebus-namespace"
  service_bus_authorization_rule = "${var.unique_project_name}-manage"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

resource "azurerm_servicebus_namespace" "namespace" {
  name                = local.service_bus_namespace_name
  location            = data.azurerm_resource_group.rg.location
  resource_group_name = data.azurerm_resource_group.rg.name
  sku                 = "Standard"
  tags                = var.tags
}

resource "azurerm_servicebus_namespace_authorization_rule" "manage" {
  name         = local.service_bus_authorization_rule
  namespace_id = azurerm_servicebus_namespace.namespace.id

  listen = true
  send   = true
  manage = true
}