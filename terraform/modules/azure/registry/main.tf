provider "azurerm" {
  features {}
  skip_provider_registration = true
}

locals {
  registry_name = "${var.unique_project_name}proxy"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

resource "azurerm_container_registry" "acr" {
  name                = local.registry_name
  resource_group_name = data.azurerm_resource_group.rg.name
  location            = var.location
  sku                 = "Basic"
  tags                = var.tags
}

resource "azurerm_container_registry_cache_rule" "docker-io" {
  name                  = "docker-io"
  container_registry_id = azurerm_container_registry.acr.id
  target_repo           = "*"
  source_repo           = "docker.io/*"
}