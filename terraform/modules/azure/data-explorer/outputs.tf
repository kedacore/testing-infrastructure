output "database" {
  value = azurerm_kusto_database.database.name
}

output "endpoint" {
  value = azurerm_kusto_cluster.cluster.uri
}