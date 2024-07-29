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
  description = "Tags to apply on every resource"
}

variable "secrets" {
  type        = list(any)
  description = "Collection of secrets (key-value) to be created in Key Vault"
}

variable "access_object_id" {
  type        = string
  description = "ObjectId with access to this key vault"
}

variable "tenant_id" {
  type        = string
  description = "TenantId for this key vault"
}

variable "key_vault_applications" {
  type        = list(any)
  description = "Managed identities to grant access in key vault"
}