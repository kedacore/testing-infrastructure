provider "azurerm" {
  features {}
  skip_provider_registration = true
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
  sku_name                    = "standard"

  access_policy {
    tenant_id = var.tenant_id
    object_id = var.access_object_id

    secret_permissions = [
      "Get",
      "Set",
      "List",
      "Delete",
      "Recover"
    ]
  }

  tags = var.tags
}

resource "azurerm_key_vault_secret" "secrets" {
  count        = length(var.secrets)
  name         = var.secrets[count.index].name
  value        = var.secrets[count.index].value
  key_vault_id = azurerm_key_vault.vault.id
}