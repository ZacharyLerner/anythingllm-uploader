from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Workspace(Base):
    __tablename__ = "workspaces"
    id = Column(String, primary_key=True)
    name = Column(String(100), nullable=False)
    owners = Column(JSON, default=list)
    created_at = Column(
        DateTime, default=lambda: datetime.now(ZoneInfo("America/New_York"))
    )

    files = relationship("File", back_populates="workspace")
    scrape_jobs = relationship("ScrapeJob", back_populates="workspace")


class ScrapeJob(Base):
    __tablename__ = "scrape_jobs"
    id = Column(String, primary_key=True)  # UUID
    workspace_id = Column(String, ForeignKey("workspaces.id"), nullable=False)
    name = Column(String(200), nullable=False)
    base_url = Column(String, nullable=False)
    mode = Column(String, default="depth")          # depth | prefix | single
    max_depth = Column(Integer, default=2)
    max_pages = Column(Integer, default=100)
    allow_offsite = Column(Boolean, default=False)
    schedule_interval = Column(String, nullable=True)  # hourly | daily | weekly | None
    last_scraped_at = Column(DateTime, nullable=True)
    next_scrape_at = Column(DateTime, nullable=True)
    is_running = Column(Boolean, default=False)
    created_at = Column(
        DateTime, default=lambda: datetime.now(ZoneInfo("America/New_York"))
    )

    workspace = relationship("Workspace", back_populates="scrape_jobs")
    files = relationship("File", back_populates="scrape_job", cascade="all, delete-orphan")


class File(Base):
    __tablename__ = "files"
    id = Column(String, primary_key=True)
    filename = Column(String, nullable=False)
    original_extension = Column(String, nullable=True)
    workspace_id = Column(String, ForeignKey("workspaces.id"), nullable=False)
    category = Column(String, nullable=False, default="uploaded_file")
    source_url = Column(String, nullable=True)
    scrape_job_id = Column(String, ForeignKey("scrape_jobs.id"), nullable=True)
    content_hash = Column(String, nullable=True)
    last_checked_at = Column(DateTime, nullable=True)

    uploaded_at = Column(
        DateTime, default=lambda: datetime.now(ZoneInfo("America/New_York"))
    )

    workspace = relationship("Workspace", back_populates="files")
    scrape_job = relationship("ScrapeJob", back_populates="files")
