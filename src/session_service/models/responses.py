"""API response models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class UploadFileResponse(BaseModel):
    """Response for POST /sessions/{id}/upload."""

    path: str
    size: int
    persisted: bool = True
    sandbox_synced: bool = Field(
        default=False,
        serialization_alias="sandboxSynced",
    )

    model_config = {"populate_by_name": True}
