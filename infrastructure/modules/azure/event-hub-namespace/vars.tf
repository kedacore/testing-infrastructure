variable "resource_group_name" {
  type        = string
  description = "Resource group name where event hub will be placed"
}

variable "location" {
  type        = string
  description = "Location where event hub will be placed"
}

variable "event_hub_name" {
  type        = string
  description = "Event Hub name"
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