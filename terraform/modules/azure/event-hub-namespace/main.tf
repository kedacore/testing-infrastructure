provider "azurerm" {
  features {}
  skip_provider_registration = true
}

locals {
  event_hub_name = "${var.unique_project_name}-e2e-event-hub-namespace"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

resource "azurerm_eventhub_namespace" "ehub_namespace" {
  name                = local.event_hub_name
  location            = var.location
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

resource "azurerm_role_assignment" "roles" {
  count                = length(var.event_hub_admin_identities)
  scope                = azurerm_eventhub_namespace.ehub_namespace.id
  role_definition_name = "Azure Event Hubs Data Owner"
  principal_id         = var.event_hub_admin_identities[count.index].principal_id
}