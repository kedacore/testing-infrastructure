provider "azurerm" {
  features {}
}

locals {
  kusto_cluster_name  = "${var.unique_project_name}cluster"
  kusto_database_name = "${var.unique_project_name}-database"
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