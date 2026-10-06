provider "azurerm" {
  features {}
}

locals {
  redis_cache_name = "${var.unique_project_name}-managed-redis${var.redis_suffix}"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

resource "azurerm_managed_redis" "this" {
  name                = local.redis_cache_name
  resource_group_name = data.azurerm_resource_group.rg.name
  sku_name            = "Balanced_B0"
  location            = var.location
  tags                = var.tags
}

resource "azurerm_managed_redis_access_policy_assignment" "identities" {
  count            = length(var.redis_data_contributor_identities)
  managed_redis_id = azurerm_managed_redis.this.id
  object_id        = var.redis_data_contributor_identities[count.index].principal_id
}


