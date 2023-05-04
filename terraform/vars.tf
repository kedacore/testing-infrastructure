variable "azure_resource_group_name" {
  default     = "keda-e2e-infrastructure"
  type        = string
  description = "Resource group name where azure resources will be placed"
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