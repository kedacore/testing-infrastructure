variable "resource_group_name" {
  type        = string
  description = "Resource group name where event hub will be placed"
}

variable "unique_project_name" {
  type        = string
  description = "Value to make unique every resource name generated"
}

variable "event_hub_sku" {
  type        = string
  description = "Event Hub SKU"
}

variable "event_hub_capacity" {
  type        = number
  description = "Event Hub capacity"
}

variable "tags" {
  type        = map(any)
  description = "Tags to apply on every resource"
}