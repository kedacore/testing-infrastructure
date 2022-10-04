locals {
  event_hub_name = "${var.unique_project_name}-event-hub-namespace"

  tags = {
    Project     = "KEDA"
    Environment = "e2e"
  }
}

module "azure_event_hub_namespace" {
  source              = "./modules/azure/event-hub-namespace"
  resource_group_name = var.azure_resource_group_name
  location            = var.azure_location

  event_hub_capacity  = 1
  event_hub_sku       = "Standard"
  unique_project_name = var.unique_project_name

  tags = local.tags
}

module "azure_storage_account" {
  source              = "./modules/azure/storage-account"
  resource_group_name = var.azure_resource_group_name
  location            = var.azure_location
  unique_project_name = var.unique_project_name

  tags = local.tags
}

module "github_secrets" {
  source     = "./modules/github/secrets"
  repository = var.repository
  secrets = [
    {
      name  = "TF_AZURE_EVENTHBUS_MANAGEMENT_CONNECTION_STRING"
      value = module.azure_event_hub_namespace.event_hub_namespace_manage_connectionstring
    },
    {
      name  = "TF_AZURE_STORAGE_CONNECTION_STRING"
      value = module.azure_storage_account.storage_account_connectionstring
    },
  ]
}