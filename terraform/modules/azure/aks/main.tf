terraform {
  required_providers {
    kubectl = {
      source = "alekc/kubectl"
    }
  }
}

provider "azurerm" {
  features {}
}

locals {
  cluster_full_name                       = "${var.unique_project_name}-e2e-${var.cluster_name}"
  azure_monitor_workspace_connection_name = "${local.cluster_full_name}-amw"
  dce_name                                = "${local.azure_monitor_workspace_connection_name}-dce"
  dcr_name                                = "${local.azure_monitor_workspace_connection_name}-dcr"
  dcra_name                               = "${local.azure_monitor_workspace_connection_name}-dcra"
  rule_group_name                         = "${local.azure_monitor_workspace_connection_name}-rules"
}

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

data "azurerm_kubernetes_service_versions" "current" {
  location        = var.location
  include_preview = true
  version_prefix  = var.kubernetes_version
}

resource "azurerm_kubernetes_cluster" "aks" {
  name                = local.cluster_full_name
  dns_prefix          = local.cluster_full_name
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name
  kubernetes_version  = data.azurerm_kubernetes_service_versions.current.latest_version
  oidc_issuer_enabled = true
  node_resource_group = var.node_resource_group_name

  monitor_metrics {}

  default_node_pool {
    name                 = "default"
    node_count           = var.default_node_pool_count
    vm_size              = var.default_node_pool_instance_type
    orchestrator_version = data.azurerm_kubernetes_service_versions.current.latest_version
    tags                 = var.tags

    upgrade_settings {
      drain_timeout_in_minutes      = 0
      max_surge                     = "10%"
      node_soak_duration_in_minutes = 0
    }
  }

  identity {
    type = "SystemAssigned"
  }

  tags = var.tags

  lifecycle {
    ignore_changes = [
      default_node_pool[0].node_count
    ]
  }
}

## Workload Identity Federation

resource "azurerm_federated_identity_credential" "msi_federation" {
  count               = length(var.workload_identity_applications)
  name                = "msi_federation-${local.cluster_full_name}-${var.workload_identity_applications[count.index].name}"
  resource_group_name = data.azurerm_resource_group.rg.name
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.aks.oidc_issuer_url
  parent_id           = var.workload_identity_applications[count.index].id
  subject             = "system:serviceaccount:keda:keda-operator"
}

## Azure Managed prometheus

resource "azurerm_resource_group_template_deployment" "dce" {
  name                = local.dce_name
  resource_group_name = data.azurerm_resource_group.rg.name
  deployment_mode     = "Incremental"

  parameters_content = jsonencode({
    "dce_name" = {
      value = local.dce_name
    }
    "location" = {
      value = var.location
    }
  })
  template_content = <<TEMPLATE
{
  "$schema": "http://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
      "dce_name": {
          "type": "String"
      },
      "location": {
          "type": "String"
      }
  },
  "resources": [
    {
      "type": "Microsoft.Insights/dataCollectionEndpoints",
      "apiVersion": "2021-09-01-preview",
      "name": "[parameters('dce_name')]",
      "location": "[parameters('location')]",
      "kind": "Linux",
      "properties": {}
    }
  ]
}
TEMPLATE

  // NOTE: whilst we show an inline template here, we recommend
  // sourcing this from a file for readability/editor support
}

resource "azurerm_resource_group_template_deployment" "dcr" {
  name                = local.dcr_name
  resource_group_name = data.azurerm_resource_group.rg.name
  deployment_mode     = "Incremental"

  depends_on = [
    azurerm_resource_group_template_deployment.dce
  ]

  parameters_content = jsonencode({
    "dce_name" = {
      value = local.dce_name
    }
    "dcr_name" = {
      value = local.dcr_name
    }
    "azure_monitor_workspace_id" = {
      value = var.azure_monitor_workspace_id
    }
    "location" = {
      value = var.location
    }
  })
  template_content = <<TEMPLATE
{
  "$schema": "http://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
      "dce_name": {
          "type": "String"
      },
      "dcr_name": {
          "type": "String"
      },
      "azure_monitor_workspace_id": {
          "type": "String"
      },
      "location": {
          "type": "String"
      }
  },
  "resources": [
    {
      "type": "Microsoft.Insights/dataCollectionRules",
      "apiVersion": "2021-09-01-preview",
      "name": "[parameters('dcr_name')]",
      "location": "[parameters('location')]",
      "kind": "Linux",
      "properties": {
        "dataCollectionEndpointId": "[resourceId('Microsoft.Insights/dataCollectionEndpoints/', parameters('dce_name'))]",
        "dataFlows": [
          {
            "destinations": ["MonitoringAccount1"],
            "streams": ["Microsoft-PrometheusMetrics"]
          }
        ],
        "dataSources": {
          "prometheusForwarder": [
            {
              "name": "PrometheusDataSource",
              "streams": ["Microsoft-PrometheusMetrics"],
              "labelIncludeFilter": {}
            }
          ]
        },
        "description": "DCR for Azure Monitor Metrics Profile (Managed Prometheus)",
        "destinations": {
          "monitoringAccounts": [
            {
              "accountResourceId": "[parameters('azure_monitor_workspace_id')]",
              "name": "MonitoringAccount1"
            }
          ]
        }
      }
    }
  ]
}
TEMPLATE

  // NOTE: whilst we show an inline template here, we recommend
  // sourcing this from a file for readability/editor support
}

