variable "resource_group_name" {
  type        = string
  description = "Resource group name where Cosmos DB will be placed"
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
  type        = map(string)
  description = "Tags to apply on every resource"
}

variable "cosmos_admin_identities" {
  type        = list(any)
  description = "Identities allowed to read and write Cosmos DB data"
  default     = []
}
