provider "azurerm" {
  features {}
  skip_provider_registration = true
}

locals {
  postgres_server_name = "${var.unique_project_name}-e2e-postgres"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

resource "random_password" "admin_password" {
  length      = 32
  special     = false
  min_lower   = 1
  min_numeric = 1
  min_upper   = 1
}

resource "random_string" "admin_username" {
  length    = 8
  special   = false
  numeric   = false
  min_lower = 1
  min_upper = 1
}

resource "azurerm_postgresql_flexible_server" "postgres_flex_server" {
  name                   = local.postgres_server_name
  resource_group_name    = data.azurerm_resource_group.rg.name
  location               = "northeurope" # We cannot provision postgres on WestEurope
  administrator_login    = random_string.admin_username.result
  administrator_password = random_password.admin_password.result
  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = true
    tenant_id                     = var.application_tenant_id
  }
  version    = "14"
  sku_name   = var.postgres_sku_name
  storage_mb = var.postgres_storage_mb
  zone       = "1"

  tags = var.tags
}

resource "azurerm_postgresql_flexible_server_active_directory_administrator" "postgres_flex_server_ad_admin_uami" {
  server_name         = azurerm_postgresql_flexible_server.postgres_flex_server.name
  resource_group_name = data.azurerm_resource_group.rg.name
  object_id           = var.user_managed_identity_pg_ad_admin.principal_id
  principal_name      = var.user_managed_identity_pg_ad_admin.name
  tenant_id           = var.application_tenant_id
  principal_type      = "ServicePrincipal"
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "postgres_flex_server_fwr_allow_azure" {
  name             = "AllowAllAzure"
  server_id        = azurerm_postgresql_flexible_server.postgres_flex_server.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

resource "azurerm_postgresql_flexible_server_database" "postgres_flex_server_db" {
  name      = var.postgres_database_name
  server_id = azurerm_postgresql_flexible_server.postgres_flex_server.id
}