resource "azurerm_resource_group_template_deployment" "dcra" {
  name                = local.dcra_name
  resource_group_name = data.azurerm_resource_group.rg.name
  deployment_mode     = "Incremental"

  depends_on = [
    azurerm_resource_group_template_deployment.dcr
  ]

  parameters_content = jsonencode({
    "dcr_name" = {
      value = local.dcr_name
    }
    "dcra_name" = {
      value = local.dcra_name
    }
    "cluster_name" = {
      value = azurerm_kubernetes_cluster.aks.name
    }
    "location" = {
      value = var.location
    }
  })
  template_content = <<TEMPLATE
{
  "$schema": "http://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "dcr_name": {
      "type": "String"
    },
    "dcra_name": {
      "type": "String"
    },
    "cluster_name": {
      "type": "String"
    },
    "location": {
      "type": "String"
    }
  },
  "resources": [
    {
      "type": "Microsoft.ContainerService/managedClusters/providers/dataCollectionRuleAssociations",
      "name": "[concat(parameters('cluster_name'),'/microsoft.insights/', parameters('dcra_name'))]",
      "apiVersion": "2021-09-01-preview",
      "location": "[parameters('location')]",
      "properties": {
        "description": "Association of data collection rule. Deleting this association will break the data collection for this AKS Cluster.",
        "dataCollectionRuleId": "[resourceId('Microsoft.Insights/dataCollectionRules', parameters('dcr_name'))]"
      }
    }
  ]
}
TEMPLATE

  // NOTE: whilst we show an inline template here, we recommend
  // sourcing this from a file for readability/editor support
}

resource "azurerm_resource_group_template_deployment" "rules" {
  name                = local.rule_group_name
  resource_group_name = data.azurerm_resource_group.rg.name
  deployment_mode     = "Incremental"

  parameters_content = jsonencode({
    "rule_group_name" = {
      value = local.rule_group_name
    }
    "workspace_name" = {
      value = var.azure_monitor_workspace_name
    }
    "cluster_name" = {
      value = azurerm_kubernetes_cluster.aks.name
    }
    "location" = {
      value = var.location
    }
  })
  template_content = <<TEMPLATE
{
  "$schema": "http://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "rule_group_name": {
      "type": "String"
    },
    "workspace_name": {
      "type": "String"
    },
    "cluster_name": {
      "type": "String"
    },
    "location": {
      "type": "String"
    }
  },
  "resources": [
    {
      "type": "Microsoft.AlertsManagement/prometheusRuleGroups",
      "apiVersion": "2021-07-22-preview",
      "name": "[parameters('rule_group_name')]",
      "location": "[parameters('location')]",
      "properties": {
          "enabled": true,
          "clusterName": "[parameters('cluster_name')]",
          "scopes": [
              "[resourceId('microsoft.monitor/accounts', parameters('workspace_name'))]"
          ],
          "rules": [
              {
                  "record": "instance:node_num_cpu:sum",
                  "expression": "count without (cpu, mode) (  node_cpu_seconds_total{job=\"node\",mode=\"idle\"})"
              },
              {
                  "record": "instance:node_cpu_utilisation:rate5m",
                  "expression": "1 - avg without (cpu) (  sum without (mode) (rate(node_cpu_seconds_total{job=\"node\", mode=~\"idle|iowait|steal\"}[5m])))"
              },
              {
                  "record": "instance:node_load1_per_cpu:ratio",
                  "expression": "(  node_load1{job=\"node\"}/  instance:node_num_cpu:sum{job=\"node\"})"
              },
              {
                  "record": "instance:node_memory_utilisation:ratio",
                  "expression": "1 - (  (    node_memory_MemAvailable_bytes{job=\"node\"}    or    (      node_memory_Buffers_bytes{job=\"node\"}      +      node_memory_Cached_bytes{job=\"node\"}      +      node_memory_MemFree_bytes{job=\"node\"}      +      node_memory_Slab_bytes{job=\"node\"}    )  )/  node_memory_MemTotal_bytes{job=\"node\"})"
              },
              {
                  "record": "instance:node_vmstat_pgmajfault:rate5m",
                  "expression": "rate(node_vmstat_pgmajfault{job=\"node\"}[5m])"
              },
              {
                  "record": "instance_device:node_disk_io_time_seconds:rate5m",
                  "expression": "rate(node_disk_io_time_seconds_total{job=\"node\", device!=\"\"}[5m])"
              },
              {
                  "record": "instance_device:node_disk_io_time_weighted_seconds:rate5m",
                  "expression": "rate(node_disk_io_time_weighted_seconds_total{job=\"node\", device!=\"\"}[5m])"
              },
              {
                  "record": "instance:node_network_receive_bytes_excluding_lo:rate5m",
                  "expression": "sum without (device) (  rate(node_network_receive_bytes_total{job=\"node\", device!=\"lo\"}[5m]))"
              },
              {
                  "record": "instance:node_network_transmit_bytes_excluding_lo:rate5m",
                  "expression": "sum without (device) (  rate(node_network_transmit_bytes_total{job=\"node\", device!=\"lo\"}[5m]))"
              },
              {
                  "record": "instance:node_network_receive_drop_excluding_lo:rate5m",
                  "expression": "sum without (device) (  rate(node_network_receive_drop_total{job=\"node\", device!=\"lo\"}[5m]))"
              },
              {
                  "record": "instance:node_network_transmit_drop_excluding_lo:rate5m",
                  "expression": "sum without (device) (  rate(node_network_transmit_drop_total{job=\"node\", device!=\"lo\"}[5m]))"
              }
          ],
          "interval": "PT1M"
      }
    }
  ]
}
TEMPLATE

  // NOTE: whilst we show an inline template here, we recommend
  // sourcing this from a file for readability/editor support
}

