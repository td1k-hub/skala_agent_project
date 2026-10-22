from typing import Any
from mcp.server.fastmcp import FastMCP
from pump_guard import data
from pump_guard.models import InspectionRequest
from pump_guard.research_data import measured_history
from pump_guard.config import Settings

mcp = FastMCP("pump-guard-equipment-mock")


@mcp.tool()
def list_equipment() -> dict[str, Any]:
    """List synthetic pump specifications."""
    return {"items": list(data.PUMPS.values())}


@mcp.tool()
def get_equipment(equipment_id: str) -> dict[str, Any]:
    """Get synthetic pump specifications by equipment ID."""
    return data.require_pump(equipment_id)


@mcp.tool()
def get_operating_history(equipment_id: str) -> dict[str, Any]:
    """Return six synthetic hourly observations."""
    return data.history(equipment_id)


@mcp.tool()
def get_maintenance_history(equipment_id: str) -> dict[str, Any]:
    """Read the latest 100 persisted mock inspections."""
    return data.maintenance(equipment_id)


@mcp.tool()
def get_measured_history(equipment_id: str, cycle_id: int) -> dict[str, Any]:
    """Read up to five UCI hydraulic experimental cycles ending at the selected cycle.

    Only UCI-447 is allowed. These are not current plant readings or predictions.
    """
    if equipment_id != "UCI-447":
        raise ValueError("Experimental data cannot be assigned to another asset")
    return measured_history(cycle_id, Settings().research_database_path)


@mcp.tool()
def save_inspection(record: InspectionRequest) -> dict[str, Any]:
    """Persist a validated inspection result in SQLite."""
    return data.save_inspection(record)


if __name__ == "__main__":
    mcp.run(transport="stdio")
