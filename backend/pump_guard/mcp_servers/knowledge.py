from typing import Any
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from typing import Annotated
from pump_guard.data import MANUALS

mcp = FastMCP("pump-guard-knowledge-mock")


@mcp.tool()
def search_manuals(query: Annotated[str, Field(min_length=1, max_length=100)]) -> dict[str, Any]:
    """Search synthetic fault symptoms and troubleshooting guides (not an LLM)."""
    tokens = query.casefold().split()
    matches = [item for item in MANUALS if any(token in " ".join([item["fault"], *item["symptoms"]]).casefold() for token in tokens)]
    return {"items": matches[:5], "data_source": "synthetic"}


if __name__ == "__main__":
    mcp.run(transport="stdio")
