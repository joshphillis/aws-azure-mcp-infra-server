# aws-azure-mcp-infra-server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that bridges **multi-cloud infrastructure** with **AI agent context management**.

Designed to complement AI agent platforms running on EKS and AKS — agents can query live infrastructure state, track costs, and maintain persistent memory and task queues across runs.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    AI Agent (Claude / GPT)               │
│              (aws-bedrock-eks-agent-platform)            │
│              (azure-openai-aks-agent-platform)           │
└────────────────────────┬────────────────────────────────┘
                         │ MCP Protocol
                         ▼
┌─────────────────────────────────────────────────────────┐
│              aws-azure-mcp-infra-server                  │
│                                                         │
│  ┌──────────────────┐    ┌──────────────────────────┐   │
│  │  AWS Tools       │    │  Azure Tools              │   │
│  │  · EC2 / EKS     │    │  · VM / AKS               │   │
│  │  · S3 / Lambda   │    │  · Storage / Functions    │   │
│  │  · Cost Explorer │    │  · Cost Management        │   │
│  └──────────────────┘    └──────────────────────────┘   │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Agent Tools                                      │   │
│  │  · Memory store  (in-proc or Redis)               │   │
│  │  · Task queue    (priority-ordered)               │   │
│  │  · Health summary (aggregated, multi-cloud)       │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
                    │               │
             ┌──────▼──────┐  ┌────▼──────┐
             │     AWS     │  │   Azure   │
             │  boto3 SDK  │  │  Azure SDK│
             └─────────────┘  └───────────┘
```

---

## Tools

| Tool | Description |
|------|-------------|
| `get_aws_resources` | List EC2, EKS, S3, Lambda resources by region |
| `get_aws_costs` | Cost Explorer data grouped by service/region |
| `get_azure_resources` | List VMs, AKS clusters, storage, function apps |
| `get_azure_costs` | Azure Cost Management grouped by service/RG |
| `store_agent_memory` | Persist key-value context for an agent |
| `get_agent_memory` | Retrieve stored agent context |
| `add_agent_task` | Enqueue a prioritized task for an agent |
| `get_agent_tasks` | Retrieve task queue (filterable by status) |
| `complete_agent_task` | Mark a task done with optional result |
| `get_infra_health_summary` | Aggregated health across AWS + Azure |

---

## Quickstart

### 1. Clone and configure

```bash
git clone https://github.com/joshphillis/aws-azure-mcp-infra-server.git
cd aws-azure-mcp-infra-server

cp .env.example .env
# Edit .env with your AWS and Azure credentials
```

### 2. Run locally with Docker Compose

```bash
docker compose up --build
```

This starts:
- The MCP server on port `8080`
- Redis on port `6379` (for persistent agent memory)

### 3. Run without Docker

```bash
pip install -r requirements.txt
export PYTHONPATH=src
python src/server.py
```

---

## Configuration

| Variable | Description | Required |
|----------|-------------|----------|
| `AWS_ACCESS_KEY_ID` | AWS access key | No (use IAM role) |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key | No (use IAM role) |
| `AWS_DEFAULT_REGION` | Default AWS region | No (default: `us-east-1`) |
| `AZURE_TENANT_ID` | Azure tenant ID | No (use Workload Identity) |
| `AZURE_CLIENT_ID` | Azure service principal client ID | No (use Workload Identity) |
| `AZURE_CLIENT_SECRET` | Azure service principal secret | No (use Workload Identity) |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID | Yes (for Azure tools) |
| `REDIS_URL` | Redis connection URL | No (uses in-process store) |

> **Production tip:** Use **IRSA** (EKS) or **Workload Identity** (AKS) instead of static credentials. See the Terraform module for the service account annotations.

---

## Kubernetes Deployment

Deploy to an existing EKS or AKS cluster:

```bash
cd terraform
terraform init
terraform apply \
  -var="image=ghcr.io/joshphillis/aws-azure-mcp-infra-server:latest" \
  -var="azure_subscription_id=YOUR_SUB_ID" \
  -var="replicas=2"
```

---

## Connecting to Claude / Cursor / VS Code

Add to your MCP client config:

```json
{
  "mcpServers": {
    "infra": {
      "command": "python",
      "args": ["src/server.py"],
      "env": {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AZURE_SUBSCRIPTION_ID": "YOUR_SUB_ID"
      }
    }
  }
}
```

---

## Related Repositories

| Repo | Description |
|------|-------------|
| [aws-bedrock-eks-agent-platform](https://github.com/joshphillis/aws-bedrock-eks-agent-platform) | AI agent platform on AWS Bedrock + EKS |
| [azure-openai-aks-agent-platform](https://github.com/joshphillis/azure-openai-aks-agent-platform) | AI agent platform on Azure OpenAI + AKS |
| [aws-secure-agent-platform-terraform](https://github.com/joshphillis/aws-secure-agent-platform-terraform) | Secure Terraform IaC for AWS agent platform |
| [azure-openai-secure-agent-platform-terraform](https://github.com/joshphillis/azure-openai-secure-agent-platform-terraform) | Secure Terraform IaC for Azure agent platform |

---

## License

MIT
