terraform {
  backend "azurerm" {
  }

  required_version = ">= 1.3.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "=3.31.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "=2.29.0"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "=4.35"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "=4.0.3"
    }
    google = {
      source  = "hashicorp/google"
      version = "=4.44.0"
    }
  }
}

provider "azurerm" {
  features {}
  skip_provider_registration = true
}