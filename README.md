# aws-azure-mcp-infra-server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that bridges **multi-cloud infrastructure** with **AI agent context management** and a **RAG-powered knowledge base**.

Designed to complement AI agent platforms running on EKS and AKS — agents can query live infrastructure state, track costs, maintain persistent memory, and retrieve grounded answers from an infrastructure knowledge base powered by Claude or OpenAI.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│              AI Agent (Claude / GPT)                    │
│       (aws-bedrock-eks-agent-platform)                  │
│       (azure-openai-aks-agent-platform)                 │
└──────────────────────────┬──────────────────────────────┘
                           │ MCP Protocol
                           ▼
┌─────────────────────────────────────────────────────────┐
│              aws-azure-mcp-infra-server                 │
│                                                         │
│  ┌──────────────────┐  ┌──────────────────────────┐    │
│  │  AWS Tools       │  │  Azure Tools             │    │
│  │  · EC2 / EKS     │  │  · VM / AKS              │    │
│  │  · S3 / Lambda   │  │  · Storage / Functions   │    │
│  │  · Cost Explorer │  │  · Cost Management       │    │
│  └──────────────────┘  └──────────────────────────┘    │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Agent Tools                                     │   │
│  │  · Memory store  (in-proc or Redis)              │   │
│  │  · Task queue    (priority-ordered)              │   │
│  │  · Health summary (aggregated, multi-cloud)      │   │
│  └──────────────────────────────────────────────────┘   │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │  RAG Tools (NEW)                                 │   │
│  │  · query_knowledge_base  → Claude or OpenAI      │   │
│  │  · compare_knowledge_base → both in parallel     │   │
│  └──────────────────────────────────────────────────┘   │
└───────────┬───────────────────────┬─────────────────────┘
            │                       │
     ┌──────▼──────┐      ┌─────────▼──────────────┐
     │  AWS/Azure  │      │  aws-azure-rag-pipeline │
     │  boto3/SDK  │      │  (Qdrant + Claude/GPT)  │
     └─────────────┘      └─────────────────────────┘
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
| `query_knowledge_base` | RAG query against infrastructure docs and code — answers powered by Claude or OpenAI |
| `compare_knowledge_base` | Same RAG query answered by both Claude and OpenAI in parallel |

---

## RAG Integration

The `query_knowledge_base` and `compare_knowledge_base` tools connect to the [aws-azure-rag-pipeline](https://github.com/joshphillis/aws-azure-rag-pipeline), which indexes infrastructure docs, Terraform configs, runbooks, and code from your AWS and Azure repos.

Agents use this tool to retrieve grounded, cited answers before taking infrastructure actions — eliminating hallucination and keeping agent decisions traceable to source.

Set `RAG_PIPELINE_URL` to point to your running RAG pipeline instance:

```
RAG_PIPELINE_URL=http://host.docker.internal:8000  # local Docker
RAG_PIPELINE_URL=http://rag-pipeline-service:8000  # Kubernetes
```

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

### 3. Test the RAG tool

```bash
curl -X POST http://localhost:8080/tool \
  -H "Content-Type: application/json" \
  -d '{
    "name": "query_knowledge_base",
    "arguments": {
      "query": "How does the EKS node group scale?",
      "provider": "claude"
    }
  }'
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
| `RAG_PIPELINE_URL` | URL of the aws-azure-rag-pipeline | No (default: `http://localhost:8000`) |

> **Production tip:** Use **IRSA** (EKS) or **Workload Identity** (AKS) instead of static credentials.

---

## Kubernetes Deployment

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

```json
{
  "mcpServers": {
    "infra": {
      "command": "python",
      "args": ["src/server.py"],
      "env": {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AZURE_SUBSCRIPTION_ID": "YOUR_SUB_ID",
        "RAG_PIPELINE_URL": "http://localhost:8000"
      }
    }
  }
}
```

---

## Related Repositories

| Repo | Description |
|------|-------------|
| [aws-azure-rag-pipeline](https://github.com/joshphillis/aws-azure-rag-pipeline) | Provider-agnostic RAG pipeline — Claude or OpenAI, Qdrant vector store |
| [aws-bedrock-eks-agent-platform](https://github.com/joshphillis/aws-bedrock-eks-agent-platform) | AI agent platform on AWS Bedrock + EKS |
| [azure-openai-aks-agent-platform](https://github.com/joshphillis/azure-openai-aks-agent-platform) | AI agent platform on Azure OpenAI + AKS |
| [aws-secure-agent-platform-terraform](https://github.com/joshphillis/aws-secure-agent-platform-terraform) | Secure Terraform IaC for AWS agent platform |
| [azure-openai-secure-agent-platform-terraform](https://github.com/joshphillis/azure-openai-secure-agent-platform-terraform) | Secure Terraform IaC for Azure agent platform |
| [agent-platform-cicd](https://github.com/joshphillis/agent-platform-cicd) | CI/CD pipeline for AWS and Azure agent platforms |

---

## License

MIT
