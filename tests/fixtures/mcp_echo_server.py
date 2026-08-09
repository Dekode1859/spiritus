"""Minimal newline-delimited stdio MCP server used only by live tests."""
from __future__ import annotations

import json
import os
import sys


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def result(request_id, value) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": value})


def main() -> None:
    marker_prefix = os.environ.get("SPIRITUS_MCP_PREFIX", "MCP")
    log_path = os.environ.get("SPIRITUS_MCP_LOG", "")
    for raw in sys.stdin:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        request_id = message.get("id")
        if log_path:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"method": method, "id": request_id}) + "\n")
        if method == "initialize":
            requested = message.get("params", {}).get("protocolVersion")
            result(request_id, {
                "protocolVersion": requested or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "spiritus-echo-fixture", "version": "1.0.0"},
            })
        elif method in {"notifications/initialized", "initialized"}:
            continue
        elif method == "ping":
            result(request_id, {})
        elif method == "tools/list":
            result(request_id, {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Return a Spiritus MCP validation marker",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "value": {
                                    "type": "string",
                                    "description": "Value to echo after the marker prefix",
                                }
                            },
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    }
                ]
            })
        elif method == "tools/call":
            params = message.get("params", {})
            if params.get("name") != "echo":
                result(request_id, {
                    "content": [{"type": "text", "text": "unknown tool"}],
                    "isError": True,
                })
                continue
            value = params.get("arguments", {}).get("value", "")
            result(request_id, {
                "content": [{"type": "text", "text": f"{marker_prefix}:{value}"}],
                "isError": False,
            })
        elif request_id is not None:
            send({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            })


if __name__ == "__main__":
    main()
