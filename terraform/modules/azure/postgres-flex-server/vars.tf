variable "resource_group_name" {
  type        = string
  description = "Resource group name where event hub will be placed"
}

variable "unique_project_name" {
  type        = string
  description = "Value to make unique every resource name generated"
}

variable "tags" {
  type        = map(any)
  description = "Tags to apply on resources accepting it"
}

variable "postgres_runtime_version" {
  type        = string
  description = "Postgres version to use"
  default     = "14"
}

variable "postgres_sku_name" {
  type        = string
  description = "The SKU Name for the PostgreSQL Flexible Server"
  default     = "B_Standard_B1ms"
}

variable "postgres_storage_mb" {
  type        = number
  description = "The max storage allowed for the PostgreSQL Flexible Server"
  default     = 32768
}

variable "postgres_database_name" {
  type        = string
  description = "Database name to create inside the server"
  default     = "test_db"
}

variable "user_managed_identity_pg_ad_admin" {
  type        = any
  description = "User managed identitiy that will be granted admin access on the PostgreSQL Flexible Server"
}

variable "application_tenant_id" {
  type        = string
  description = "TenantId of the application"
}