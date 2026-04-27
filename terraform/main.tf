terraform {
  required_version = ">= 1.5.0"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.27"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.13"
    }
  }
}

# ── Variables ──────────────────────────────────────────────────────────────────

variable "namespace" {
  description = "Kubernetes namespace to deploy into"
  type        = string
  default     = "mcp-system"
}

variable "image" {
  description = "Docker image for the MCP server"
  type        = string
  default     = "ghcr.io/joshphillis/aws-azure-mcp-infra-server:latest"
}

variable "replicas" {
  description = "Number of MCP server replicas"
  type        = number
  default     = 2
}

variable "redis_enabled" {
  description = "Deploy Redis alongside the MCP server"
  type        = bool
  default     = true
}

variable "aws_region" {
  description = "AWS region for resource queries"
  type        = string
  default     = "us-east-1"
}

variable "azure_subscription_id" {
  description = "Azure subscription ID"
  type        = string
  default     = ""
}

# ── Namespace ──────────────────────────────────────────────────────────────────

resource "kubernetes_namespace" "mcp" {
  metadata {
    name = var.namespace
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

# ── Secret (credentials) ───────────────────────────────────────────────────────

resource "kubernetes_secret" "mcp_credentials" {
  metadata {
    name      = "mcp-credentials"
    namespace = kubernetes_namespace.mcp.metadata[0].name
  }

  data = {
    AWS_DEFAULT_REGION    = var.aws_region
    AZURE_SUBSCRIPTION_ID = var.azure_subscription_id
    # In production: inject via External Secrets Operator or CSI Secret Store
  }
}

# ── Deployment ─────────────────────────────────────────────────────────────────

resource "kubernetes_deployment" "mcp_server" {
  metadata {
    name      = "mcp-infra-server"
    namespace = kubernetes_namespace.mcp.metadata[0].name
    labels = {
      app = "mcp-infra-server"
    }
  }

  spec {
    replicas = var.replicas

    selector {
      match_labels = {
        app = "mcp-infra-server"
      }
    }

    template {
      metadata {
        labels = {
          app = "mcp-infra-server"
        }
        annotations = {
          # Enables KEDA or HPA based on custom metrics
          "prometheus.io/scrape" = "true"
          "prometheus.io/port"   = "8080"
        }
      }

      spec {
        service_account_name = kubernetes_service_account.mcp.metadata[0].name

        container {
          name  = "mcp-server"
          image = var.image

          port {
            container_port = 8080
          }

          env_from {
            secret_ref {
              name = kubernetes_secret.mcp_credentials.metadata[0].name
            }
          }

          dynamic "env" {
            for_each = var.redis_enabled ? [1] : []
            content {
              name  = "REDIS_URL"
              value = "redis://mcp-redis:6379/0"
            }
          }

          resources {
            requests = {
              cpu    = "100m"
              memory = "256Mi"
            }
            limits = {
              cpu    = "500m"
              memory = "512Mi"
            }
          }

          liveness_probe {
            http_get {
              path = "/health"
              port = 8080
            }
            initial_delay_seconds = 15
            period_seconds        = 20
          }

          readiness_probe {
            http_get {
              path = "/ready"
              port = 8080
            }
            initial_delay_seconds = 5
            period_seconds        = 10
          }
        }
      }
    }
  }
}

# ── Service ────────────────────────────────────────────────────────────────────

resource "kubernetes_service" "mcp_server" {
  metadata {
    name      = "mcp-infra-server"
    namespace = kubernetes_namespace.mcp.metadata[0].name
  }

  spec {
    selector = {
      app = "mcp-infra-server"
    }

    port {
      port        = 80
      target_port = 8080
    }

    type = "ClusterIP"
  }
}

# ── Service Account (for IRSA/Workload Identity) ───────────────────────────────

resource "kubernetes_service_account" "mcp" {
  metadata {
    name      = "mcp-infra-server"
    namespace = kubernetes_namespace.mcp.metadata[0].name
    annotations = {
      # EKS IRSA — set the IAM role ARN here
      # "eks.amazonaws.com/role-arn" = "arn:aws:iam::ACCOUNT_ID:role/mcp-infra-role"
      # AKS Workload Identity
      # "azure.workload.identity/client-id" = var.azure_client_id
    }
  }
}

# ── Redis (optional) ───────────────────────────────────────────────────────────

resource "helm_release" "redis" {
  count = var.redis_enabled ? 1 : 0

  name       = "mcp-redis"
  namespace  = kubernetes_namespace.mcp.metadata[0].name
  repository = "https://charts.bitnami.com/bitnami"
  chart      = "redis"
  version    = "19.5.0"

  set {
    name  = "architecture"
    value = "standalone"
  }

  set {
    name  = "auth.enabled"
    value = "false"
  }

  set {
    name  = "master.persistence.enabled"
    value = "true"
  }
}

# ── Outputs ────────────────────────────────────────────────────────────────────

output "mcp_service_name" {
  value = kubernetes_service.mcp_server.metadata[0].name
}

output "mcp_namespace" {
  value = kubernetes_namespace.mcp.metadata[0].name
}
