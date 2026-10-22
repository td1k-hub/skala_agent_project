import os
import sys
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from .config import Settings


class UpstreamError(Exception):
    pass


class AnalysisSession:
    """Request-owned sessions: calls serialize per server, never across runs."""
    def __init__(self, sessions):
        self.sessions = sessions
        self.locks = {name: asyncio.Lock() for name in sessions}

    async def call(self, server, tool, arguments=None):
        async with self.locks[server]:
            result = await self.sessions[server].call_tool(tool, arguments or {})
            if result.isError or result.structuredContent is None:
                raise UpstreamError("MCP tool failed")
            return result.structuredContent


class Gateway:
    def __init__(self, settings: Settings):
        self.settings = settings

    @asynccontextmanager
    async def analysis_session(self):
        # Open/close both SDK task groups on this task, outside LangGraph nodes.
        # Sessions span the whole analysis; no global sharing or cross-run state.
        async with self.session("equipment") as equipment:
            async with self.session("knowledge") as knowledge:
                yield AnalysisSession({"equipment": equipment, "knowledge": knowledge})

    @asynccontextmanager
    async def session(self, server):
        # Keep SDK task groups and their cancel scopes on the calling task.
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMPDIR") if key in os.environ}
        env["PUMP_GUARD_DATABASE_PATH"] = str(self.settings.database_path.resolve())
        env["PUMP_GUARD_RESEARCH_DATABASE_PATH"] = str(self.settings.research_database_path.resolve())
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        params = StdioServerParameters(command=sys.executable, args=["-m", f"pump_guard.mcp_servers.{server}"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def call(self, server: str, tool: str, arguments: dict | None = None):
        async with self.session(server) as session:
            result = await session.call_tool(tool, arguments or {})
            if result.isError or result.structuredContent is None:
                raise UpstreamError("MCP tool failed")
            return result.structuredContent
