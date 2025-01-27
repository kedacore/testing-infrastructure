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
  default     = "keda"
  type        = string
  description = "Value to make unique every resource name generated"
}

variable "tags" {
  type        = map(any)
  description = "Tags to apply on every resource"
}

variable "cluster_name" {
  type        = string
  description = "AKS cluster name"
}

variable "kubernetes_version" {
  type        = string
  description = "Kubernetes version"
}

variable "node_resource_group_name" {
  type        = string
  default     = null
  description = "AKS nodes resource group name"
}

variable "azure_container_registry_enpoint" {
  type        = string
  description = "Azure Container Registry Endpoint"
}

variable "azure_container_registry_username" {
  type        = string
  description = "Azure Container Registry Username"
}

variable "azure_container_registry_password" {
  type        = string
  description = "Azure Container Registry Password"
}

variable "default_node_pool_count" {
  type        = number
  default     = 3
  description = "Default node pool instance count"
}

variable "default_node_pool_instance_type" {
  type        = string
  default     = "Standard_D2_v2"
  description = "Default node pool instance type"
}

variable "workload_identity_applications" {
  type        = list(any)
  description = "Managed identities to federate with the AKS oidc"
}

variable "azure_monitor_workspace_id" {
  type        = string
  description = "Azure Monitor Workspace ID"
}

variable "azure_monitor_workspace_name" {
  type        = string
  description = "Azure Monitor Workspace name"
}