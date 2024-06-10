locals {
  tags = {
    Project     = "KEDA"
    Environment = "e2e"
  }

  pr_cluster_name   = "cluster-pr"
  main_cluster_name = "cluster-nightly"
}

// ====== GRAFANA CLOUD =======

module "grafana_cloud" {
  source = "./modules/grafana/iam"
  slug   = var.grafana_slug
}

// ====== GCP ======

module "gcp_apis" {
  source = "./modules/gcp/apis"
  apis_to_enable = [
    "appengine.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudtasks.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "serviceusage.googleapis.com",
    "sts.googleapis.com",
    "secretmanager.googleapis.com",
  ]
}

module "gcp_iam" {
  source = "./modules/gcp/iam"
  identity_providers = [
    {
      provider_name   = module.azure_aks_pr.cluster_full_name
      oidc_issuer_url = module.azure_aks_pr.oidc_issuer_url
    },
    {
      provider_name   = module.azure_aks_nightly.cluster_full_name
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
      role_name       = "${module.azure_aks_pr.cluster_full_name}-role"
      oidc_issuer_url = module.azure_aks_pr.oidc_issuer_url
    },
    {
      role_name       = "${module.azure_aks_nightly.cluster_full_name}-role"
      oidc_issuer_url = module.azure_aks_nightly.oidc_issuer_url
    },
  ]
}

// ====== AZURE ======

data "azurerm_client_config" "current" {}

module "azuread_applications" {
  source              = "./modules/azure/managed_identities"
  resource_group_name = var.azure_resource_group_name
  unique_project_name = var.unique_project_name
}

module "azure_aks_pr" {
  source              = "./modules/azure/aks"
  resource_group_name = var.azure_resource_group_name
  kubernetes_version  = "1.29"
  cluster_name        = local.pr_cluster_name
  unique_project_name = var.unique_project_name

  azure_monitor_workspace_id   = module.azure_monitor_stack.azure_monitor_workspace_id
  azure_monitor_workspace_name = module.azure_monitor_stack.azure_monitor_workspace_name

  default_node_pool_count         = 1
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
  kubernetes_version  = "1.29"
  cluster_name        = local.main_cluster_name
  unique_project_name = var.unique_project_name

  azure_monitor_workspace_id   = module.azure_monitor_stack.azure_monitor_workspace_id
  azure_monitor_workspace_name = module.azure_monitor_stack.azure_monitor_workspace_name

  default_node_pool_count         = 1
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
  unique_project_name = var.unique_project_name
  service_bus_suffix  = "-alt"
  service_bus_admin_identities = [
    module.azuread_applications.identity_2
  ]
  tags = local.tags
}

module "azure_servicebus_namespace_event_grid" {
  source              = "./modules/azure/service-bus"
  resource_group_name = var.azure_resource_group_name
  unique_project_name = var.unique_project_name
  service_bus_suffix  = "-event-grid"
  service_bus_admin_identities = [
    module.azuread_applications.identity_1
  ]
  tags = local.tags
}

module "azurerm_eventgrid_topic" {
  source               = "./modules/azure/event-grid"
  resource_group_name  = var.azure_resource_group_name
  unique_project_name  = var.unique_project_name
  service_bus_topic_id = module.azure_servicebus_namespace_event_grid.event_grid_receive_topic_id

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

module "azure_rabbitmq_app_registration" {
  source              = "./modules/azure/app-registration"
  unique_project_name = var.unique_project_name
  application_purpose = "rabbitmq-oauth"
  # list of roles to create in application - see https://www.rabbitmq.com/oauth2.html#scope-and-tags
  app_roles = {
    management    = "rabbitmq.tag:management"
    administrator = "rabbitmq.tag:administrator"
    read_all      = "rabbitmq.read:*/*/*"
    write_all     = "rabbitmq.write:*/*/*"
    configure_all = "rabbitmq.configure:*/*/*"
  }

  access_identities = [
    module.azuread_applications.identity_1,
    module.azuread_applications.identity_2
  ]
}

module "azurerm_postgres_flexible_server" {
  source              = "./modules/azure/postgres-flex-server"
  resource_group_name = var.azure_resource_group_name
  unique_project_name = var.unique_project_name