## Deploy the image proxy

provider "kubectl" {
  host                   = azurerm_kubernetes_cluster.aks.kube_config.0.host
  client_certificate     = base64decode(azurerm_kubernetes_cluster.aks.kube_config.0.client_certificate)
  client_key             = base64decode(azurerm_kubernetes_cluster.aks.kube_config.0.client_key)
  cluster_ca_certificate = base64decode(azurerm_kubernetes_cluster.aks.kube_config.0.cluster_ca_certificate)
  load_config_file       = false
}


resource "kubectl_manifest" "namespace" {
  yaml_body = <<YAML
apiVersion: v1
kind: Namespace
metadata:
  name: node-installer
YAML
}

resource "kubectl_manifest" "configuration" {
  yaml_body = <<YAML
apiVersion: v1
kind: ConfigMap
metadata:
  name: installer-config
  namespace: node-installer
data:
  install.sh: |
    #!/bin/bash

    # Update docker.io mirror
    mkdir -p /etc/containerd/certs.d/docker.io
    cat <<EOF > /etc/containerd/certs.d/docker.io/hosts.toml
    server = 'https://registry-1.docker.io'

    [host.'https://${var.azure_container_registry_enpoint}/v2']
    capabilities = ['pull', 'resolve']
    override_path = true
    EOF

    # Update credentials
    if grep "${var.azure_container_registry_enpoint}" /etc/containerd/config.toml; 
    then 
      echo "credentials already set, ignorning"
    else 
      cat <<EOF >> /etc/containerd/config.toml
    [plugins."io.containerd.grpc.v1.cri".registry.configs."${var.azure_container_registry_enpoint}".auth]
      username = "${var.azure_container_registry_username}"
      password = "${var.azure_container_registry_password}"
    EOF
      # Restart containerd
      systemctl restart containerd
    fi
YAML

  depends_on = [kubectl_manifest.namespace]
}

resource "kubectl_manifest" "daemonset" {
  yaml_body = <<YAML
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: installer
  namespace: node-installer
spec:
  selector:
    matchLabels:
      job: installer
  template:
    metadata:
      labels:
        job: installer
    spec:
      hostPID: true
      restartPolicy: Always
      containers:
      - image: patnaikshekhar/node-installer:1.3
        name: installer
        securityContext:
          privileged: true
        volumeMounts:
        - name: install-script
          mountPath: /tmp
        - name: host-mount
          mountPath: /host
        - name: containerd
          mountPath: /etc/containerd
      volumes:
      - name: install-script
        configMap:
          name: installer-config
      - name: containerd
        hostPath:
          path: /etc/containerd
          type: Directory
      - name: host-mount
        hostPath:
          path: /tmp/install
YAML

  depends_on = [kubectl_manifest.configuration]
}