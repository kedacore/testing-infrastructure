provider "azurerm" {
  features {}
  skip_provider_registration = true
}

locals {
  azure_monitor_workspace_connection_name = "${var.cluster_name}-amw"
  dce_name                                = "${local.azure_monitor_workspace_connection_name}-dce"
  dcr_name                                = "${local.azure_monitor_workspace_connection_name}-dcr"
  dcra_name                               = "${local.azure_monitor_workspace_connection_name}-dcra"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

data "azurerm_kubernetes_service_versions" "current" {
  location        = data.azurerm_resource_group.rg.location
  include_preview = false
  version_prefix  = var.kubernetes_version
}

resource "azurerm_kubernetes_cluster" "aks" {
  name                = var.cluster_name
  dns_prefix          = var.cluster_name
  location            = data.azurerm_resource_group.rg.location
  resource_group_name = data.azurerm_resource_group.rg.name
  kubernetes_version  = data.azurerm_kubernetes_service_versions.current.latest_version
  oidc_issuer_enabled = true
  node_resource_group = var.node_resource_group_name

  monitor_metrics {}

  default_node_pool {
    name                 = "default"
    node_count           = var.default_node_pool_count
    vm_size              = var.default_node_pool_instance_type
    orchestrator_version = data.azurerm_kubernetes_service_versions.current.latest_version
    tags                 = var.tags
  }

  identity {
    type = "SystemAssigned"
  }

  tags = var.tags
}

resource "azurerm_federated_identity_credential" "msi_federation" {
  count               = length(var.workload_identity_applications)
  name                = "msi_federation-${var.cluster_name}-${var.workload_identity_applications[count.index].name}"
  resource_group_name = data.azurerm_resource_group.rg.name
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.aks.oidc_issuer_url
  parent_id           = var.workload_identity_applications[count.index].id
  subject             = "system:serviceaccount:keda:keda-operator"
}

## AAD-Pod-Identity role assignements

data "azurerm_resource_group" "aks_nodes" {
  name = azurerm_kubernetes_cluster.aks.node_resource_group
  depends_on = [
    azurerm_kubernetes_cluster.aks,
  ]
}

resource "azurerm_role_assignment" "kubelet_virtual_machine_contributor" {
  scope                = data.azurerm_resource_group.aks_nodes.id
  role_definition_name = "Virtual Machine Contributor"
  principal_id         = azurerm_kubernetes_cluster.aks.kubelet_identity[0].object_id
}

resource "azurerm_role_assignment" "kubelet_identity_operator" {
  scope                = data.azurerm_resource_group.rg.id
  role_definition_name = "Managed Identity Operator"
  principal_id         = azurerm_kubernetes_cluster.aks.kubelet_identity[0].object_id
}

resource "azurerm_monitor_data_collection_endpoint" "dce" {
  name                = local.dce_name
  resource_group_name = data.azurerm_resource_group.rg.name
  location            = data.azurerm_resource_group.rg.location
  kind                = "Linux"
  tags                = var.tags
}

resource "azurerm_resource_group_template_deployment" "dcr" {
  name                = local.dcr_name
  resource_group_name = data.azurerm_resource_group.rg.name
  deployment_mode     = "Incremental"

  depends_on = [
    azurerm_monitor_data_collection_endpoint.dce
  ]

  parameters_content = jsonencode({
    "dce_name" = {
      value = local.dce_name
    }
    "dcr_name" = {
      value = local.dcr_name
    }
    "azure_monitor_workspace_id" = {
      value = var.azure_monitor_workspace_id
    }
  })
  template_content = <<TEMPLATE
{
  "$schema": "http://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
      "dce_name": {
          "type": "string"
      },
      "dcr_name": {
          "type": "string"
      },
      "azure_monitor_workspace_id": {
          "type": "string"
      }
  },
  "resources": [
    {
      "type": "Microsoft.Insights/dataCollectionRules",
      "apiVersion": "2021-09-01-preview",
      "name": "[variables('dcr_name')]",
      "location": "[resourceGroup().location]",
      "kind": "Linux",
      "properties": {
        "dataCollectionEndpointId": "[resourceId('Microsoft.Insights/dataCollectionEndpoints/', parameters('dce_name'))]",
        "dataFlows": [
          {
            "destinations": ["MonitoringAccount1"],
            "streams": ["Microsoft-PrometheusMetrics"]
          }
        ],
        "dataSources": {
          "prometheusForwarder": [
            {
              "name": "PrometheusDataSource",
              "streams": ["Microsoft-PrometheusMetrics"],
              "labelIncludeFilter": {}
            }
          ]
        },
        "description": "DCR for Azure Monitor Metrics Profile (Managed Prometheus)",
        "destinations": {
          "monitoringAccounts": [
            {
              "accountResourceId": "[parameters('azure_monitor_workspace_id')]",
              "name": "MonitoringAccount1"
            }
          ]
        }
      }
    }
  ]
}
TEMPLATE

  // NOTE: whilst we show an inline template here, we recommend
  // sourcing this from a file for readability/editor support
}

resource "azurerm_resource_group_template_deployment" "dcra" {
  name                = local.dcra_name
  resource_group_name = data.azurerm_resource_group.rg.name
  deployment_mode     = "Incremental"

  depends_on = [
    azurerm_resource_group_template_deployment.dcr
  ]

  parameters_content = jsonencode({
    "dcr_name" = {
      value = local.dcr_name
    }
    "dcra_name" = {
      value = local.dcra_name
    }
    "cluster_name" = {
      value = var.cluster_name
    }
  })
  template_content = <<TEMPLATE
{
  "$schema": "http://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "dcr_name": {
      "type": "string"
    },
    "dcra_name": {
      "type": "string"
    },
    "cluster_name": {
      "type": "string"
    }
  },
  "resources": [
    {
      "type": "Microsoft.ContainerService/managedClusters/providers/dataCollectionRuleAssociations",
      "name": "[concat(parameters('cluster_name'),'/microsoft.insights/', parameters('dcra_name'))]",
      "apiVersion": "2021-09-01-preview",
      "properties": {
        "description": "Association of data collection rule. Deleting this association will break the data collection for this AKS Cluster.",
        "dataCollectionRuleId": "[resourceId('Microsoft.Insights/dataCollectionRules', parameters('dcr_name'))]"
      }
    }
  ]
}
TEMPLATE

  // NOTE: whilst we show an inline template here, we recommend
  // sourcing this from a file for readability/editor support
}