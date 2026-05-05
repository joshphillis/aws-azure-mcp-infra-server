# Architecture: AWS/Azure AI Agent Stack

This document describes the full three-layer architecture connecting AI agents to real cloud infrastructure. It covers how the repositories relate to each other, how data flows through the system, and how to run the complete stack locally.

---

## The Problem This Solves

AI agents that interact with cloud infrastructure have a fundamental problem: they hallucinate. Without grounding, an agent asked "how do I scale the EKS node group?" will generate a plausible-sounding answer based on training data — not your actual infrastructure.

This stack solves that by giving agents access to two things:

1. **Live infrastructure state** — real-time queries against AWS and Azure via SDK
2. **Grounded institutional knowledge** — answers retrieved from your actual Terraform configs, Kubernetes manifests, runbooks, and architecture docs

The result is an AI agent that reasons from evidence, not assumptions.

---

## Three-Layer Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        LAYER 3: AGENT                          │
│                                                                 │
│              AI Agent (Claude / GPT / Custom)                  │
│         running on aws-bedrock-eks-agent-platform              │
│              or azure-openai-aks-agent-platform                │
└───────────────────────────────┬─────────────────────────────────┘
                                │ MCP Protocol
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                      LAYER 2: INTERFACE                        │
│                                                                 │
│                  aws-azure-mcp-infra-server                    │
│                                                                 │
│   ┌─────────────────┐  ┌──────────────────┐  ┌─────────────┐  │
│   │   AWS Tools     │  │   Azure Tools    │  │  RAG Tools  │  │
│   │  · EC2 / EKS    │  │  · VM / AKS      │  │  · /ask     │  │
│   │  · S3 / Lambda  │  │  · Storage       │  │  · /compare │  │
│   │  · Cost Explorer│  │  · Cost Mgmt     │  │             │  │
│   └─────────────────┘  └──────────────────┘  └──────┬──────┘  │
│                                                       │        │
│   ┌──────────────────────────────────────────────┐   │        │
│   │              Agent Tools                     │   │        │
│   │  · Memory store  · Task queue  · Health      │   │        │
│   └──────────────────────────────────────────────┘   │        │
└───────────────────────────────────────────────────────┼────────┘
                                                        │ HTTP
                                                        ▼
┌─────────────────────────────────────────────────────────────────┐
│                     LAYER 1: KNOWLEDGE                         │
│                                                                 │
│                   aws-azure-rag-pipeline                       │
│                                                                 │
│   ┌──────────────┐    ┌──────────────┐    ┌─────────────────┐  │
│   │   /ask       │    │   /compare   │    │   /ingest       │  │
│   │  retrieve +  │    │  Claude vs   │    │  text or        │  │
│   │  generate    │    │  OpenAI      │    │  directory      │  │
│   └──────┬───────┘    └──────┬───────┘    └────────┬────────┘  │
│          │                   │                     │           │
│          └───────────────────┴──────────┐          │           │
│                                         ▼          ▼           │
│                               ┌──────────────────────────┐     │
│                               │       Qdrant             │     │
│                               │   Vector Store           │     │
│                               │   671+ chunks            │     │
│                               │   7 repos indexed        │     │
│                               └──────────────────────────┘     │
│                                         │                      │
│                    ┌────────────────────┴─────────────────┐    │
│                    ▼                                       ▼    │
│           OpenAI Embeddings                     Claude / GPT-4o │
│           text-embedding-3-small                (swappable)     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Repository Map

