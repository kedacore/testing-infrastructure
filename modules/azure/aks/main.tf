provider "azurerm" {
  features {}
  skip_provider_registration = true
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

# Terraform doesn't support MSI federation, replace this once it does
resource "azurerm_resource_group_template_deployment" "msi_federation" {
  name                = "msi_federation-${var.cluster_name}-${var.workload_identity_applications[count.index].name}"
  count               = length(var.workload_identity_applications)
  resource_group_name = data.azurerm_resource_group.rg.name

  template_content = <<TEMPLATE
{
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {},
    "resources": [
        {
            "type": "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials",
            "apiVersion": "2022-01-31-preview",
            "name": "${var.workload_identity_applications[count.index].name}/${var.cluster_name}-federation",
            "properties": {
                "issuer": "${azurerm_kubernetes_cluster.aks.oidc_issuer_url}",
                "subject": "system:serviceaccount:keda:keda-operator",
                "audiences": [
                    "api://AzureADTokenExchange"
                ]
            }
        }
    ]
}
TEMPLATE

  deployment_mode = "Incremental"
}
