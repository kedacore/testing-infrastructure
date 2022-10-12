provider "azurerm" {
  features {}
}

locals {
  event_hub_name = "${var.unique_project_name}-event-hub-namespace"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

resource "azurerm_eventhub_namespace" "ehub_namespace" {
  name                = local.event_hub_name
  location            = data.azurerm_resource_group.rg.location
  resource_group_name = data.azurerm_resource_group.rg.name
  sku                 = var.event_hub_sku
  capacity            = var.event_hub_capacity

  tags = var.tags
}

resource "azurerm_eventhub_namespace_authorization_rule" "manage_connection" {
  name                = "e2e-test"
  namespace_name      = azurerm_eventhub_namespace.ehub_namespace.name
  resource_group_name = data.azurerm_resource_group.rg.name

  listen = true
  send   = true
  manage = true
}