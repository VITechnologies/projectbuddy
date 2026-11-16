"""Dedicated stdio MCP server for the synthetic UI's single action tool."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from threading import Thread
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, TextContent, Tool

from assisted_benchmark_synthetic_ui import SyntheticUiWorkload
from assisted_benchmark_synthetic_ui_server import create_server as create_ui_server


MCP_SERVER_NAME = "projectbuddy-synthetic-ui"
MCP_SERVER_VERSION = "1.0.0"
UI_TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "taskId": {"title": "Taskid", "type": "string"},
        "targetId": {"enum": ["approve", "hold"], "title": "Targetid", "type": "string"},
    },
    "required": ["taskId", "targetId"],
    "title": "click_targetArguments",
    "additionalProperties": False,
}


def create_mcp_server(workload: SyntheticUiWorkload) -> Server:
    async def list_tools(_context: Any, _params: Any) -> ListToolsResult:
        return ListToolsResult(tools=[
            Tool(
                name="click_target",
                description="Record one exact target choice in the current synthetic UI task.",
                inputSchema=UI_TOOL_INPUT_SCHEMA,
            )
        ])

    async def call_tool(_context: Any, request: CallToolRequestParams) -> CallToolResult:
        arguments = request.arguments
        if (request.name != "click_target" or not isinstance(arguments, dict)
                or set(arguments) != {"taskId", "targetId"}
                or not isinstance(arguments["taskId"], str)
                or not isinstance(arguments["targetId"], str)
                or arguments["targetId"] not in {"approve", "hold"}):
            return CallToolResult(isError=True, content=[TextContent(text="synthetic UI action arguments are invalid")])
        if arguments["taskId"] != workload.task_id:
            return CallToolResult(isError=True, content=[TextContent(text="synthetic UI task identity mismatch")])
        try:
            result = workload.submit_action(arguments["targetId"])
        except ValueError:
            return CallToolResult(isError=True, content=[TextContent(text="synthetic UI action was refused")])
        return CallToolResult(
            content=[TextContent(text=json.dumps(result, sort_keys=True, separators=(",", ":")))],
            structuredContent=result,
        )

    return Server(
        name=MCP_SERVER_NAME,
        version=MCP_SERVER_VERSION,
        instructions="Expose one policy-authorized action for the local synthetic UI task.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--ui-port", type=int, default=8765)
    args = parser.parse_args()
    workload = SyntheticUiWorkload(args.seed)
    ui_server = create_ui_server(workload, args.ui_port)
    ui_thread = Thread(target=ui_server.serve_forever, daemon=True)
    ui_thread.start()
    print(f"Synthetic UI bound to loopback port {ui_server.server_port}", file=sys.stderr)
    try:
        async def run_stdio():
            async with stdio_server() as (read_stream, write_stream):
                server = create_mcp_server(workload)
                await server.run(read_stream, write_stream, server.create_initialization_options(), raise_exceptions=True)

        asyncio.run(run_stdio())
    except KeyboardInterrupt:
        pass
    finally:
        ui_server.shutdown()
        ui_server.server_close()
        ui_thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())