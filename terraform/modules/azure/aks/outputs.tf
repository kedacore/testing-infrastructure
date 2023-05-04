output "oidc_issuer_url" {
  value = azurerm_kubernetes_cluster.aks.oidc_issuer_url
}

output "cluster_full_name" {
  value = local.cluster_full_name
}