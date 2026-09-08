from __future__ import annotations

import io
import json
import os
import re
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .assets import (
    AssetError,
    ImageInfo,
    UnsafeArchiveError,
    asset_path,
    safe_zip_members,
    validate_image,
)
from .models import BackupBundle, ImportReport, SearchResult, Settings, Sticker
from .storage import Database


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json_list(values: list[str] | tuple[str, ...] | None) -> str:
    return json.dumps([str(v).strip() for v in (values or []) if str(v).strip()], ensure_ascii=False)


_TAG_FIELDS = ("description", "ocr_text", "semantic_description", "emotions", "scenes", "keywords")


class StickerLibrary:
    """Persistent sticker store with no network or provider side effects."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "assets").mkdir(exist_ok=True)
        self.db = Database(self.root / "library.sqlite3")
        if self.db.get_setting("preferences") is None:
            self.db.set_setting("preferences", Settings().to_dict())

    def close(self) -> None:
        self.db.close()

    def _row(self, sticker_id: str) -> Any:
        return self.db.connection.execute("SELECT * FROM stickers WHERE id=?", (sticker_id,)).fetchone()

    def get(self, sticker_id: str, *, include_deleted: bool = True) -> Sticker:
        row = self._row(sticker_id)
        if row is None or (row["deleted"] and not include_deleted):
            raise KeyError(sticker_id)
        return Sticker.from_row(row)

    def asset_bytes(self, sticker_id: str, *, include_deleted: bool = True) -> bytes:
        sticker = self.get(sticker_id, include_deleted=include_deleted)
        path = self.root / "assets" / f"{sticker.id}{sticker.extension}"
        try:
            return path.read_bytes()
        except OSError as exc:
            raise KeyError(sticker_id) from exc

    def import_bytes(self, data: bytes, filename: str) -> Sticker:
        info = validate_image(data, filename)
        existing = self._row(info.sha256)
        if existing is not None:
            if existing["deleted"]:
                self.db.connection.execute(
                    "UPDATE stickers SET deleted=0, updated_at=? WHERE id=?", (_now(), info.sha256)
                )
                self.db.connection.commit()
            return Sticker.from_row(self._row(info.sha256))
        return self._insert_new(data, filename, info)

    def _insert_new(self, data: bytes, filename: str, info: ImageInfo) -> Sticker:
        destination = asset_path(self.root, info)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, destination)
        now = _now()
        try:
            self.db.connection.execute(
                """INSERT INTO stickers
                   (id,filename,mime_type,extension,byte_size,width,height,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (info.sha256, Path(filename).name[:200], info.mime_type, info.extension,
                 info.byte_size, info.width, info.height, now, now),
            )
            self.db.connection.commit()
        except Exception:
            destination.unlink(missing_ok=True)
            self.db.connection.rollback()
            raise
        return self.get(info.sha256)

    def import_many(self, files: list[tuple[str, bytes]]) -> ImportReport:
        imported: list[Sticker] = []
        duplicates: list[str] = []
        rejected: list[dict[str, str]] = []
        for filename, data in files:
            try:
                info = validate_image(data, filename)
                if self._row(info.sha256) is not None:
                    duplicates.append(info.sha256)
                imported.append(self.import_bytes(data, filename))
            except AssetError as exc:
                rejected.append({"filename": filename, "error": str(exc)})
        return ImportReport(imported, duplicates, rejected)

    def import_zip(self, raw: bytes, filename: str = "stickers.zip") -> ImportReport:
        members = safe_zip_members(raw)
        archive = zipfile.ZipFile(io.BytesIO(raw))
        try:
            files: list[tuple[str, bytes]] = []
            for info, safe_name in members:
                files.append((safe_name, archive.read(info)))
        finally:
            archive.close()
        return self.import_many(files)

    def update_metadata(
        self,
        sticker_id: str,
        *,
        description: str | None = None,
        ocr_text: str | None = None,
        semantic_description: str | None = None,
        emotions: list[str] | None = None,
        scenes: list[str] | None = None,
        keywords: list[str] | None = None,
        mark_manual: bool = True,
    ) -> Sticker:
        current = self.get(sticker_id)
        values = {
            "description": current.description if description is None else str(description)[:2000],
            "ocr_text": current.ocr_text if ocr_text is None else str(ocr_text)[:1000],
            "semantic_description": current.semantic_description if semantic_description is None else str(semantic_description)[:4000],
            "emotions_json": _json_list(current.emotions if emotions is None else emotions),
            "scenes_json": _json_list(current.scenes if scenes is None else scenes),
            "keywords_json": _json_list(current.keywords if keywords is None else keywords),
        }
        manual_fields = set(current.manual_fields)
        if mark_manual:
            for field_name, value in (("description", description), ("ocr_text", ocr_text),
                                      ("semantic_description", semantic_description), ("emotions", emotions),
                                      ("scenes", scenes), ("keywords", keywords)):
                if value is not None:
                    manual_fields.add(field_name)
        self.db.connection.execute(
            """UPDATE stickers SET description=?,ocr_text=?,semantic_description=?,emotions_json=?,
               scenes_json=?,keywords_json=?,manually_edited=?,manual_fields_json=?,updated_at=? WHERE id=?""",
            (*values.values(), int(bool(manual_fields)), _json_list(sorted(manual_fields)), _now(), sticker_id),
        )
        self.db.connection.commit()
        return self.get(sticker_id)

    def apply_auto_metadata(self, sticker_id: str, metadata: dict[str, Any]) -> Sticker:
        current = self.get(sticker_id)
        manual = set(current.manual_fields)
        updates: dict[str, Any] = {}
        for field_name in _TAG_FIELDS:
            if field_name in metadata and field_name not in manual:
                updates[field_name] = metadata[field_name]
        if not updates:
            return current
        values = {
            "description": updates.get("description", current.description),
            "ocr_text": updates.get("ocr_text", current.ocr_text),
            "semantic_description": updates.get("semantic_description", current.semantic_description),
            "emotions_json": _json_list(updates.get("emotions", current.emotions)),
            "scenes_json": _json_list(updates.get("scenes", current.scenes)),
            "keywords_json": _json_list(updates.get("keywords", current.keywords)),
        }
        self.db.connection.execute(
            """UPDATE stickers SET description=?,ocr_text=?,semantic_description=?,emotions_json=?,
               scenes_json=?,keywords_json=?,updated_at=? WHERE id=?""",
            (*values.values(), _now(), sticker_id),
        )
        self.db.connection.commit()
        return self.get(sticker_id)

    def soft_delete(self, sticker_id: str) -> None:
        self.get(sticker_id)
        self.db.connection.execute("UPDATE stickers SET deleted=1,updated_at=? WHERE id=?", (_now(), sticker_id))
        self.db.connection.commit()

    def restore(self, sticker_id: str) -> Sticker:
        self.get(sticker_id)
        self.db.connection.execute("UPDATE stickers SET deleted=0,updated_at=? WHERE id=?", (_now(), sticker_id))
        self.db.connection.commit()
        return self.get(sticker_id)

    def search(self, query: str, *, page: int = 1, page_size: int = 50,
               include_deleted: bool = False, agent_only: bool = False) -> SearchResult:
        page = max(1, int(page))
        page_size = min(100, max(1, int(page_size)))
        query = (query or "").strip().lower()
        clauses = []
        params: list[Any] = []
        if not include_deleted:
            clauses.append("deleted=0")
        if agent_only:
            prefs = self.settings()
            if not prefs.enabled or not prefs.assistant_enabled:
                return SearchResult([], 0, page, page_size)
            clauses.append("last_feedback IS NOT 'dislike'")
        if query:
            token = f"%{query}%"
            clauses.append("lower(filename || ' ' || description || ' ' || ocr_text || ' ' || semantic_description || ' ' || emotions_json || ' ' || scenes_json || ' ' || keywords_json) LIKE ?")
            params.append(token)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = int(self.db.connection.execute(f"SELECT COUNT(*) FROM stickers{where}", tuple(params)).fetchone()[0])
        rows = self.db.connection.execute(
            f"SELECT * FROM stickers{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        return SearchResult([Sticker.from_row(row) for row in rows], total, page, page_size)

    def settings(self) -> Settings:
        value = self.db.get_setting("preferences") or {}
        defaults = Settings().to_dict()
        defaults.update(value)
        return Settings(**{k: defaults[k] for k in Settings().__dict__})

    def update_settings(self, **changes: Any) -> Settings:
        current = self.settings().to_dict()
        allowed = set(current)
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError("unknown settings field")
        for key, value in changes.items():
            if key in {"enabled", "assistant_enabled", "auto_tag", "show_feedback"} and not isinstance(value, bool):
                raise ValueError(f"{key} must be boolean")
            if key in {"casual_frequency", "work_frequency"} and value not in {"off", "rare", "normal", "often"}:
                raise ValueError(f"{key} has invalid value")
            if key == "avoid_recent" and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 20):
                raise ValueError("avoid_recent must be between 0 and 20")
        current.update(changes)
        current["casual_frequency"] = current["casual_frequency"] if current["casual_frequency"] in {"off", "rare", "normal", "often"} else "normal"
        current["work_frequency"] = current["work_frequency"] if current["work_frequency"] in {"off", "rare", "normal", "often"} else "rare"
        current["avoid_recent"] = min(20, max(0, int(current["avoid_recent"])))
        self.db.set_setting("preferences", current)
        return self.settings()

    def feedback(self, sticker_id: str, value: str) -> Sticker:
        if value not in {"like", "dislike", "clear"}:
            raise ValueError("feedback must be like, dislike, or clear")
        self.get(sticker_id)
        self.db.connection.execute("UPDATE stickers SET last_feedback=?,updated_at=? WHERE id=?", (None if value == "clear" else value, _now(), sticker_id))
        self.db.connection.commit()
        return self.get(sticker_id)

    def create_tag_job(self, sticker_id: str, *, source: str = "manual") -> int:
        if source not in {"manual", "auto"}:
            raise ValueError("invalid tag job source")
        self.get(sticker_id)
        now = _now()
        cursor = self.db.connection.execute(
            "INSERT INTO tag_jobs(sticker_id,status,created_at,updated_at,source) VALUES (?,?,?,?,?)",
            (sticker_id, "queued", now, now, source),
        )
        self.db.connection.commit()
        return int(cursor.lastrowid)

    def get_tag_job(self, job_id: int) -> dict[str, Any]:
        row = self.db.connection.execute("SELECT * FROM tag_jobs WHERE id=?", (int(job_id),)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(zip(row.keys(), tuple(row), strict=True))

    def mark_tag_job(self, job_id: int, status: str, error: str | None, *, source: str | None = None) -> None:
        if status not in {"queued", "running", "done", "failed", "retryable"}:
            raise ValueError("invalid tag job status")
        if source is not None and source not in {"manual", "auto"}:
            raise ValueError("invalid tag job source")
        if source is None:
            self.db.connection.execute("UPDATE tag_jobs SET status=?,error=?,updated_at=? WHERE id=?", (status, error, _now(), int(job_id)))
        else:
            self.db.connection.execute("UPDATE tag_jobs SET status=?,error=?,source=?,updated_at=? WHERE id=?", (status, error, source, _now(), int(job_id)))
        self.db.connection.commit()

    def recover_tag_jobs(self) -> int:
        cursor = self.db.connection.execute(
            "UPDATE tag_jobs SET status='retryable',error='interrupted; retry explicitly',updated_at=? WHERE status IN ('queued','running')",
            (_now(),),
        )
        self.db.connection.commit()
        return int(cursor.rowcount)

    def list_tag_jobs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.connection.execute("SELECT * FROM tag_jobs ORDER BY id DESC LIMIT ?", (min(1000, max(1, int(limit))),)).fetchall()
        return [dict(zip(row.keys(), tuple(row), strict=True)) for row in rows]

    def vision_settings(self) -> dict[str, str]:
        value = self.db.get_setting("vision") or {}
        if not isinstance(value, dict):
            return {}
        return {key: str(value[key]) for key in ("provider", "model", "base_url", "api_key_env") if isinstance(value.get(key), str)}

    def vision_api_key(self) -> str:
        secret_path = self.root / "vision-secrets.json"
        try:
            value = json.loads(secret_path.read_text())
        except (OSError, json.JSONDecodeError):
            return ""
        return value.get("api_key", "") if isinstance(value, dict) and isinstance(value.get("api_key"), str) else ""

    def update_vision_settings(self, **changes: Any) -> dict[str, str]:
        allowed = {"provider", "model", "base_url", "api_key_env", "api_key"}
        if set(changes) - allowed:
            raise ValueError("unknown vision setting")
        current = self.vision_settings()
        secret_update: str | None = None
        for key, value in changes.items():
            if not isinstance(value, str) or len(value) > 500:
                raise ValueError("vision settings must be short strings")
            if key == "provider" and value not in {"", "minimax", "openai-compatible"}:
                raise ValueError("unsupported vision provider")
            if key == "api_key_env" and value and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,80}", value):
                raise ValueError("invalid API key environment name")
            if key == "api_key":
                secret_update = value
        if secret_update is not None:
            secret_path = self.root / "vision-secrets.json"
            temporary = secret_path.with_name(f".{secret_path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps({"api_key": secret_update}, ensure_ascii=False))
                temporary.chmod(0o600)
                os.replace(temporary, secret_path)
            finally:
                temporary.unlink(missing_ok=True)
        current.update(changes)
        current.pop("api_key", None)
        self.db.set_setting("vision", current)
        return current

    def untagged_ids(self, *, limit: int = 100) -> list[str]:
        result: list[str] = []
        page = 1
        while len(result) < limit:
            items = self.search("", page=page, page_size=100, include_deleted=False).items
            if not items:
                break
            result.extend(item.id for item in items if not item.description and not item.semantic_description and not item.ocr_text)
            if len(items) < 100:
                break
            page += 1
        return result[:limit]

    def agent_get(self, sticker_id: str) -> Sticker:
        prefs = self.settings()
        sticker = self.get(sticker_id, include_deleted=False)
        if not prefs.enabled or not prefs.assistant_enabled or sticker.last_feedback == "dislike":
            raise KeyError(sticker_id)
        return sticker

    def _recent_ids(self, session_id: str | None, limit: int, *, current_turn: int | None = None, min_gap: int | None = None) -> set[str]:
        if not session_id or limit <= 0:
            return set()
        if current_turn is not None and min_gap is not None:
            rows = self.db.connection.execute(
                "SELECT sticker_id FROM usage_events WHERE session_id=? AND (turn IS NULL OR ? - turn < ?) ORDER BY id DESC LIMIT ?",
                (session_id, current_turn, min_gap, limit),
            ).fetchall()
        else:
            rows = self.db.connection.execute(
                "SELECT sticker_id FROM usage_events WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return {row[0] for row in rows}

    def _last_turn(self, session_id: str | None) -> int | None:
        if not session_id:
            return None
        row = self.db.connection.execute(
            "SELECT MAX(turn) FROM usage_events WHERE session_id=? AND turn IS NOT NULL", (session_id,)
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    @staticmethod
    def _query_tokens(query: str) -> list[str]:
        query = query.strip().lower()
        tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}", query)
        result: set[str] = set(tokens)
        for token in tokens:
            if re.fullmatch(r"[\u4e00-\u9fff]+", token):
                result.update(token[index:index + 2] for index in range(len(token) - 1))
                result.update(token[index:index + 3] for index in range(len(token) - 2))
        return sorted(result, key=len, reverse=True)

    def pick(self, intent: str, *, context: str = "casual", session_id: str | None = None,
             turn: int | None = None, turns_since: int | None = None,
             recent_ids: list[str] | None = None, limit: int = 1) -> list[Sticker]:
        prefs = self.settings()
        if not prefs.enabled or not prefs.assistant_enabled:
            return []
        frequency = prefs.work_frequency if context == "work" else prefs.casual_frequency
        if frequency == "off":
            return []
        min_gap = {"rare": 8, "normal": 3, "often": 1}[frequency]
        if turns_since is not None and turns_since < min_gap:
            return []
        if turn is not None:
            last_turn = self._last_turn(session_id)
            if last_turn is not None and turn - last_turn < min_gap:
                return []
        excluded = set(recent_ids or [])
        if turns_since is None:
            excluded |= self._recent_ids(session_id, prefs.avoid_recent, current_turn=turn, min_gap=min_gap)
        elif turns_since < min_gap:
            excluded |= self._recent_ids(session_id, prefs.avoid_recent)
        tokens = self._query_tokens(intent or "")
        rows = self.db.connection.execute(
            "SELECT * FROM stickers WHERE deleted=0 AND last_feedback IS NOT 'dislike' ORDER BY use_count ASC, updated_at DESC"
        ).fetchall()
        scored: list[tuple[int, Any]] = []
        for row in rows:
            if row["id"] in excluded:
                continue
            corpus = " ".join(row[key] or "" for key in ("description", "semantic_description", "emotions_json", "scenes_json", "keywords_json", "ocr_text")).lower()
            score = sum(2 if token and token in corpus else 0 for token in tokens)
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["use_count"], pair[1]["updated_at"]))
        result = [Sticker.from_row(row) for _, row in scored[: max(1, min(3, limit))]]
        for sticker in result:
            self.db.connection.execute("UPDATE stickers SET use_count=use_count+1,last_used_at=? WHERE id=?", (_now(), sticker.id))
            self.db.connection.execute("INSERT INTO usage_events(sticker_id,session_id,turn,used_at) VALUES (?,?,?,?)", (sticker.id, session_id, turn, _now()))
        self.db.connection.commit()
        return [self.get(sticker.id) for sticker in result]

    def export_backup(self, destination: Path | None = None) -> BackupBundle:
        destination = Path(destination or (self.root / "sticker-backup.zip"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, Any] = {"schema": 1, "items": [], "preferences": self.settings().to_dict()}
        data: list[tuple[str, bytes]] = []
        total_bytes = 0
        page = 1
        while True:
            page_items = self.search("", include_deleted=True, page=page, page_size=100).items
            for sticker in page_items:
                if len(data) >= 1999:
                    raise ValueError("backup exceeds the portable 100 MiB or 1999 item limit")
                raw = self.asset_bytes(sticker.id)
                total_bytes += len(raw)
                if total_bytes > 100 * 1024 * 1024:
                    raise ValueError("backup exceeds the portable 100 MiB or 1999 item limit")
                path = f"assets/{sticker.id}{sticker.extension}"
                manifest["items"].append({"id": sticker.id, "path": path, "sha256": sticker.id, "metadata": sticker.metadata()})
                data.append((path, raw))
            if len(page_items) < 100:
                break
            page += 1
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                for path, raw in data:
                    archive.writestr(path, raw)
            # The restore budget includes ZIP headers and the manifest. Validate
            # the exact bytes before making the backup visible to callers.
            safe_zip_members(temporary.read_bytes(), max_entries=2000)
            os.replace(temporary, destination)
        except (OSError, UnsafeArchiveError) as exc:
            temporary.unlink(missing_ok=True)
            raise ValueError(f"backup exceeds the portable ZIP budget: {exc}") from exc
        manifest_path = destination.with_suffix(".manifest.json")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        return BackupBundle(destination, manifest_path)

    def restore_backup(self, backup: Path) -> ImportReport:
        backup = Path(backup)
        try:
            archive_bytes = backup.read_bytes()
            safe_zip_members(archive_bytes, max_entries=2000)
        except (OSError, UnsafeArchiveError) as exc:
            raise ValueError(f"invalid backup ZIP: {exc}") from exc
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                raw_manifest = archive.read("manifest.json")
                manifest = json.loads(raw_manifest)
                members = {name: archive.read(name) for name in archive.namelist() if name != "manifest.json"}
        except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("invalid backup") from exc
        if not isinstance(manifest, dict) or manifest.get("schema") != 1 or not isinstance(manifest.get("items"), list):
            raise ValueError("unsupported backup schema")
        preferences = manifest.get("preferences", {})
        self._validate_preferences(preferences)
        validated: list[tuple[bytes, str, ImageInfo, dict[str, Any]]] = []
        seen_paths: set[str] = set()
        seen_ids: set[str] = set()
        metadata_fields = {
            "id", "filename", "mime_type", "extension", "byte_size", "width", "height",
            "description", "ocr_text", "semantic_description", "emotions", "scenes", "keywords",
            "created_at", "updated_at", "deleted", "manually_edited", "use_count",
            "last_feedback", "manual_fields",
        }
        for item in manifest["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("path"), str):
                raise ValueError("invalid backup item")
            path = item["path"]
            if path in seen_paths or path not in members or not path.startswith("assets/") or ".." in Path(path).parts:
                raise ValueError("invalid backup path")
            seen_paths.add(path)
            data = members[path]
            info = validate_image(data, Path(path).name)
            if item["id"] in seen_ids or item["id"] != info.sha256 or item.get("sha256") != info.sha256:
                raise ValueError("backup hash/id mismatch")
            seen_ids.add(item["id"])
            metadata = item.get("metadata")
            if not isinstance(metadata, dict) or set(metadata) != metadata_fields:
                raise ValueError("invalid backup metadata")
            if metadata.get("id") != info.sha256 or metadata.get("mime_type") != info.mime_type or metadata.get("extension") != info.extension:
                raise ValueError("invalid backup metadata")
            filename = metadata.get("filename")
            if not isinstance(filename, str) or not filename or len(filename) > 200 or "\x00" in filename or "\\" in filename or Path(filename).name != filename:
                raise ValueError("invalid backup metadata")
            for field_name, expected in (("byte_size", info.byte_size), ("width", info.width), ("height", info.height)):
                value = metadata.get(field_name)
                if isinstance(value, bool) or not isinstance(value, int) or value != expected:
                    raise ValueError("invalid backup metadata")
            for field_name in ("description", "ocr_text", "semantic_description"):
                if field_name in metadata and not isinstance(metadata[field_name], str):
                    raise ValueError("invalid backup metadata")
            for field_name in ("emotions", "scenes", "keywords", "manual_fields"):
                if field_name in metadata and (not isinstance(metadata[field_name], list) or not all(isinstance(value, str) for value in metadata[field_name])):
                    raise ValueError("invalid backup metadata")
            if any(field not in _TAG_FIELDS for field in metadata["manual_fields"]) or len(set(metadata["manual_fields"])) != len(metadata["manual_fields"]):
                raise ValueError("invalid backup manual fields")
            if not isinstance(metadata["created_at"], str) or not isinstance(metadata["updated_at"], str):
                raise ValueError("invalid backup metadata")
            if not isinstance(metadata["deleted"], bool) or not isinstance(metadata["manually_edited"], bool):
                raise ValueError("invalid backup metadata")
            if metadata["manually_edited"] != bool(metadata["manual_fields"]):
                raise ValueError("invalid backup manual fields")
            if isinstance(metadata["use_count"], bool) or not isinstance(metadata["use_count"], int) or metadata["use_count"] < 0:
                raise ValueError("invalid backup metadata")
            if "last_feedback" in metadata and metadata["last_feedback"] not in {None, "like", "dislike"}:
                raise ValueError("invalid backup feedback")
            validated.append((data, Path(path).name, info, metadata))
        if set(members) != seen_paths:
            raise ValueError("backup contains unreferenced files")
        return self._restore_validated(validated, preferences)

    @staticmethod
    def _validate_preferences(preferences: Any) -> None:
        if not isinstance(preferences, dict):
            raise ValueError("invalid backup preferences")
        bool_fields = {"enabled", "assistant_enabled", "auto_tag", "show_feedback"}
        frequency_fields = {"casual_frequency", "work_frequency"}
        if set(preferences) != bool_fields | frequency_fields | {"avoid_recent"}:
            raise ValueError("invalid backup preferences")
        for key, value in preferences.items():
            if key in bool_fields and not isinstance(value, bool):
                raise ValueError("invalid backup preferences")
            if key in frequency_fields and value not in {"off", "rare", "normal", "often"}:
                raise ValueError("invalid backup preferences")
            if key == "avoid_recent" and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 20):
                raise ValueError("invalid backup preferences")
            if key not in bool_fields | frequency_fields | {"avoid_recent"}:
                raise ValueError("invalid backup preferences field")

    def _restore_validated(self, items: list[tuple[bytes, str, ImageInfo, dict[str, Any]]], preferences: Any) -> ImportReport:
        imported: list[Sticker] = []
        duplicates: list[str] = []
        created_paths: list[Path] = []
        conn = self.db.connection
        try:
            conn.execute("BEGIN")
            for data, filename, info, metadata in items:
                path = asset_path(self.root, info)
                if self._row(info.sha256) is not None:
                    duplicates.append(info.sha256)
                else:
                    path.write_bytes(data)
                    created_paths.append(path)
                    now = _now()
                    conn.execute(
                        """INSERT INTO stickers(id,filename,mime_type,extension,byte_size,width,height,description,ocr_text,semantic_description,emotions_json,scenes_json,keywords_json,created_at,updated_at,deleted,manually_edited,manual_fields_json,last_feedback,use_count)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (info.sha256, metadata.get("filename", filename), info.mime_type, info.extension, info.byte_size, info.width, info.height,
                         str(metadata.get("description", ""))[:2000], str(metadata.get("ocr_text", ""))[:1000], str(metadata.get("semantic_description", ""))[:4000],
                         _json_list(metadata.get("emotions")), _json_list(metadata.get("scenes")), _json_list(metadata.get("keywords")), now, now,
                         int(bool(metadata.get("deleted"))), int(bool(metadata.get("manually_edited"))),
                         _json_list(metadata.get("manual_fields")), metadata.get("last_feedback"), int(metadata.get("use_count", 0) or 0)))
                    imported.append(self.get(info.sha256))
            if isinstance(preferences, dict):
                merged_preferences = {**self.settings().to_dict(), **preferences}
                conn.execute(
                    "INSERT INTO settings(key,value_json) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                    ("preferences", json.dumps(merged_preferences, ensure_ascii=False)),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            for path in created_paths:
                path.unlink(missing_ok=True)
            raise
        return ImportReport(imported, duplicates, [])
