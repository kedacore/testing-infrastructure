provider "azurerm" {
  features {}
  skip_provider_registration = true
}

locals {
  storage_name = "${var.unique_project_name}e2estorage"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

resource "azurerm_storage_account" "storage" {
  name                     = local.storage_name
  resource_group_name      = data.azurerm_resource_group.rg.name
  location                 = data.azurerm_resource_group.rg.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  tags                     = var.tags
}

resource "azurerm_role_assignment" "blob_roles" {
  count                = length(var.storage_admin_identities)
  scope                = azurerm_storage_account.storage.id
  role_definition_name = "Storage Blob Data Owner"
  principal_id         = var.storage_admin_identities[count.index].principal_id
}

resource "azurerm_role_assignment" "queue_roles" {
  count                = length(var.storage_admin_identities)
  scope                = azurerm_storage_account.storage.id
  role_definition_name = "Storage Queue Data Contributor"
  principal_id         = var.storage_admin_identities[count.index].principal_id
}