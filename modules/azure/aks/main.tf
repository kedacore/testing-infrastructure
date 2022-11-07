terraform {
  required_providers {
    azapi = {
      source  = "azure/azapi"
      version = "1.0.0"
    }
  }
}

provider "azurerm" {
  features {}
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
resource "azapi_resource" "federated_identity_credential" {
  count                     = length(var.workload_identity_applications)
  schema_validation_enabled = false
  name                      = "${var.cluster_name}-federation"
  parent_id                 = var.workload_identity_applications[count.index].id
  type                      = "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2022-01-31-preview"

  location = data.azurerm_resource_group.rg.location
  body = jsonencode({
    properties = {
      issuer    = azurerm_kubernetes_cluster.aks.oidc_issuer_url
      subject   = "system:serviceaccount:keda:keda-operator"
      audiences = ["api://AzureADTokenExchange"]
    }
  })
  lifecycle {
    ignore_changes = [location]
  }
}
