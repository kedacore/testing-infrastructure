variable "azure_resource_group_name" {
  type        = string
  description = "Resource group name where azure resources will be placed"
}

variable "azure_location" {
  type        = string
  description = "Location where azure resources will be placed"
}

variable "unique_project_name" {
  default     = "keda"
  type        = string
  description = "Value to make unique every resource name generated"
}

variable "repository" {
  default     = "kedacore/keda"
  type        = string
  description = "Repository where secrets will be created/updated"
}

variable "keda_sp_name" {
  default     = "keda-infra"
  type        = string
  description = "Service principal name used to deploy e2e test infrastructure"
}