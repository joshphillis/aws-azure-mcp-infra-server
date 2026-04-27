"""
AWS infrastructure tools for the MCP server.
Uses boto3 to query EC2, EKS, S3, Lambda, and Cost Explorer.

Fixes applied (v1.1):
  - All blocking boto3 calls wrapped in asyncio.to_thread (non-blocking)
  - Full pagination via paginators for EC2, Lambda; loop for EKS
  - connect_timeout + read_timeout on all clients
  - Structured JSON error responses: {"error": ..., "code": ..., "tool": ...}
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, NoCredentialsError

# 10s connect, 30s read — prevents event loop stalls on slow/hung AWS calls
_BOTO_CONFIG = Config(
    connect_timeout=10,
    read_timeout=30,
    retries={"max_attempts": 2, "mode": "standard"},
)


def _get_boto3_session(region: str = "us-east-1") -> boto3.Session:
    """Create a boto3 session using environment credentials or IAM role."""
    return boto3.Session(
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=os.getenv("AWS_SESSION_TOKEN"),
        region_name=region,
    )


def _err(message: str, code: str, tool: str) -> str:
    """Return a structured JSON error string."""
    return json.dumps({"error": message, "code": code, "tool": tool})


# ── Sync helpers (run inside asyncio.to_thread) ────────────────────────────────

def _fetch_ec2(session: boto3.Session) -> list[dict]:
    """Paginate all EC2 instances."""
    ec2 = session.client("ec2", config=_BOTO_CONFIG)
    paginator = ec2.get_paginator("describe_instances")
    instances = []
    for page in paginator.paginate():
        for reservation in page.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                instances.append({
                    "id": inst["InstanceId"],
                    "name": tags.get("Name", inst["InstanceId"]),
                    "type": inst["InstanceType"],
                    "state": inst["State"]["Name"],
                    "az": inst["Placement"]["AvailabilityZone"],
                    "tags": tags,
                })
    return instances


def _fetch_eks(session: boto3.Session) -> list[dict]:
    """Paginate all EKS clusters (list_clusters paginates via nextToken)."""
    eks = session.client("eks", config=_BOTO_CONFIG)
    clusters = []
    kwargs: dict = {}
    while True:
        resp = eks.list_clusters(**kwargs)
        for name in resp.get("clusters", []):
            info = eks.describe_cluster(name=name)["cluster"]
            clusters.append({
                "name": name,
                "status": info["status"],
                "version": info["version"],
                "endpoint": info.get("endpoint", ""),
                "tags": info.get("tags", {}),
            })
        next_token = resp.get("nextToken")
        if not next_token:
            break
        kwargs["nextToken"] = next_token
    return clusters


def _fetch_s3(session: boto3.Session) -> list[dict]:
    """List S3 buckets (global, no pagination needed — max 1000 buckets per account)."""
    s3 = session.client("s3", config=_BOTO_CONFIG)
    buckets = s3.list_buckets().get("Buckets", [])
    return [{"name": b["Name"], "created": b["CreationDate"].isoformat()} for b in buckets]


def _fetch_lambda(session: boto3.Session) -> list[dict]:
    """Paginate all Lambda functions."""
    lmb = session.client("lambda", config=_BOTO_CONFIG)
    paginator = lmb.get_paginator("list_functions")
    funcs = []
    for page in paginator.paginate():
        for f in page.get("Functions", []):
            funcs.append({
                "name": f["FunctionName"],
                "runtime": f.get("Runtime", "N/A"),
                "memory": f["MemorySize"],
                "last_modified": f["LastModified"],
                "tags": f.get("Tags", {}),
            })
    return funcs


def _fetch_costs(session: boto3.Session, days: int, group_by: str) -> dict:
    """Fetch AWS Cost Explorer data (no paginator — single call is sufficient)."""
    ce = session.client("ce", region_name="us-east-1", config=_BOTO_CONFIG)
    end = datetime.utcnow().date()
    start = end - timedelta(days=days)

    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": group_by}],
    )

    results = []
    for period in resp.get("ResultsByTime", []):
        for group in period.get("Groups", []):
            results.append({
                "period_start": period["TimePeriod"]["Start"],
                "period_end": period["TimePeriod"]["End"],
                group_by.lower(): group["Keys"][0],
                "cost_usd": round(float(group["Metrics"]["UnblendedCost"]["Amount"]), 2),
                "currency": group["Metrics"]["UnblendedCost"]["Unit"],
            })
    return results


# ── Public async tools ─────────────────────────────────────────────────────────

async def get_aws_resources(region: str = "us-east-1", resource_type: str = "all") -> str:
    """List AWS resources in a given region — fully paginated, non-blocking."""
    try:
        session = _get_boto3_session(region)
        results: dict = {}

        if resource_type in ("ec2", "all"):
            results["ec2_instances"] = await asyncio.to_thread(_fetch_ec2, session)

        if resource_type in ("eks", "all"):
            results["eks_clusters"] = await asyncio.to_thread(_fetch_eks, session)

        if resource_type in ("s3", "all"):
            results["s3_buckets"] = await asyncio.to_thread(_fetch_s3, session)

        if resource_type in ("lambda", "all"):
            results["lambda_functions"] = await asyncio.to_thread(_fetch_lambda, session)

        results["region"] = region
        results["queried_at"] = datetime.utcnow().isoformat()
        return json.dumps(results, indent=2)

    except NoCredentialsError:
        return _err(
            "AWS credentials not configured. Set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY or use an IAM role.",
            "NO_CREDENTIALS",
            "get_aws_resources",
        )
    except ClientError as e:
        return _err(str(e), e.response["Error"]["Code"], "get_aws_resources")
    except Exception as e:
        return _err(str(e), "UNEXPECTED_ERROR", "get_aws_resources")


async def get_aws_costs(days: int = 30, group_by: str = "SERVICE") -> str:
    """Retrieve AWS cost and usage data — non-blocking."""
    try:
        session = _get_boto3_session()
        breakdown = await asyncio.to_thread(_fetch_costs, session, days, group_by)
        total = sum(r["cost_usd"] for r in breakdown)
        return json.dumps({
            "period": f"Last {days} days",
            "group_by": group_by,
            "total_cost_usd": round(total, 2),
            "breakdown": breakdown,
            "queried_at": datetime.utcnow().isoformat(),
        }, indent=2)

    except NoCredentialsError:
        return _err("AWS credentials not configured.", "NO_CREDENTIALS", "get_aws_costs")
    except ClientError as e:
        return _err(str(e), e.response["Error"]["Code"], "get_aws_costs")
    except Exception as e:
        return _err(str(e), "UNEXPECTED_ERROR", "get_aws_costs")


async def get_infra_health_aws(region: str = "us-east-1") -> dict:
    """Return a health summary dict for AWS (used by the aggregated health tool)."""
    try:
        session = _get_boto3_session(region)

        instances = await asyncio.to_thread(_fetch_ec2, session)
        states: dict = {}
        for inst in instances:
            s = inst["state"]
            states[s] = states.get(s, 0) + 1

        clusters = await asyncio.to_thread(_fetch_eks, session)
        cluster_statuses = {c["name"]: c["status"] for c in clusters}

        return {
            "provider": "AWS",
            "region": region,
            "ec2_instance_states": states,
            "eks_clusters": cluster_statuses,
            "status": "healthy" if all(v == "ACTIVE" for v in cluster_statuses.values()) else "degraded",
        }
    except Exception as e:
        return {"provider": "AWS", "status": "error", "error": str(e), "code": "UNEXPECTED_ERROR"}
