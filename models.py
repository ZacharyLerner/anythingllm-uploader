from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, JSON
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

class File(Base):
    __tablename__ = "files"
    id = Column(String, primary_key=True)
    filename = Column(String, nullable=False)
    original_extension = Column(String, nullable=True)
    workspace_id = Column(String, ForeignKey("workspaces.id"), nullable=False)
    category = Column(String, nullable=False, default="uploaded_file")
    source_url = Column(String, nullable=True)

    uploaded_at = Column(
        DateTime, default=lambda: datetime.now(ZoneInfo("America/New_York"))
    )

    workspace = relationship("Workspace", back_populates="files")
