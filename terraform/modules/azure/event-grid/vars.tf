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

variable "service_bus_topic_id" {
  type        = string
  description = "Service bus topic to subscription events"
}

variable "tags" {
  type        = map(any)
  description = "Tags to apply on every resource"
}

variable "event_grid_admin_identities" {
  type        = list(any)
  description = "Azure Service Bus Data Owner identities"
  default     = []
}