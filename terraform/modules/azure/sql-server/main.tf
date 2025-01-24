provider "azurerm" {
  features {}
  skip_provider_registration = true
}

locals {
  sql_server_name         = "${var.unique_project_name}-e2e-sql-server"
  sql_server_network_name = "${var.unique_project_name}-e2e-sql-server-net"
  sql_server_subnet_name  = "${var.unique_project_name}-e2e-sql-server-subnet"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

resource "azurerm_virtual_network" "network" {
  name                = local.sql_server_network_name
  resource_group_name = data.azurerm_resource_group.rg.name
  location            = var.location
  address_space       = ["10.0.0.0/16"]
}

resource "azurerm_subnet" "subnet" {
  name                 = local.sql_server_subnet_name
  resource_group_name  = data.azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.network.name
  address_prefixes     = ["10.0.2.0/24"]
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

resource "azurerm_mssql_managed_instance" "instance" {
  name                = local.sql_server_name
  resource_group_name = data.azurerm_resource_group.rg.name
  location            = var.location

  license_type       = "BasePrice"
  sku_name           = var.sql_sku_name
  storage_size_in_gb = var.sql_storage_gb
  subnet_id          = azurerm_subnet.subnet.id
  vcores             = var.sql_vcores

  administrator_login          = random_string.admin_username.result
  administrator_login_password = random_password.admin_password.result

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_mssql_managed_instance_active_directory_administrator" "admin" {
  managed_instance_id = azurerm_mssql_managed_instance.instance.id
  login_username      = "AzureAD Admin"
  object_id           = var.user_managed_identity_sql_ad_admin
  tenant_id           = var.application_tenant_id
}


resource "azurerm_mssql_managed_database" "database" {
  name                = var.sql_database_name
  managed_instance_id = azurerm_mssql_managed_instance.instance.id
}
