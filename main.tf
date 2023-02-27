locals {
  tags = {
    Project     = "KEDA"
    Environment = "e2e"
  }

  pr_cluster_name   = "keda-pr-run"
  main_cluster_name = "keda-nightly-run-3"
}

// ====== GCP ======

module "gcp_iam" {
  source = "./modules/gcp/iam"
  identity_providers = [
    {
      provider_name   = local.pr_cluster_name
      oidc_issuer_url = module.azure_aks_pr.oidc_issuer_url
    },
    {
      provider_name   = local.main_cluster_name
      oidc_issuer_url = module.azure_aks_nightly.oidc_issuer_url
    },
  ]
}

// ====== AWS ======

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

module "aws_iam" {
  source = "./modules/aws/iam"
  tags   = local.tags
  identity_providers = [
    {
      role_name       = "${local.pr_cluster_name}-role"
      oidc_issuer_url = module.azure_aks_pr.oidc_issuer_url
    },
    {
      role_name       = "${local.main_cluster_name}-role"
      oidc_issuer_url = module.azure_aks_nightly.oidc_issuer_url
    },
  ]
}

// ====== AZURE ======

data "azurerm_client_config" "current" {}

module "azuread_applications" {
  source              = "./modules/azure/managed_identities"
  resource_group_name = var.azure_resource_group_name
}

module "azure_aks_pr" {
  source              = "./modules/azure/aks"
  resource_group_name = var.azure_resource_group_name
  kubernetes_version  = "1.25"
  cluster_name        = "keda-pr-run"

  azure_monitor_workspace_id   = module.azure_monitor_stack.azure_monitor_workspace_id
  azure_monitor_workspace_name = module.azure_monitor_stack.azure_monitor_workspace_name

  default_node_pool_count         = 4
  default_node_pool_instance_type = "Standard_B4ms"
  node_resource_group_name        = null

  workload_identity_applications = [
    module.azuread_applications.identity_1,
    module.azuread_applications.identity_2
  ]

  tags = local.tags
}

module "azure_aks_nightly" {
  source              = "./modules/azure/aks"
  resource_group_name = var.azure_resource_group_name
  kubernetes_version  = "1.25"
  cluster_name        = "keda-nightly-run-3"

  azure_monitor_workspace_id   = module.azure_monitor_stack.azure_monitor_workspace_id
  azure_monitor_workspace_name = module.azure_monitor_stack.azure_monitor_workspace_name

  default_node_pool_count         = 4
  default_node_pool_instance_type = "Standard_B4ms"
  node_resource_group_name        = null

  workload_identity_applications = [
    module.azuread_applications.identity_1,
    module.azuread_applications.identity_2
  ]

  tags = local.tags
}

module "azure_key_vault" {
  source              = "./modules/azure/key-vault"
  resource_group_name = var.azure_resource_group_name

  unique_project_name = var.unique_project_name

  access_object_id = data.azurerm_client_config.current.object_id
  tenant_id        = data.azurerm_client_config.current.tenant_id

  key_vault_applications = [
    module.azuread_applications.identity_1,
    module.azuread_applications.identity_2
  ]

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

  unique_project_name = var.unique_project_name

  admin_principal_ids = [
    data.azurerm_client_config.current.client_id,
    module.azuread_applications.identity_1.principal_id,
    module.azuread_applications.identity_2.principal_id
  ]
  admin_tenant_id = data.azurerm_client_config.current.tenant_id

  tags = local.tags
}

module "azure_event_hub_namespace" {
  source              = "./modules/azure/event-hub-namespace"
  resource_group_name = var.azure_resource_group_name

  event_hub_capacity  = 1
  event_hub_sku       = "Standard"
  unique_project_name = var.unique_project_name

  event_hub_admin_identities = [
    module.azuread_applications.identity_1,
    module.azuread_applications.identity_2
  ]

  tags = local.tags
}

module "azure_monitor_stack" {
  source              = "./modules/azure/monitor-stack"
  resource_group_name = var.azure_resource_group_name
  unique_project_name = var.unique_project_name

