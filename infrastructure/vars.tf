variable "azure_resource_group_name" {}
variable "azure_location" {}
variable "unique_project_name" {
  default     = "keda"
  description = "Value to make unique every resource name generated"
}