output "postgres_flex_server_fqdn" {
  value = azurerm_postgresql_flexible_server.postgres_flex_server.fqdn
}

output "postgres_database_name" {
  value = azurerm_postgresql_flexible_server_database.postgres_flex_server_db.name
}

output "admin_username" {
  value = random_string.admin_username.result
}

output "admin_password" {
  value = random_password.admin_password.result
}

