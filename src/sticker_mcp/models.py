from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


@dataclass(frozen=True)
class Sticker:
    id: str
    filename: str
    mime_type: str
    extension: str
    byte_size: int
    width: int
    height: int
    description: str = ""
    ocr_text: str = ""
    semantic_description: str = ""
    emotions: list[str] = field(default_factory=list)
    scenes: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    deleted: bool = False
    manually_edited: bool = False
    use_count: int = 0
    last_feedback: str | None = None
    manual_fields: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: Any) -> Sticker:
        return cls(
            id=row["id"], filename=row["filename"], mime_type=row["mime_type"],
            extension=row["extension"], byte_size=int(row["byte_size"]),
            width=int(row["width"]), height=int(row["height"]),
            description=row["description"] or "", ocr_text=row["ocr_text"] or "",
            semantic_description=row["semantic_description"] or "",
            emotions=_list(row["emotions_json"]), scenes=_list(row["scenes_json"]),
            keywords=_list(row["keywords_json"]), created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "", deleted=bool(row["deleted"]),
            manually_edited=bool(row["manually_edited"]), use_count=int(row["use_count"]),
            last_feedback=row["last_feedback"],
            manual_fields=_list(row["manual_fields_json"]),
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id, "filename": self.filename, "mime_type": self.mime_type,
            "extension": self.extension, "byte_size": self.byte_size,
            "width": self.width, "height": self.height, "description": self.description,
            "ocr_text": self.ocr_text, "semantic_description": self.semantic_description,
            "emotions": list(self.emotions), "scenes": list(self.scenes),
            "keywords": list(self.keywords), "created_at": self.created_at,
            "updated_at": self.updated_at, "deleted": self.deleted,
            "manually_edited": self.manually_edited, "use_count": self.use_count,
            "last_feedback": self.last_feedback,
            "manual_fields": list(self.manual_fields),
        }


@dataclass(frozen=True)
class SearchResult:
    items: list[Sticker]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class ImportReport:
    imported: list[Sticker]
    duplicates: list[str]
    rejected: list[dict[str, str]]


@dataclass(frozen=True)
class BackupBundle:
    path: Path
    manifest: Path


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    assistant_enabled: bool = True
    auto_tag: bool = True
    show_feedback: bool = False
    casual_frequency: str = "normal"
    work_frequency: str = "rare"
    avoid_recent: int = 3

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "assistant_enabled": self.assistant_enabled,
            "auto_tag": self.auto_tag, "show_feedback": self.show_feedback,
            "casual_frequency": self.casual_frequency, "work_frequency": self.work_frequency,
            "avoid_recent": self.avoid_recent,
        }
