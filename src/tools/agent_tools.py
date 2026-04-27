"""
Agent memory and task management tools for the MCP server.
Provides persistent context and task queuing for AI agents running on
EKS/AKS platforms.

Storage backends:
  - Default: in-process dict (dev/testing)
  - Redis: set REDIS_URL for production use
"""

import json
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

from tools.aws_tools import get_infra_health_aws
from tools.azure_tools import get_infra_health_azure

# ── Storage Backend ────────────────────────────────────────────────────────────

_USE_REDIS = bool(os.getenv("REDIS_URL"))

if _USE_REDIS:
    import redis.asyncio as aioredis
    _redis_client = aioredis.from_url(os.getenv("REDIS_URL"), decode_responses=True)
else:
    # Simple in-process store — fine for dev, single-pod deployments
    _memory_store: dict[str, dict] = {}   # agent_id -> {key: value}
    _task_store: dict[str, list] = {}     # agent_id -> [task, ...]


# ── Memory Tools ───────────────────────────────────────────────────────────────

async def store_agent_memory(
    agent_id: str,
    key: str,
    value: str,
    ttl_seconds: Optional[int] = None,
) -> str:
    """Store a memory entry for an agent."""
    entry = {
        "value": value,
        "stored_at": datetime.utcnow().isoformat(),
        "expires_at": (
            (datetime.utcnow() + timedelta(seconds=ttl_seconds)).isoformat()
            if ttl_seconds else None
        ),
    }

    if _USE_REDIS:
        redis_key = f"memory:{agent_id}:{key}"
        await _redis_client.set(redis_key, json.dumps(entry), ex=ttl_seconds)
    else:
        if agent_id not in _memory_store:
            _memory_store[agent_id] = {}
        _memory_store[agent_id][key] = entry

    return json.dumps({
        "status": "stored",
        "agent_id": agent_id,
        "key": key,
        "expires_at": entry["expires_at"],
    })


async def get_agent_memory(agent_id: str, key: Optional[str] = None) -> str:
    """Retrieve memory entries for an agent."""
    if _USE_REDIS:
        if key:
            raw = await _redis_client.get(f"memory:{agent_id}:{key}")
            if not raw:
                return json.dumps({"agent_id": agent_id, "key": key, "value": None})
            entry = json.loads(raw)
            return json.dumps({"agent_id": agent_id, "key": key, **entry})
        else:
            pattern = f"memory:{agent_id}:*"
            keys = await _redis_client.keys(pattern)
            memories = {}
            for k in keys:
                field = k.split(":", 2)[2]
                raw = await _redis_client.get(k)
                if raw:
                    memories[field] = json.loads(raw)
            return json.dumps({"agent_id": agent_id, "memories": memories})
    else:
        agent_mem = _memory_store.get(agent_id, {})
        if key:
            entry = agent_mem.get(key)
            return json.dumps({"agent_id": agent_id, "key": key, **(entry or {"value": None})})
        return json.dumps({"agent_id": agent_id, "memories": agent_mem})


# ── Task Tools ─────────────────────────────────────────────────────────────────

async def add_agent_task(
    agent_id: str,
    task: str,
    priority: str = "medium",
    metadata: Optional[dict] = None,
) -> str:
    """Add a task to an agent's queue."""
    task_entry = {
        "task_id": str(uuid.uuid4()),
        "task": task,
        "priority": priority,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
        "metadata": metadata or {},
        "result": None,
    }

    if _USE_REDIS:
        list_key = f"tasks:{agent_id}"
        await _redis_client.rpush(list_key, json.dumps(task_entry))
    else:
        if agent_id not in _task_store:
            _task_store[agent_id] = []
        _task_store[agent_id].append(task_entry)

    return json.dumps({
        "status": "queued",
        "agent_id": agent_id,
        "task_id": task_entry["task_id"],
        "priority": priority,
    })


async def get_agent_tasks(agent_id: str, status: str = "pending") -> str:
    """Retrieve tasks for an agent filtered by status."""
    if _USE_REDIS:
        list_key = f"tasks:{agent_id}"
        raw_tasks = await _redis_client.lrange(list_key, 0, -1)
        all_tasks = [json.loads(t) for t in raw_tasks]
    else:
        all_tasks = _task_store.get(agent_id, [])

    if status != "all":
        filtered = [t for t in all_tasks if t["status"] == status]
    else:
        filtered = all_tasks

    # Sort by priority weight
    priority_weight = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    filtered.sort(key=lambda t: priority_weight.get(t.get("priority", "medium"), 2))

    return json.dumps({
        "agent_id": agent_id,
        "status_filter": status,
        "count": len(filtered),
        "tasks": filtered,
    }, indent=2)


async def complete_agent_task(agent_id: str, task_id: str, result: Optional[str] = None) -> str:
    """Mark a task as completed."""
    if _USE_REDIS:
        list_key = f"tasks:{agent_id}"
        raw_tasks = await _redis_client.lrange(list_key, 0, -1)
        tasks = [json.loads(t) for t in raw_tasks]
        found = False
        for t in tasks:
            if t["task_id"] == task_id:
                t["status"] = "completed"
                t["result"] = result
                t["updated_at"] = datetime.utcnow().isoformat()
                found = True
        if found:
            await _redis_client.delete(list_key)
            for t in tasks:
                await _redis_client.rpush(list_key, json.dumps(t))
        return json.dumps({"status": "completed" if found else "not_found", "task_id": task_id})
    else:
        tasks = _task_store.get(agent_id, [])
        for t in tasks:
            if t["task_id"] == task_id:
                t["status"] = "completed"
                t["result"] = result
                t["updated_at"] = datetime.utcnow().isoformat()
                return json.dumps({"status": "completed", "task_id": task_id})
        return json.dumps({"status": "not_found", "task_id": task_id})


# ── Aggregated Health ──────────────────────────────────────────────────────────

async def get_infra_health_summary(include_costs: bool = False) -> str:
    """Aggregated health summary across AWS and Azure."""
    aws_health = await get_infra_health_aws()
    azure_health = await get_infra_health_azure()

    overall = "healthy"
    if aws_health.get("status") != "healthy" or azure_health.get("status") != "healthy":
        overall = "degraded"
    if aws_health.get("status") == "error" or azure_health.get("status") == "error":
        overall = "error"

    summary = {
        "overall_status": overall,
        "providers": {
            "aws": aws_health,
            "azure": azure_health,
        },
        "queried_at": datetime.utcnow().isoformat(),
    }

    return json.dumps(summary, indent=2)
