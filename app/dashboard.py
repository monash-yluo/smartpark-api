"""Operator dashboard page for SmartPark."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()
_DASHBOARD_FILE = Path(__file__).resolve().parent / "static" / "dashboard.html"


@router.get("/dashboard", include_in_schema=False)
@router.get("/dashboard/", include_in_schema=False)
def dashboard() -> FileResponse:
    """Serve the client-side operational dashboard."""
    return FileResponse(_DASHBOARD_FILE, media_type="text/html")
