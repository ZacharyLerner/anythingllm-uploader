from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime
from typing import Optional


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
    source_url: str | None = None
    scrape_job_id: str | None = None
    content_hash: str | None = None
    last_checked_at: datetime | None = None


class WorkspaceBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class WorkspaceCreate(WorkspaceBase):
    id: str
    owners: list[str] = []


class WorkspaceUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class WorkspaceResponse(WorkspaceBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    owners: list[str] = []
    created_at: datetime
    files: list[FileResponse] = []


class ScrapeJobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    base_url: str
    mode: str = "depth"          # depth | prefix | single
    max_depth: int = 2
    max_pages: int = 100
    allow_offsite: bool = False
    schedule_interval: Optional[str] = None  # hourly | daily | weekly | None


class ScrapeJobUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    schedule_interval: Optional[str] = None
    base_url: Optional[str] = None
    mode: Optional[str] = None
    max_depth: Optional[int] = None
    max_pages: Optional[int] = None
    allow_offsite: Optional[bool] = None


class ScrapeJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    workspace_id: str
    name: str
    base_url: str
    mode: str
    max_depth: int
    max_pages: int
    allow_offsite: bool
    schedule_interval: Optional[str] = None
    last_scraped_at: Optional[datetime] = None
    next_scrape_at: Optional[datetime] = None
    is_running: bool
    created_at: datetime
    page_count: int = 0
