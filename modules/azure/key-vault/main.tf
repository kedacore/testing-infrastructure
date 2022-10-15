provider "azurerm" {
  features {}
}

locals {
  key_vault_name = "${var.unique_project_name}-key-vault"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

resource "azurerm_key_vault" "vault" {
  name                        = local.key_vault_name
  location                    = data.azurerm_resource_group.rg.location
  resource_group_name         = data.azurerm_resource_group.rg.name
  enabled_for_disk_encryption = false
  tenant_id                   = var.tenant_id
  soft_delete_retention_days  = 7
  purge_protection_enabled    = false
  enable_rbac_authorization   = true
  sku_name                    = "standard"

  tags = var.tags
}

resource "azurerm_key_vault_secret" "secrets" {
  count        = length(var.secrets)
  name         = var.secrets[count.index].name
  value        = var.secrets[count.index].value
  key_vault_id = azurerm_key_vault.vault.id
}
