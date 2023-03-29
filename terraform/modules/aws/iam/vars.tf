variable "identity_providers" {
  type        = list(any)
  description = "Collection of OIDCs to create assignements"
}

variable "tags" {
  type        = map(any)
  description = "Tags to apply on every resource"
}