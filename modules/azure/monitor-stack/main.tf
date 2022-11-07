provider "azurerm" {
  features {}
  skip_provider_registration = true
}

locals {
  app_insights_name            = "${var.unique_project_name}-app-insights"
  log_analytics_workspace_name = "${var.unique_project_name}-log-analytics"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

resource "azurerm_log_analytics_workspace" "workspace" {
  name                = local.log_analytics_workspace_name
  location            = data.azurerm_resource_group.rg.location
  resource_group_name = data.azurerm_resource_group.rg.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = var.tags
}

resource "azurerm_application_insights" "insights" {
  name                = local.app_insights_name
  location            = data.azurerm_resource_group.rg.location
  resource_group_name = data.azurerm_resource_group.rg.name
  workspace_id        = azurerm_log_analytics_workspace.workspace.id
  application_type    = "web"
  tags                = var.tags
}
