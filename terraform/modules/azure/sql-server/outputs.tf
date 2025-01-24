output "sql_server_fqdn" {
  value = azurerm_mssql_server.server.fully_qualified_domain_name
}

output "sql_database_name" {
  value = var.sql_database_name
}

output "admin_username" {
  value = random_string.admin_username.result
}

output "admin_password" {
  value = random_password.admin_password.result
}

