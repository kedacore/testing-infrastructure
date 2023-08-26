terraform {
  backend "azurerm" {
    key = "keda.tfstate"
  }

  required_version = ">= 1.3.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "=5.14.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "=2.41.0"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "=3.71.0"
    }
    google = {
      source  = "hashicorp/google"
      version = "=4.79.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "=3.5.1"
    } 
    tls = {
      source  = "hashicorp/tls"
      version = "=4.0.4"
    }    
  }
}

provider "azurerm" {
  features {}
  skip_provider_registration = true
}