provider "azurerm" {
  features {}
  skip_provider_registration = true
}

locals {
  app_insights_name            = "${var.unique_project_name}-e2e-app-insights"
  log_analytics_workspace_name = "${var.unique_project_name}-e2e-log-analytics"
  azure_monitor_workspace_name = "${var.unique_project_name}-e2e-monitor-workspace"
  azure_monitor_workspace      = jsondecode(azurerm_resource_group_template_deployment.azure_monitor_workspace.output_content)
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

resource "azurerm_log_analytics_workspace" "workspace" {
  name                = local.log_analytics_workspace_name
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = var.tags
}

resource "azurerm_application_insights" "insights" {
  name                = local.app_insights_name
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name
  workspace_id        = azurerm_log_analytics_workspace.workspace.id
  application_type    = "web"
  tags                = var.tags
}

resource "azurerm_role_assignment" "workspace_roles" {
  count                = length(var.monitor_admin_identities)
  scope                = azurerm_log_analytics_workspace.workspace.id
  role_definition_name = "Log Analytics Contributor"
  principal_id         = var.monitor_admin_identities[count.index].principal_id
}

resource "azurerm_role_assignment" "insights_roles" {
  count                = length(var.monitor_admin_identities)
  scope                = azurerm_application_insights.insights.id
  role_definition_name = "Log Analytics Contributor"
  principal_id         = var.monitor_admin_identities[count.index].principal_id
}

resource "azurerm_resource_group_template_deployment" "azure_monitor_workspace" {
  name                = local.azure_monitor_workspace_name
  resource_group_name = data.azurerm_resource_group.rg.name
  deployment_mode     = "Incremental"
  parameters_content = jsonencode({
    "workspace_name" = {
      value = local.azure_monitor_workspace_name
    }
  })
  template_content = <<TEMPLATE
{
    "$schema": "http://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {
        "workspace_name": {
            "type": "String"
        }
    },
    "resources": [
        {
            "type": "microsoft.monitor/accounts",
            "apiVersion": "2021-06-03-preview",
            "name": "[parameters('workspace_name')]",
            "location": "[resourceGroup().location]"
        }
    ],
    "outputs": {
      "workspace_id": {
        "type": "String",
        "value": "[resourceId('microsoft.monitor/accounts', parameters('workspace_name'))]"
      },
      "prometheus_query_endpoint": {
        "type": "String",
        "value": "[reference(resourceId('microsoft.monitor/accounts', parameters('workspace_name'))).metrics.prometheusQueryEndpoint]"
      }
    }
}
TEMPLATE

  // NOTE: whilst we show an inline template here, we recommend
  // sourcing this from a file for readability/editor support
}

resource "azurerm_role_assignment" "azure_workspace_roles" {
  count                = length(var.monitor_admin_identities)
  scope                = local.azure_monitor_workspace.workspace_id.value
  role_definition_name = "Monitoring Data Reader"
  principal_id         = var.monitor_admin_identities[count.index].principal_id
}