  monitor_admin_identities = [
    module.azuread_applications.identity_1,
    module.azuread_applications.identity_2
  ]

  tags = local.tags
}

module "azure_servicebus_namespace" {
  source              = "./modules/azure/service-bus"
  resource_group_name = var.azure_resource_group_name
  unique_project_name = var.unique_project_name
  service_bus_admin_identities = [
    module.azuread_applications.identity_1
  ]

  tags = local.tags
}

module "azure_servicebus_namespace_alternative" {
  source              = "./modules/azure/service-bus"
  resource_group_name = var.azure_resource_group_name
  unique_project_name = "${var.unique_project_name}-alt"
  service_bus_admin_identities = [
    module.azuread_applications.identity_2
  ]
  tags = local.tags
}

module "azure_storage_account" {
  source              = "./modules/azure/storage-account"
  resource_group_name = var.azure_resource_group_name
  unique_project_name = var.unique_project_name

  storage_admin_identities = [
    module.azuread_applications.identity_1,
    module.azuread_applications.identity_2
  ]

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
      name  = "TF_AZURE_APP_INSIGHTS_NAME"
      value = module.azure_monitor_stack.insights_name
    },
    // TO BE DELETED ONCE https://github.com/kedacore/keda/pull/4200 is merged
    {
      name  = "TF_AZURE_APP_INSIGHTS_CONNECTION_STRING"
      value = module.azure_monitor_stack.connection_string
    },
    {
      name  = "TF_AZURE_LOG_ANALYTICS_WORKSPACE_ID"
      value = module.azure_monitor_stack.log_analytics_workspace_id
    },
    # {
    #   name  = "TF_AZURE_MANAGED_PROMETHEUS_QUERY_ENDPOINT"
    #   value = module.azure_monitor_stack.azure_monitor_prometheus_query_endpoint
    # },
    {
      name  = "TF_AZURE_SERVICE_BUS_CONNECTION_STRING"
      value = module.azure_servicebus_namespace.connection_string
    },
    {
      name  = "TF_AZURE_SERVICE_BUS_ALTERNATIVE_CONNECTION_STRING"
      value = module.azure_servicebus_namespace_alternative.connection_string
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
      value = data.azurerm_client_config.current.client_id
    },
    {
      name  = "TF_AZURE_SP_TENANT"
      value = data.azurerm_client_config.current.tenant_id
    },
    {
      name  = "TF_AZURE_SUBSCRIPTION"
      value = data.azurerm_client_config.current.subscription_id
    },
    {
      name  = "TF_AZURE_IDENTITY_1_APP_ID"
      value = module.azuread_applications.identity_1.client_id
    },
    {
      name  = "TF_AZURE_IDENTITY_1_APP_FULL_ID"
      value = module.azuread_applications.identity_1.id
    },
    {
      name  = "TF_AZURE_IDENTITY_2_APP_ID"
      value = module.azuread_applications.identity_2.client_id
    },
    {
      name  = "TF_AZURE_KEYVAULT_URI"
      value = module.azure_key_vault.vault_uri
    },
    {
      name  = "TF_AWS_ACCESS_KEY"
      value = module.aws_iam.e2e_user_access_key
    },
    {
      name  = "TF_AWS_SECRET_KEY"
      value = module.aws_iam.e2e_user_secret_key
    },
    {
      name  = "TF_AWS_REGION"
      value = data.aws_region.current.name
    },
    {
      name  = "TF_AWS_ACCOUNT_ID"
      value = data.aws_caller_identity.current.account_id
    },
    {
      name  = "TF_GCP_SA_CREDENTIALS"
      value = module.gcp_iam.e2e_user_credentials
    },
    {
      name  = "TF_GCP_SA_EMAIL"
      value = module.gcp_iam.e2e_user_email
    },
    {
      name  = "TF_GCP_PROJECT_NUMBER"
      value = module.gcp_iam.project_number
    },
  ]
}