| Repository | Layer | Purpose |
|---|---|---|
| [aws-azure-mcp-infra-server](https://github.com/joshphillis/aws-azure-mcp-infra-server) | Interface | MCP server — 12 tools exposed to AI agents |
| [aws-azure-rag-pipeline](https://github.com/joshphillis/aws-azure-rag-pipeline) | Knowledge | RAG pipeline — Claude/OpenAI, Qdrant, FastAPI |
| [agent-platform-cicd](https://github.com/joshphillis/agent-platform-cicd) | DevOps | CI/CD — GitHub Actions, EKS + AKS deployment |
| [aws-bedrock-eks-agent-platform](https://github.com/joshphillis/aws-bedrock-eks-agent-platform) | Agent | AI agent platform on AWS Bedrock + EKS |
| [azure-openai-aks-agent-platform](https://github.com/joshphillis/azure-openai-aks-agent-platform) | Agent | AI agent platform on Azure OpenAI + AKS |
| [aws-secure-agent-platform-terraform](https://github.com/joshphillis/aws-secure-agent-platform-terraform) | Base | Secure Terraform IaC for AWS agent platform |
| [azure-openai-secure-agent-platform-terraform](https://github.com/joshphillis/azure-openai-secure-agent-platform-terraform) | Base | Secure Terraform IaC for Azure agent platform |

---

## Data Flow: A Complete Request

Here is what happens when an AI agent asks "How do I configure IRSA for EKS?":

```
1. Agent calls MCP tool: query_knowledge_base(query="How do I configure IRSA for EKS?")

2. MCP server (aws-azure-mcp-infra-server) receives the tool call
   └── routes to rag_tools.py
   └── makes HTTP POST to aws-azure-rag-pipeline /ask

3. RAG pipeline receives the request
   └── embeds the query using OpenAI text-embedding-3-small (1536 dims)
   └── searches Qdrant for top-5 most similar chunks
   └── retrieves relevant chunks from your actual Terraform and K8s files

4. Claude (or OpenAI) receives the query + retrieved context
   └── generates a grounded answer citing [1][2][3] source chunks

5. Response travels back:
   RAG pipeline → MCP server → AI agent

6. Agent receives a cited, grounded answer from your real infrastructure code
   — not a hallucination
```

---

## Key Design Decisions

### Provider-agnostic LLM layer
The RAG pipeline separates retrieval (Qdrant) from generation (LLM). Claude and GPT-4o are swappable via a single environment variable — `LLM_PROVIDER=claude` or `LLM_PROVIDER=openai` — without touching retrieval logic.

### Auto-ingest on startup
When the RAG pipeline container starts with an empty Qdrant collection, it automatically ingests all configured infrastructure repos. On subsequent restarts, it detects existing data and skips ingestion. Zero manual steps after first run.

### MCP as the agent interface
The Model Context Protocol (MCP) is used as the standard interface between AI agents and infrastructure tools. This means the same MCP server works with Claude Desktop, Cursor, VS Code, and any agent platform that supports MCP — without code changes.

### Persistent vector storage
Qdrant data is stored in a named Docker volume and persists across container restarts. Data is only lost if the volume is explicitly deleted (`docker compose down -v`).

### Read-only infrastructure mount
Infrastructure repos are mounted into the RAG pipeline container as read-only (`ro`). The pipeline never modifies your source repos — it only reads them.

---

## Running the Full Stack Locally

### Prerequisites
- Docker Desktop
- OpenAI API key (for embeddings)
- Anthropic API key (for Claude answers)

### Step 1: Start the RAG pipeline

```bash
cd aws-azure-rag-pipeline
cp .env.example .env
# Add OPENAI_API_KEY, ANTHROPIC_API_KEY, set INGEST_ON_STARTUP=true
# Add your repo paths to INGEST_DIRS
docker compose up --build
```

The pipeline auto-ingests your repos on first start. Check `http://localhost:8000/info` for chunk count.

### Step 2: Start the MCP server

```bash
cd aws-azure-mcp-infra-server
cp .env.example .env
# Add AWS and Azure credentials
# Set RAG_PIPELINE_URL=http://host.docker.internal:8000
docker compose up --build
```

### Step 3: Open the chat UI

Open `aws-azure-rag-pipeline/ui.html` directly in your browser. No server needed.

### Step 4: Test the full chain

```bash
# Test RAG pipeline directly
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "How does the MCP server expose tools to agents?", "provider": "claude"}'

# Test through MCP server
curl -X POST http://localhost:8080/tool \
  -H "Content-Type: application/json" \
  -d '{"name": "query_knowledge_base", "arguments": {"query": "How does the MCP server expose tools to agents?"}}'
```

---

## What Gets Indexed

The RAG pipeline ingests the following content types from your infrastructure repos:

| Content Type | Extensions | Chunking Strategy |
|---|---|---|
| Documentation | `.md` `.txt` `.rst` | Split by heading, then ~512 tokens |
| Code | `.py` `.ts` `.go` `.tf` `.hcl` | Split by function/class, then 100 lines |
| Config | `.yaml` `.yml` `.json` | By line count |

Skipped: `.git`, `__pycache__`, `node_modules`, `.terraform`, `.venv`

---

## Ports Reference

| Service | Port | Purpose |
|---|---|---|
| RAG Pipeline API | `8000` | `/ask`, `/compare`, `/query`, `/ingest` |
| Qdrant REST | `6333` | Vector store REST API + dashboard |
| Qdrant gRPC | `6334` | Vector store gRPC |
| MCP Server | `8080` | MCP HTTP transport + `/tool` endpoint |
| Redis | `6379` | Agent memory store (MCP server) |

---

## CI/CD Pipeline

The `agent-platform-cicd` repo provides centralized GitHub Actions workflows for the entire ecosystem:

```
push to main
     │
     ▼
Build + Test + Push to GHCR
     │
     ▼
Deploy to Staging (EKS or AKS via Terraform)
     │
git tag v*
     │
     ▼
Deploy to Production (EKS or AKS via Terraform)
```

Each service repo has a caller workflow that references the reusable workflows in `agent-platform-cicd`. Adding a new service takes less than 5 minutes — copy an existing caller workflow, update the image name and cluster target, commit.

---

## Enterprise Use Case

This stack is designed as a template. Swap out the infrastructure repos for an organization's:

- Internal runbooks and SOPs
- Terraform state and configs
- Architecture decision records
- Incident playbooks
- Confluence or wiki exports

The result is an AI agent grounded in that organization's actual institutional knowledge — not generic training data. Answers are cited and traceable to source documents. This is particularly valuable in defense and government environments where accuracy is non-negotiable and institutional knowledge walks out the door with departing personnel.

---

## Security Notes

- Credentials are managed via `.env` files, never committed to Git
- Infrastructure repos are mounted read-only into Docker containers
- Use IAM roles (IRSA on EKS) and Workload Identity (AKS) instead of static credentials in production
- Qdrant has no auth by default — add API key authentication before exposing to a network

---

## Author

Joshua Phillis — Cloud Infrastructure & AI Platform Engineer  
Active Secret Clearance · AZ-104 · 25 years U.S. Army  
[github.com/joshphillis](https://github.com/joshphillis) · Fort Worth, TX
