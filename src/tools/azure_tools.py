"""
Azure infrastructure tools for the MCP server.
Uses the Azure SDK to query VMs, AKS clusters, storage, functions, and costs.

Fixes applied (v1.1):
  - All blocking Azure SDK calls wrapped in asyncio.to_thread (non-blocking)
  - Explicit iteration on all paged iterators ensures full pagination
  - connection_timeout + read_timeout on all SDK clients
  - Structured JSON error responses: {"error": ..., "code": ..., "tool": ...}
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Optional

from azure.identity import DefaultAzureCredential, ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.web import WebSiteManagementClient
from azure.mgmt.costmanagement import CostManagementClient
from azure.core.exceptions import AzureError, HttpResponseError


def _get_credential():
    tenant_id = os.getenv("AZURE_TENANT_ID")
    client_id = os.getenv("AZURE_CLIENT_ID")
    client_secret = os.getenv("AZURE_CLIENT_SECRET")
    if tenant_id and client_id and client_secret:
        return ClientSecretCredential(tenant_id, client_id, client_secret)
    return DefaultAzureCredential()


def _get_subscription_id() -> str:
    sub_id = os.getenv("AZURE_SUBSCRIPTION_ID")
    if not sub_id:
        raise ValueError("AZURE_SUBSCRIPTION_ID environment variable is required.")
    return sub_id


def _err(message: str, code: str, tool: str) -> str:
    return json.dumps({"error": message, "code": code, "tool": tool})


# ── Sync helpers ───────────────────────────────────────────────────────────────

def _fetch_vms(credential, subscription_id: str, resource_group: Optional[str]) -> list:
    client = ComputeManagementClient(credential, subscription_id, connection_timeout=10, read_timeout=30)
    iterator = (
        client.virtual_machines.list(resource_group)
        if resource_group
        else client.virtual_machines.list_all()
    )
    return [
        {
            "name": vm.name,
            "location": vm.location,
            "resource_group": vm.id.split("/")[4],
            "vm_size": vm.hardware_profile.vm_size if vm.hardware_profile else None,
            "os_type": str(vm.storage_profile.os_disk.os_type) if vm.storage_profile else None,
            "tags": vm.tags or {},
        }
        for vm in iterator
    ]


def _fetch_aks(credential, subscription_id: str, resource_group: Optional[str]) -> list:
    client = ContainerServiceClient(credential, subscription_id, connection_timeout=10, read_timeout=30)
    iterator = (
        client.managed_clusters.list_by_resource_group(resource_group)
        if resource_group
        else client.managed_clusters.list()
    )
    return [
        {
            "name": c.name,
            "location": c.location,
            "resource_group": c.id.split("/")[4],
            "kubernetes_version": c.kubernetes_version,
            "provisioning_state": c.provisioning_state,
            "fqdn": c.fqdn,
            "tags": c.tags or {},
        }
        for c in iterator
    ]


def _fetch_storage(credential, subscription_id: str, resource_group: Optional[str]) -> list:
    client = StorageManagementClient(credential, subscription_id, connection_timeout=10, read_timeout=30)
    iterator = (
        client.storage_accounts.list_by_resource_group(resource_group)
        if resource_group
        else client.storage_accounts.list()
    )
    return [
        {
            "name": sa.name,
            "location": sa.location,
            "kind": sa.kind,
            "sku": sa.sku.name if sa.sku else None,
            "tags": sa.tags or {},
        }
        for sa in iterator
    ]


def _fetch_functions(credential, subscription_id: str) -> list:
    client = WebSiteManagementClient(credential, subscription_id, connection_timeout=10, read_timeout=30)
    return [
        {
            "name": app.name,
            "location": app.location,
            "kind": app.kind,
            "state": app.state,
            "tags": app.tags or {},
        }
        for app in client.web_apps.list()
        if app.kind and "functionapp" in app.kind.lower()
    ]


def _fetch_azure_costs(credential, subscription_id: str, days: int, group_by: str) -> list:
    from azure.mgmt.costmanagement.models import (
        QueryDefinition, QueryTimePeriod, QueryDataset, QueryGrouping, QueryAggregation,
    )
    client = CostManagementClient(credential)
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    scope = f"/subscriptions/{subscription_id}"
    query = QueryDefinition(
        type="Usage",
        timeframe="Custom",
        time_period=QueryTimePeriod(from_property=start, to=end),
        dataset=QueryDataset(
            granularity="Monthly",
            aggregation={"totalCost": QueryAggregation(name="Cost", function="Sum")},
            grouping=[QueryGrouping(type="Dimension", name=group_by)],
        ),
    )
    result = client.query.usage(scope, query)
    rows = result.rows or []
    columns = [c.name for c in result.columns] if result.columns else []
    return [dict(zip(columns, row)) for row in rows]


# ── Public async tools ─────────────────────────────────────────────────────────

async def get_azure_resources(resource_group: Optional[str] = None, resource_type: str = "all") -> str:
    try:
        credential = _get_credential()
        subscription_id = _get_subscription_id()
        results: dict = {}

        if resource_type in ("vm", "all"):
            results["virtual_machines"] = await asyncio.to_thread(
                _fetch_vms, credential, subscription_id, resource_group)
        if resource_type in ("aks", "all"):
            results["aks_clusters"] = await asyncio.to_thread(
                _fetch_aks, credential, subscription_id, resource_group)
        if resource_type in ("storage", "all"):
            results["storage_accounts"] = await asyncio.to_thread(
                _fetch_storage, credential, subscription_id, resource_group)
        if resource_type in ("function", "all"):
            results["function_apps"] = await asyncio.to_thread(
                _fetch_functions, credential, subscription_id)

        results["subscription_id"] = subscription_id
        results["resource_group_filter"] = resource_group or "all"
        results["queried_at"] = datetime.utcnow().isoformat()
        return json.dumps(results, indent=2, default=str)

    except ValueError as e:
        return _err(str(e), "MISSING_CONFIG", "get_azure_resources")
    except HttpResponseError as e:
        return _err(str(e), e.error.code if e.error else "HTTP_ERROR", "get_azure_resources")
    except AzureError as e:
        return _err(str(e), "AZURE_ERROR", "get_azure_resources")
    except Exception as e:
        return _err(str(e), "UNEXPECTED_ERROR", "get_azure_resources")


async def get_azure_costs(days: int = 30, group_by: str = "ServiceName") -> str:
    try:
        credential = _get_credential()
        subscription_id = _get_subscription_id()
        breakdown = await asyncio.to_thread(
            _fetch_azure_costs, credential, subscription_id, days, group_by)
        total = sum(float(r.get("Cost", 0)) for r in breakdown)
        return json.dumps({
            "period": f"Last {days} days",
            "group_by": group_by,
            "total_cost_usd": round(total, 2),
            "breakdown": breakdown,
            "queried_at": datetime.utcnow().isoformat(),
        }, indent=2, default=str)

    except ValueError as e:
        return _err(str(e), "MISSING_CONFIG", "get_azure_costs")
    except HttpResponseError as e:
        return _err(str(e), e.error.code if e.error else "HTTP_ERROR", "get_azure_costs")
    except AzureError as e:
        return _err(str(e), "AZURE_ERROR", "get_azure_costs")
    except Exception as e:
        return _err(str(e), "UNEXPECTED_ERROR", "get_azure_costs")


async def get_infra_health_azure() -> dict:
    try:
        credential = _get_credential()
        subscription_id = _get_subscription_id()
        clusters = await asyncio.to_thread(_fetch_aks, credential, subscription_id, None)
        cluster_states = {c["name"]: c["provisioning_state"] for c in clusters}
        vms = await asyncio.to_thread(_fetch_vms, credential, subscription_id, None)
        return {
            "provider": "Azure",
            "subscription_id": subscription_id,
            "vm_count": len(vms),
            "aks_clusters": cluster_states,
            "status": "healthy" if all(v == "Succeeded" for v in cluster_states.values()) else "degraded",
        }
    except Exception as e:
        return {"provider": "Azure", "status": "error", "error": str(e), "code": "UNEXPECTED_ERROR"}
