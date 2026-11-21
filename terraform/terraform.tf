terraform {
  backend "azurerm" {
    key = "c211-i121507-cicd.tfstate"
  }

  required_version = ">= 1.10.5"

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }
}
