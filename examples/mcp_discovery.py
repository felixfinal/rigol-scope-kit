"""Real stdio MCP discovery with an offline configuration; never calls SCPI tools."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def tool_object(reply):
    if reply.structuredContent is not None:
        return reply.structuredContent
    # MCP clients must also support tools returning JSON in TextContent.
    return json.loads(next(item.text for item in reply.content if item.type == "text"))


async def discover():
    with tempfile.TemporaryDirectory(prefix="scope-discovery-") as directory:
        config = Path(directory) / "devices.toml"
        config.write_text('schema_version=1\nstate_dir="state"\n[devices.bench-scope]\nhost="192.0.2.10"\nport=5555\n')
        command = StdioServerParameters(command=sys.executable, args=["-m", "rigol_scope.mcp_server", "--config", str(config)])
        async with stdio_client(command) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                contract = await session.call_tool("scope_contract", {"section": "overview"})
                profile = await session.call_tool("scope_profile_get", {"profile_id": "passive-10x"})
                skill = await session.read_resource("scope://skill/SKILL.md")
                if contract.isError or profile.isError or len(tools.tools) != 41 or not skill.contents:
                    raise RuntimeError("Offline MCP discovery failed")
                result = tool_object(contract)
                defaults = tool_object(profile)
                if result.get("instrument_access") is not False or defaults.get("instrument_access") is not False:
                    raise RuntimeError("Discovery must be offline")
                print(json.dumps({"software_version": result["software_version"], "tools": len(tools.tools),
                                  "profile": defaults["profile"]["profile_id"],
                                  "skill_resource": True, "instrument_access": False}))


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(discover(), timeout=30))
