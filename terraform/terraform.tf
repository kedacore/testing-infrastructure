terraform {
  backend "azurerm" {
    key = "keda.tfstate"
  }

  required_version = ">= 1.5.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "=5.60.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "=2.53.1"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "=3.113.0"
    }
    google = {
      source  = "hashicorp/google"
      version = "=5.39.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "=3.6.2"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "=4.0.5"
    }
  }
}

provider "azurerm" {
  features {}
  skip_provider_registration = true
}