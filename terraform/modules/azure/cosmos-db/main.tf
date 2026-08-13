provider "azurerm" {
  features {}
}

locals {
  cosmos_name = "${var.unique_project_name}-cosmos-db"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

resource "azurerm_cosmosdb_account" "db" {
  name                = local.cosmos_name
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name
  offer_type          = "Standard"
  kind                = "GlobalDocumentDB"

  capabilities {
    name = "EnableServerless"
  }

  consistency_policy {
    consistency_level = "Session"
  }

  geo_location {
    location          = var.location
    failover_priority = 0
  }

  tags = var.tags
}

resource "azurerm_cosmosdb_sql_role_assignment" "data_contributor" {
  count               = length(var.cosmos_admin_identities)
  resource_group_name = data.azurerm_resource_group.rg.name
  account_name        = azurerm_cosmosdb_account.db.name
  principal_id        = var.cosmos_admin_identities[count.index].principal_id
  role_definition_id  = "${azurerm_cosmosdb_account.db.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  scope               = azurerm_cosmosdb_account.db.id
}