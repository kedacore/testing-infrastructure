variable "unique_project_name" {
  type        = string
  description = "Value to make unique every resource name generated"
}

variable "application_purpose" {
  type        = string
  description = "Value to create app name / app identifier from"
}

variable "app_roles" {
  type        = map(string)
  description = "Role names of application"
}

variable "access_identities" {
  type        = list(any)
  description = "Identities with access to this application (all roles)"
}
