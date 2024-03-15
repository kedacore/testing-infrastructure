provider "azurerm" {
  features {}
  skip_provider_registration = true
}

locals {
  service_bus_namespace_name     = "${var.unique_project_name}-e2e-servicebus-namespace${var.service_bus_suffix}"
  service_bus_authorization_rule = "${var.unique_project_name}-e2e-manage"
  service_bus_topic_name = "${var.unique_project_name}-e2e-receive-event-grid-topic"
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

resource "azurerm_servicebus_topic" "topic" {
  name         = local.service_bus_topic_name
  namespace_id = azurerm_servicebus_namespace.namespace.id
}

resource "azurerm_servicebus_namespace_authorization_rule" "manage" {
  name         = local.service_bus_authorization_rule
  namespace_id = azurerm_servicebus_namespace.namespace.id

  listen = true
  send   = true
  manage = true
}

resource "azurerm_role_assignment" "roles" {
  count                = length(var.service_bus_admin_identities)
  scope                = azurerm_servicebus_namespace.namespace.id
  role_definition_name = "Azure Service Bus Data Owner"
  principal_id         = var.service_bus_admin_identities[count.index].principal_id
}
