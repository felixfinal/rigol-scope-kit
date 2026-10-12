"""Read-only acceptance through real MCP stdio, including image/resource delivery."""
import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

parser = argparse.ArgumentParser()
parser.add_argument("--command", required=True)
parser.add_argument("--server-arg", action="append", default=[], help="Repeat for arguments such as SSH host and remote MCP runner")
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--device", default="bench-scope")
args = parser.parse_args()


async def main():
    args.output.mkdir(parents=True, exist_ok=True)
    async with stdio_client(StdioServerParameters(command=args.command, args=args.server_arg)) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            assert {"scope_identify", "scope_status", "scope_screenshot", "scope_generator_status", "scope_capabilities", "scope_doctor"} <= set(names), names
            identity = await session.call_tool("scope_identify", {"device": args.device})
            assert not identity.isError
            screenshot = await session.call_tool("scope_screenshot", {"device": args.device})
            assert not screenshot.isError
            meta = screenshot.structuredContent
            images = [c for c in screenshot.content if c.type == "image"]
            assert len(images) == 1
            png = base64.b64decode(images[0].data, validate=True)
            assert hashlib.sha256(png).hexdigest() == meta["sha256"]
            resource = await session.read_resource(meta["image_uri"])
            assert base64.b64decode(resource.contents[0].blob) == png
            (args.output / "mcp-screen.png").write_bytes(png)
            summary = {"tools": names, "identity": identity.structuredContent,
                       "screenshot": meta, "inline_image_verified": True, "resource_bytes_verified": True}
            (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({"tools": len(names), "inline_image_verified": True, "resource_bytes_verified": True,
                              "image_bytes": len(png), "capture_id": meta["capture_id"]}))


asyncio.run(main())
