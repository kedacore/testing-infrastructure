locals {
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

module "azure_monitor_stack" {
  source              = "./modules/azure/monitor-stack"
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
    {
      name  = "TF_AZURE_APP_INSIGHTS_APP_ID"
      value = module.azure_monitor_stack.app_id
    },
    {
      name  = "TF_AZURE_APP_INSIGHTS_INSTRUMENTATION_KEY"
      value = module.azure_monitor_stack.instrumentation_key
    },
    {
      name  = "TF_AZURE_APP_INSIGHTS_CONNECTION_STRING"
      value = module.azure_monitor_stack.connections_string
    },
    {
      name  = "TF_AZURE_LOG_ANALYTICS_WORKSPACE_ID"
      value = module.azure_monitor_stack.workspace_id
    },
  ]
}