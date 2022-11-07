variable "resource_group_name" {
  type        = string
  description = "Resource group name where event hub will be placed"
}

variable "keda_sp_name" {
  default     = "keda-infra"
  type        = string
  description = "Service principal name used to deploy e2e test infrastructure"
}