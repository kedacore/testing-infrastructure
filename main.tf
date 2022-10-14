locals {
  tags = {
    Project     = "KEDA"
    Environment = "e2e"
  }
}

// ====== AZURE ======

module "azuread_applications" {
  source       = "./modules/azure/aad-applications"
  keda_sp_name = var.keda_sp_name
}

module "azure_current_client" {
  source = "./modules/azure/client-config"
}

module "azure_aks_pr" {
  source              = "./modules/azure/aks"
  resource_group_name = var.azure_resource_group_name
  location            = var.azure_location
  kubernetes_version  = "1.23"
  cluster_name        = "keda-pr-run"

  default_node_pool_count         = 1
  default_node_pool_instance_type = "Standard_B2s"
  node_resource_group_name        = null

  workload_identity_service_principals = [
    module.azuread_applications.identity_1,
    module.azuread_applications.identity_2
  ]

  tags = local.tags
}

module "azure_aks_nightly" {
  source              = "./modules/azure/aks"
  resource_group_name = var.azure_resource_group_name
  location            = var.azure_location
  kubernetes_version  = "1.23"
  cluster_name        = "keda-nightly-run-3"

  default_node_pool_count         = 1
  default_node_pool_instance_type = "Standard_B2s"
  node_resource_group_name        = null

  workload_identity_service_principals = [
    module.azuread_applications.identity_1,
    module.azuread_applications.identity_2
  ]

  tags = local.tags
}

module "azure_key_vault" {
  source              = "./modules/azure/key-vault"
  resource_group_name = var.azure_resource_group_name
  location            = var.azure_location

  unique_project_name = var.unique_project_name

  access_app_id    = module.azuread_applications.keda_sp_app_id
  access_object_id = module.azuread_applications.keda_sp_object_id
  tenant_id        = module.azure_current_client.tenant_id

  secrets = [
    {
      name  = "E2E-Storage-ConnectionString"
      value = module.azure_storage_account.connection_string
    },
  ]

  tags = local.tags
}

module "azure_data_explorer" {
  source              = "./modules/azure/data-explorer"
  resource_group_name = var.azure_resource_group_name
  location            = var.azure_location

  unique_project_name = var.unique_project_name

  tags = local.tags
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

module "azure_monitor_stack" {
  source              = "./modules/azure/monitor-stack"
  resource_group_name = var.azure_resource_group_name
  location            = var.azure_location
  unique_project_name = var.unique_project_name

  tags = local.tags
}

module "azure_servicebus_namespace" {
  source              = "./modules/azure/service-bus"
  resource_group_name = var.azure_resource_group_name
  location            = var.azure_location
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

// ====== GITHUB SECRETS ======

module "github_secrets" {
  source     = "./modules/github/secrets"
  repository = var.repository
  secrets = [
    {
      name  = "TF_AZURE_EVENTHBUS_MANAGEMENT_CONNECTION_STRING"
      value = module.azure_event_hub_namespace.manage_connection_string
    },
    {
      name  = "TF_AZURE_STORAGE_CONNECTION_STRING"
      value = module.azure_storage_account.connection_string
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
      value = module.azure_monitor_stack.connection_string
    },
    {
      name  = "TF_AZURE_LOG_ANALYTICS_WORKSPACE_ID"
      value = module.azure_monitor_stack.workspace_id
    },
    {
      name  = "TF_AZURE_SERVICE_BUS_CONNECTION_STRING"
      value = module.azure_servicebus_namespace.connection_string
    },
    {
      name  = "TF_AZURE_DATA_EXPLORER_DB"
      value = module.azure_data_explorer.database
    },
    {
      name  = "TF_AZURE_DATA_EXPLORER_ENDPOINT"
      value = module.azure_data_explorer.endpoint
    },
    {
      name  = "TF_AZURE_RESOURCE_GROUP"
      value = var.azure_resource_group_name
    },
    {
      name  = "TF_AZURE_SP_APP_ID"
      value = module.azuread_applications.keda_sp_app_id
    },
    {
      name  = "TF_AZURE_SP_KEY"
      value = module.azuread_applications.keda_sp_secret
    },
    {
      name  = "TF_AZURE_SP_TENANT"
      value = module.azure_current_client.tenant_id
    },
    {
      name  = "TF_AZURE_SUBSCRIPTION"
      value = module.azure_current_client.subscription_id
    },
    {
      name  = "TF_AZURE_IDENTITY_1_APP_ID"
      value = module.azure_current_client.tenant_id
    },
    {
      name  = "TF_AZURE_IDENTITY_2_APP_ID"
      value = module.azure_current_client.subscription_id
    },
    {
      name  = "TF_AZURE_KEYVAULT_URI"
      value = module.azure_key_vault.vault_uri
    },
  ]
}