  postgres_runtime_version = "14"
  postgres_sku_name        = "B_Standard_B1ms"
  postgres_storage_mb      = 32768

  postgres_database_name = "test_db"

  user_managed_identity_pg_ad_admin = module.azuread_applications.identity_1
  application_tenant_id             = data.azurerm_client_config.current.tenant_id

  tags = local.tags
}

// ====== GITHUB SECRETS ======

module "github_secrets" {
  source     = "./modules/github/secrets"
  repository = var.repository
  secrets = [
    {
      name  = "TF_AZURE_EVENTHUB_NAMESPACE"
      value = module.azure_event_hub_namespace.namespace_name
    },
    # Remove TF_AZURE_EVENTHBUS_MANAGEMENT_CONNECTION_STRING after 
    # https://github.com/kedacore/keda/pull/5471 is merged
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
    {
      name  = "TF_AZURE_MANAGED_PROMETHEUS_QUERY_ENDPOINT"
      value = module.azure_monitor_stack.azure_monitor_prometheus_query_endpoint
    },
    {
      name  = "TF_AZURE_LOG_ANALYTICS_WORKSPACE_ID"
      value = module.azure_monitor_stack.log_analytics_workspace_id
    },
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
      name  = "TF_AZURE_IDENTITY_1_NAME"
      value = module.azuread_applications.identity_1.name
    },
    {
      name  = "TF_AZURE_IDENTITY_2_APP_ID"
      value = module.azuread_applications.identity_2.client_id
    },
    {
      name  = "TF_AZURE_POSTGRES_FQDN"
      value = module.azurerm_postgres_flexible_server.postgres_flex_server_fqdn
    },
    {
      name  = "TF_AZURE_POSTGRES_ADMIN_USERNAME"
      value = module.azurerm_postgres_flexible_server.admin_username
    },
    {
      name  = "TF_AZURE_POSTGRES_ADMIN_PASSWORD"
      value = module.azurerm_postgres_flexible_server.admin_password
    },
    {
      name  = "TF_AZURE_POSTGRES_DB_NAME"
      value = module.azurerm_postgres_flexible_server.postgres_database_name
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
      // TO REMOVE AFTER MERGING https://github.com/kedacore/keda/pull/5061
      name  = "TF_AWS_ACCOUNT_ID"
      value = data.aws_caller_identity.current.account_id
    },
    {
      name  = "TF_AWS_KEDA_ROLE"
      value = module.aws_iam.keda_role_arn
    },
    {
      name  = "TF_AWS_WORKLOAD1_ROLE"
      value = module.aws_iam.workload1_role_arn
    },
    {
      name  = "TF_AWS_WORKLOAD2_ROLE"
      value = module.aws_iam.workload2_role_arn
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
    {
      name  = "TF_AZURE_RABBIT_API_APPLICATION_ID"
      value = module.azure_rabbitmq_app_registration.application_id
    },
    {
      name  = "TF_AZURE_SERVICE_BUS_EVENTGRID_CONNECTION_STRING"
      value = module.azure_servicebus_namespace_event_grid.connection_string
    },
    {
      name  = "TF_AZURE_EVENT_GRID_TOPIC_ENDPOINT"
      value = module.azurerm_eventgrid_topic.endpoint
    },
    {
      name  = "TF_AZURE_EVENT_GRID_TOPIC_KEY"
      value = module.azurerm_eventgrid_topic.key
    },
    {
      name  = "TF_AZURE_SB_EVENT_GRID_RECEIVE_TOPIC"
      value = module.azure_servicebus_namespace_event_grid.event_grid_receive_topic
    },
  ]
}

module "github__performance_secrets" {
  source     = "./modules/github/secrets"
  repository = var.performance_repository
  secrets = [
    {
      name  = "TF_GRAFANA_PROMETHEUS_URL"
      value = module.grafana_cloud.prometheus_url
    },
    {
      name  = "TF_GRAFANA_PROMETHEUS_USER"
      value = module.grafana_cloud.prometheus_user
    },
    {
      name  = "TF_GRAFANA_PROMETHEUS_PASSWORD"
      value = module.grafana_cloud.prometheus_password
    },
  ]
}
