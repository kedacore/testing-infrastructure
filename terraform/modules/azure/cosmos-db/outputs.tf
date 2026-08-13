output "connection_string" {
  value     = azurerm_cosmosdb_account.db.primary_sql_connection_string
  sensitive = true
}