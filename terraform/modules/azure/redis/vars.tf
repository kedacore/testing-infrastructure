variable "resource_group_name" {
  type        = string
  description = "Resource group name where the Redis instance will be placed"
}

variable "location" {
  type        = string
  description = "Location to place the resource"
  default     = "northeurope"
}

variable "unique_project_name" {
  type        = string
  description = "Value to make unique every resource name generated"
}

variable "redis_suffix" {
  type        = string
  description = "Optional suffix to disambiguate multiple instances"
  default     = ""
}

variable "redis_data_contributor_identities" {
  type        = list(any)
  description = "Identities that will get full data-plane access via Entra ID"
  default     = []
}

variable "tags" {
  type        = map(any)
  description = "Tags to apply on every resource"
}
