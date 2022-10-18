variable "resource_group_name" {
  type        = string
  description = "Resource group name where event hub will be placed"
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
  description = "Service Principals to federate with the AKS oidc"
}

