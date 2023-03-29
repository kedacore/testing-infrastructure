terraform {
  required_providers {
    github = {
      source  = "integrations/github"
      version = "=5.3.0"
    }
  }
}

data "github_repository" "repo" {
  full_name = var.repository
}

resource "github_actions_secret" "secrets" {
  repository      = data.github_repository.repo.name
  secret_name     = var.secrets[count.index].name
  plaintext_value = var.secrets[count.index].value
  count           = length(var.secrets)
}
