terraform {
  backend "azurerm" {
    key = "keda.tfstate"
  }

  required_version = ">= 1.10.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "=5.84.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "=3.1.0"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "=4.16.0"
    }
    google = {
      source  = "hashicorp/google"
      version = "=6.17.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "=3.6.3"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "=4.0.6"
    }
    kubectl = {
      source  = "alekc/kubectl"
      version = "2.1.3"
    }
    mssql = {
      source  = "betr-io/mssql"
      version = "0.3.1"
    }
    grafana = {
      source  = "grafana/grafana"
      version = "3.17.1"
    }
  }
}

provider "azurerm" {
  features {}
}