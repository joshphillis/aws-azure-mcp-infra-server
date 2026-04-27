"""
AWS/Azure MCP Infrastructure Server
Exposes cloud infrastructure tools and agent memory/task management
via the Model Context Protocol (MCP).

Fixes applied (v1.1):
  - Structured JSON error responses with tool name, error type, and message
  - Execution timing logged and included in error responses
  - /health and /ready HTTP endpoints (required by Kubernetes liveness/readiness probes)
  - HTTP server runs alongside MCP stdio server in a background task
"""

import asyncio
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from tools.aws_tools import (
    get_aws_resources,
    get_aws_costs,
    get_infra_health_aws,
)
from tools.azure_tools import (
    get_azure_resources,
    get_azure_costs,
    get_infra_health_azure,
)
from tools.agent_tools import (
    store_agent_memory,
    get_agent_memory,
    add_agent_task,
    get_agent_tasks,
    complete_agent_task,
    get_infra_health_summary,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Server("aws-azure-mcp-infra-server")

# ── Health HTTP Server ─────────────────────────────────────────────────────────
# Kubernetes liveness (/health) and readiness (/ready) probes.
# Runs in a daemon thread so it doesn't block the MCP event loop.

_SERVER_READY = False


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/health", "/ready"):
            status = 200 if (_SERVER_READY or self.path == "/health") else 503
            body = json.dumps({"status": "ok" if status == 200 else "not_ready"}).encode()
        else:
            status, body = 404, b'{"error": "not found"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # suppress default access logs
        pass


def _start_health_server(port: int = 8080):
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health server listening on :{port}")


# ── Tool Registry ──────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # ── AWS Tools ──────────────────────────────────────────────
        Tool(
            name="get_aws_resources",
            description="List AWS resources (EC2 instances, EKS clusters, S3 buckets, Lambda functions) in a given region.",
            inputSchema={
                "type": "object",
                "properties": {
                    "region": {"type": "string", "description": "AWS region, e.g. us-east-1"},
                    "resource_type": {
                        "type": "string",
                        "enum": ["ec2", "eks", "s3", "lambda", "all"],
                        "description": "Type of resource to list",
                    },
                },
                "required": ["region"],
            },
        ),
        Tool(
            name="get_aws_costs",
            description="Retrieve AWS cost and usage data for the last N days.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Number of days to look back (default: 30)"},
                    "group_by": {
                        "type": "string",
                        "enum": ["SERVICE", "REGION", "LINKED_ACCOUNT"],
                        "description": "Dimension to group costs by",
                    },
                },
                "required": [],
            },
        ),
        # ── Azure Tools ────────────────────────────────────────────
        Tool(
            name="get_azure_resources",
            description="List Azure resources (VMs, AKS clusters, storage accounts, function apps) in a subscription or resource group.",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_group": {"type": "string", "description": "Resource group name (optional, lists all if omitted)"},
                    "resource_type": {
                        "type": "string",
                        "enum": ["vm", "aks", "storage", "function", "all"],
                        "description": "Type of resource to list",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_azure_costs",
            description="Retrieve Azure cost and usage data for the last N days.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Number of days to look back (default: 30)"},
                    "group_by": {
                        "type": "string",
                        "enum": ["ResourceType", "ResourceGroupName", "ServiceName"],
                        "description": "Dimension to group costs by",
                    },
                },
                "required": [],
            },
        ),
        # ── Agent Memory Tools ─────────────────────────────────────
        Tool(
            name="store_agent_memory",
            description="Store a key-value memory entry for an agent. Used to persist context, decisions, or state across agent runs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Unique identifier for the agent"},
                    "key": {"type": "string", "description": "Memory key"},
                    "value": {"type": "string", "description": "Memory value (can be JSON string)"},
                    "ttl_seconds": {"type": "integer", "description": "Time-to-live in seconds (optional)"},
                },
                "required": ["agent_id", "key", "value"],
            },
        ),
        Tool(
            name="get_agent_memory",
            description="Retrieve memory entries for an agent by agent_id and optional key.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Unique identifier for the agent"},
                    "key": {"type": "string", "description": "Specific memory key (optional, returns all if omitted)"},
                },
                "required": ["agent_id"],
            },
        ),
        Tool(
            name="add_agent_task",
            description="Add a task to an agent's task queue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Unique identifier for the agent"},
                    "task": {"type": "string", "description": "Task description"},
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                        "description": "Task priority level",
                    },
                    "metadata": {"type": "object", "description": "Optional metadata as key-value pairs"},
                },
                "required": ["agent_id", "task"],
            },
        ),
        Tool(
            name="get_agent_tasks",
            description="Retrieve the task queue for an agent, optionally filtered by status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Unique identifier for the agent"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "all"],
                        "description": "Filter tasks by status (default: pending)",
                    },
                },
                "required": ["agent_id"],
            },
        ),
        Tool(
            name="complete_agent_task",
            description="Mark an agent task as completed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Unique identifier for the agent"},
                    "task_id": {"type": "string", "description": "Task ID to mark complete"},
                    "result": {"type": "string", "description": "Optional result or output of the task"},
                },
                "required": ["agent_id", "task_id"],
            },
        ),
        Tool(
            name="get_infra_health_summary",
            description="Get an aggregated health summary across AWS and Azure infrastructure.",
            inputSchema={
                "type": "object",
                "properties": {
                    "include_costs": {"type": "boolean", "description": "Include cost summary (default: false)"},
                },
                "required": [],
            },
        ),
    ]


