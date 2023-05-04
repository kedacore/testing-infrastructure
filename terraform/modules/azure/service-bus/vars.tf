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

variable "service_bus_admin_identities" {
  type        = list(any)
  description = "Azure Service Bus Data Owner identities"
  default     = []
}

variable "service_bus_suffix" {
  type        = string
  description = "Suffix to append at the end of the name"
  default     = ""
}
