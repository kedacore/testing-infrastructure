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

resource "azuread_application_federated_identity_credential" "identity_federation" {
  count                 = length(var.workload_identity_applications)
  application_object_id = var.workload_identity_applications[count.index].object_id
  display_name          = "${var.cluster_name}-federation"
  description           = "${var.cluster_name} federation"
  audiences             = ["api://AzureADTokenExchange"]
  issuer                = azurerm_kubernetes_cluster.aks.oidc_issuer_url
  subject               = "system:serviceaccount:keda:keda-operator"
}
