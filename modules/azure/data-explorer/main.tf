provider "azurerm" {
  features {}
  skip_provider_registration = true
}

locals {
  kusto_cluster_name          = "${var.unique_project_name}cluster"
  kusto_database_name         = "${var.unique_project_name}-database"
  kusto_role_assignement_name = "${var.unique_project_name}-role-assignement"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

resource "azurerm_kusto_cluster" "cluster" {
  name                = local.kusto_cluster_name
  location            = data.azurerm_resource_group.rg.location
  resource_group_name = data.azurerm_resource_group.rg.name

  sku {
    name     = "Dev(No SLA)_Standard_E2a_v4"
    capacity = 1
  }
  tags = var.tags
}

resource "azurerm_kusto_database" "database" {
  name                = local.kusto_database_name
  resource_group_name = data.azurerm_resource_group.rg.name
  location            = data.azurerm_resource_group.rg.location
  cluster_name        = azurerm_kusto_cluster.cluster.name

  hot_cache_period   = "P1D"
  soft_delete_period = "P1D"
}

resource "azurerm_kusto_cluster_principal_assignment" "role" {
  count               = length(var.admin_principal_ids)
  name                = "${local.kusto_role_assignement_name}-${count.index}"
  resource_group_name = data.azurerm_resource_group.rg.name
  cluster_name        = azurerm_kusto_cluster.cluster.name

  tenant_id      = var.admin_tenant_id
  principal_id   = var.admin_principal_ids[count.index]
  principal_type = "App"
  role           = "AllDatabasesAdmin"
}