"""
RAG pipeline tools for the MCP server.
Connects to the aws-azure-rag-pipeline to provide grounded,
context-aware answers about infrastructure docs and code.
"""

import json
import os
import urllib.request
import urllib.error

RAG_BASE_URL = os.getenv("RAG_PIPELINE_URL", "http://localhost:8000")


def _post(endpoint: str, payload: dict) -> dict:
    """Simple synchronous HTTP POST to the RAG pipeline."""
    url = f"{RAG_BASE_URL}{endpoint}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return {"error": f"RAG pipeline HTTP {e.code}: {body}"}
    except urllib.error.URLError as e:
        return {"error": f"RAG pipeline unreachable: {str(e.reason)}"}
    except Exception as e:
        return {"error": f"RAG pipeline error: {str(e)}"}


async def query_knowledge_base(
    query: str,
    provider: str = "claude",
    top_k: int = 5,
    doc_type: str = None,
) -> str:
    """
    Query the infrastructure knowledge base using RAG.
    Returns a grounded answer with source citations.

    Args:
        query: Natural language question about infrastructure
        provider: LLM provider — 'claude' (default) or 'openai'
        top_k: Number of chunks to retrieve (default: 5)
        doc_type: Filter by type — 'docs', 'code', or 'runbook' (optional)
    """
    payload = {
        "query": query,
        "provider": provider,
        "top_k": top_k,
    }
    if doc_type:
        payload["doc_type"] = doc_type

    result = _post("/ask", payload)

    if "error" in result:
        return json.dumps({
            "error": result["error"],
            "tool": "query_knowledge_base",
            "query": query,
        })

    sources = [
        {
            "source": s.get("source", "unknown"),
            "doc_type": s.get("doc_type", "unknown"),
            "score": round(s.get("score", 0), 3),
        }
        for s in result.get("sources", [])
    ]

    return json.dumps({
        "query": query,
        "answer": result.get("answer", ""),
        "provider": result.get("provider", provider),
        "model": result.get("model", ""),
        "sources": sources,
        "elapsed_seconds": result.get("elapsed_seconds", 0),
    }, indent=2)


async def compare_knowledge_base(
    query: str,
    top_k: int = 5,
) -> str:
    """
    Query the infrastructure knowledge base with both Claude and OpenAI
    and return both answers for comparison.

    Args:
        query: Natural language question about infrastructure
        top_k: Number of chunks to retrieve (default: 5)
    """
    payload = {
        "query": query,
        "top_k": top_k,
    }

    result = _post("/compare", payload)

    if "error" in result:
        return json.dumps({
            "error": result["error"],
            "tool": "compare_knowledge_base",
            "query": query,
        })

    return json.dumps({
        "query": query,
        "claude": result.get("claude", {}),
        "openai": result.get("openai", {}),
        "elapsed_seconds": result.get("elapsed_seconds", 0),
    }, indent=2)