# ── Tool Dispatch ──────────────────────────────────────────────────────────────

_HANDLERS = {
    "get_aws_resources": get_aws_resources,
    "get_aws_costs": get_aws_costs,
    "get_azure_resources": get_azure_resources,
    "get_azure_costs": get_azure_costs,
    "store_agent_memory": store_agent_memory,
    "get_agent_memory": get_agent_memory,
    "add_agent_task": add_agent_task,
    "get_agent_tasks": get_agent_tasks,
    "complete_agent_task": complete_agent_task,
    "get_infra_health_summary": get_infra_health_summary,
}


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    start = time.monotonic()
    logger.info(f"Tool called: {name} args={arguments}")

    handler = _HANDLERS.get(name)
    if not handler:
        error_response = json.dumps({
            "error": f"Unknown tool: {name}",
            "code": "UNKNOWN_TOOL",
            "tool": name,
        })
        return [TextContent(type="text", text=error_response)]

    try:
        result = await handler(**arguments)
        elapsed = round(time.monotonic() - start, 3)
        logger.info(f"Tool {name} completed in {elapsed}s")
        return [TextContent(type="text", text=result)]

    except TypeError as e:
        # Bad arguments passed to handler
        elapsed = round(time.monotonic() - start, 3)
        logger.error(f"Tool {name} bad arguments after {elapsed}s: {e}")
        error_response = json.dumps({
            "error": f"Invalid arguments: {str(e)}",
            "code": "INVALID_ARGUMENTS",
            "tool": name,
            "elapsed_seconds": elapsed,
        })
        return [TextContent(type="text", text=error_response)]

    except Exception as e:
        elapsed = round(time.monotonic() - start, 3)
        logger.error(f"Tool {name} failed after {elapsed}s: {e}", exc_info=True)
        error_response = json.dumps({
            "error": str(e),
            "code": "TOOL_EXECUTION_ERROR",
            "tool": name,
            "elapsed_seconds": elapsed,
        })
        return [TextContent(type="text", text=error_response)]


# ── Entry Point ────────────────────────────────────────────────────────────────

async def main():
    global _SERVER_READY
    _start_health_server(port=8080)

    async with stdio_server() as (read_stream, write_stream):
        _SERVER_READY = True
        logger.info("MCP server ready")
        await app.run(read_stream, write_stream, app.create_initialization_options())

# ── HTTP Transport (for Docker / Kubernetes standalone mode) ───────────────────

# ── HTTP Transport (for Docker / Kubernetes standalone mode) ───────────────────

async def _handle_http_tool_call(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        raw = await asyncio.wait_for(reader.read(65536), timeout=30)
        request = raw.decode(errors="replace")
        first_line = request.split("\r\n")[0]
        parts = first_line.split(" ")
        method = parts[0] if parts else "GET"
        path = parts[1] if len(parts) > 1 else "/"

        if method == "GET" and path in ("/health", "/ready"):
            body = json.dumps({"status": "ok", "transport": "http"}).encode()
            header = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
            writer.write(header.encode() + body)
            await writer.drain()
            return

        if method == "POST" and path == "/tool":
            body_parts = request.split("\r\n\r\n", 1)
            body_str = body_parts[1] if len(body_parts) > 1 else "{}"
            payload = json.loads(body_str)
            tool_name = payload.get("name", "")
            arguments = payload.get("arguments", {})
            handler = _HANDLERS.get(tool_name)
            if not handler:
                result = json.dumps({"error": f"Unknown tool: {tool_name}", "code": "UNKNOWN_TOOL"})
                http_status = "404 Not Found"
            else:
                try:
                    result = await handler(**arguments)
                    http_status = "200 OK"
                except Exception as e:
                    result = json.dumps({"error": str(e), "code": "TOOL_EXECUTION_ERROR", "tool": tool_name})
                    http_status = "500 Internal Server Error"
            body = result.encode()
            header = f"HTTP/1.1 {http_status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
            writer.write(header.encode() + body)
            await writer.drain()
            return

        writer.write(b"HTTP/1.1 404 Not Found\r\n\r\n")
        await writer.drain()

    except Exception as e:
        logger.error(f"HTTP handler error: {e}")
    finally:
        writer.close()


async def run_http_mode(host: str = "0.0.0.0", port: int = 8080):
    """Run the MCP server in HTTP mode for Docker/Kubernetes deployments."""
    global _SERVER_READY
    server = await asyncio.start_server(_handle_http_tool_call, host, port)
    _SERVER_READY = True
    logger.info(f"MCP HTTP server listening on {host}:{port}")
    logger.info("Endpoints: GET /health  GET /ready  POST /tool")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    import os
    transport = os.getenv("TRANSPORT", "stdio").lower()
    if transport == "http":
        asyncio.run(run_http_mode(port=int(os.getenv("PORT", "8080"))))
    else:
        asyncio.run(main())
