from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime


class FileBase(BaseModel):
    filename: str


class FileCreate(FileBase):
    workspace_id: str


class FileResponse(FileBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    workspace_id: str
    original_extension: str | None = None
    category: str
    uploaded_at: datetime


class WorkspaceBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class WorkspaceCreate(WorkspaceBase):
    id: str
    owners: list[str] = []


class WorkspaceResponse(WorkspaceBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    owners: list[str] = []
    created_at: datetime
    files: list[FileResponse] = []
