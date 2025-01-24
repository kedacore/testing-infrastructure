variable "resource_group_name" {
  type        = string
  description = "Resource group name where event hub will be placed"
}

variable "location" {
  type        = string
  description = "Location to place the resource"
  default     = "westeurope"
}

variable "unique_project_name" {
  type        = string
  description = "Value to make unique every resource name generated"
}

variable "tags" {
  type        = map(any)
  description = "Tags to apply on resources accepting it"
}

variable "sql_version" {
  type        = string
  description = "Sql version to use"
  default     = "12"
}

variable "sql_sku_name" {
  type        = string
  description = "The SKU Name"
  default     = "BC_Gen4"
}

variable "sql_storage_gb" {
  type        = number
  description = "The max storage allowed"
  default     = 5
}

variable "sql_vcores" {
  type        = number
  description = "The vcores allowed"
  default     = 1
}
variable "sql_database_name" {
  type        = string
  description = "Database name to create inside the server"
  default     = "test_db"
}

variable "user_managed_identity_sql_ad_admin" {
  type        = any
  description = "User managed identitiy that will be granted admin access on the SQL server"
}