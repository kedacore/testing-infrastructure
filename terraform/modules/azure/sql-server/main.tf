terraform {
  required_providers {
    mssql = {
      source = "betr-io/mssql"
    }
  }
}

provider "azurerm" {
  features {}
  skip_provider_registration = true
}

locals {
  sql_server_name         = "${var.unique_project_name}-e2e-sql-server"
  sql_server_network_name = "${var.unique_project_name}-e2e-sql-server-net"
  sql_server_subnet_name  = "${var.unique_project_name}-e2e-sql-server-subnet"
}

data "azurerm_client_config" "current" {}

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

resource "azurerm_mssql_server" "server" {
  name                = local.sql_server_name
  resource_group_name = data.azurerm_resource_group.rg.name
  location            = var.location
  version             = var.sql_version
  minimum_tls_version = "1.2"

  administrator_login          = random_string.admin_username
  administrator_login_password = random_string.admin_password

  azuread_administrator {
    login_username = "AzureAD Admin"
    object_id      = data.azurerm_client_config.current.object_id
  }

  tags = var.naming.tags

}

resource "azurerm_mssql_database" "database" {
  name        = var.sql_database_name
  server_id   = azurerm_mssql_server.server.id
  max_size_gb = var.sql_storage_gb
  tags        = var.tags
  sku_name    = var.sql_sku_name
}

provider "mssql" {
  debug = "true"
}

resource "mssql_user" "external_users" {
  server {
    host = azurerm_mssql_managed_instance.instance.fqdn
    login {
      username = random_string.admin_username
      password = random_string.admin_password
    }
  }

  database  = azurerm_mssql_database.database.name
  username  = "msi-admin-${azurerm_mssql_database.database.name}"
  object_id = var.user_managed_identity_sql_ad_admin.client_id

  roles = ["db_owner"]
}