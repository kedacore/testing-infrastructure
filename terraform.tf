terraform {
  backend "azurerm" {
  }

  required_version = ">= 1.3.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "=3.7.0"
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
  }
}

provider "azurerm" {
  features {}
}