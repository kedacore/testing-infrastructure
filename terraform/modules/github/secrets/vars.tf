variable "secrets" {
  type        = list(any)
  description = "Collection of secrets (key-value) to be created in GH"
}

variable "repository" {
  type        = string
  description = "Repository where secrets will be created/updated"
}