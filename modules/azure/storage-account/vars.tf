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
  description = "Tags to apply on every resource"
}

variable "storage_admin_identities" {
  type        = list(any)
  description = "Storage Account Contributor identities"
  default     = []
}