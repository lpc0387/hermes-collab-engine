"""MCP Server for Hermes Collaboration Engine.
Registers collab-engine as a first-class MCP tool (same tier as process)."""
import json, os, subprocess, sys, asyncio
from pathlib import Path
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types

ENGINE_DIR = Path(__file__).resolve().parents[2]
HERMES_COLLAB = ENGINE_DIR / "hermes-collab"

server = Server("collab-engine")

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="collab_run",
            description="Run a collaboration engine task with WBS decomposition and parallel workers. "
                        "Accepts a task description, optional request file, agent type, concurrency, and timeout.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task description for the engine"},
                    "request_file": {"type": "string", "description": "Path to a request file (optional)"},
                    "agent": {"type": "string", "description": "Agent type (hermes, opencode, etc.)", "default": "hermes"},
                    "concurrency": {"type": "integer", "description": "Max concurrent workers", "default": 2},
                    "timeout": {"type": "integer", "description": "Max seconds per node", "default": 900},
                    "split_count": {"type": "integer", "description": "WBS split count", "default": 3},
                },
                "required": ["task"],
            },
        ),
        types.Tool(
            name="collab_status",
            description="Check status of a running or recent collaboration engine run.",
            inputSchema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run ID to check (optional, shows latest if omitted)"},
                },
            },
        ),
        types.Tool(
            name="collab_list_runs",
            description="List recent collaboration engine runs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of runs to show", "default": 10},
                },
            },
        ),
        types.Tool(
            name="collab_cancel",
            description="Cancel a running collaboration engine run.",
            inputSchema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run ID to cancel"},
                },
                "required": ["run_id"],
            },
        ),
    ]

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "collab_run":
        return await _run_engine(arguments)
    elif name == "collab_status":
        return await _check_status(arguments)
    elif name == "collab_list_runs":
        return await _list_runs(arguments)
    elif name == "collab_cancel":
        return await _cancel_run(arguments)
    raise ValueError(f"Unknown tool: {name}")


async def _run_engine(args: dict) -> list[types.TextContent]:
    task = args["task"]
    request_file = args.get("request_file", "")
    agent = args.get("agent", "hermes")
    concurrency = args.get("concurrency", 2)
    timeout = args.get("timeout", 900)
    split_count = args.get("split_count", 3)

    cmd = [str(HERMES_COLLAB), "run", task, "--agent", agent,
           "--concurrency", str(concurrency), "--timeout", str(timeout),
           "--split-count", str(split_count), "--json"]
    if request_file:
        cmd.extend(["--request-file", request_file])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(ENGINE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 30)
        output = stdout.decode() + ("\nSTDERR:\n" + stderr.decode() if stderr else "")
        return [types.TextContent(type="text", text=output[:50000])]
    except asyncio.TimeoutError:
        try: proc.kill()
        except: pass
        return [types.TextContent(type="text", text="TIMEOUT: Engine run exceeded timeout")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"ERROR: {e}")]


async def _check_status(args: dict) -> list[types.TextContent]:
    run_id = args.get("run_id", "")
    extra = ["--run-id", run_id] if run_id else []
    try:
        cmd = [str(HERMES_COLLAB), "status"] + extra + ["--json"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(ENGINE_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return [types.TextContent(type="text", text=stdout.decode()[:10000])]
    except Exception as e:
        return [types.TextContent(type="text", text=f"ERROR: {e}")]


async def _list_runs(args: dict) -> list[types.TextContent]:
    limit = args.get("limit", 10)
    try:
        cmd = [str(HERMES_COLLAB), "list", "--limit", str(limit), "--json"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(ENGINE_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return [types.TextContent(type="text", text=stdout.decode()[:10000])]
    except Exception as e:
        return [types.TextContent(type="text", text=f"ERROR: {e}")]


async def _cancel_run(args: dict) -> list[types.TextContent]:
    run_id = args["run_id"]
    try:
        cmd = [str(HERMES_COLLAB), "cancel", run_id, "--json"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(ENGINE_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return [types.TextContent(type="text", text=stdout.decode()[:10000])]
    except Exception as e:
        return [types.TextContent(type="text", text=f"ERROR: {e}")]


async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            InitializationOptions(
                server_name="collab-engine",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    asyncio.run(main())
