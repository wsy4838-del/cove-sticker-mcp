from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from sticker_mcp.assets import UnsafeArchiveError
from sticker_mcp.library import StickerLibrary


def png_bytes(color: tuple[int, int, int] = (30, 80, 160)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format="PNG")
    return output.getvalue()


def gif_bytes(frame_count: int) -> bytes:
    frames = [Image.new("RGB", (8, 8), (index, 20, 30)) for index in range(frame_count)]
    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=1)
    return output.getvalue()


def test_import_deduplicates_and_searches_metadata(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    first = library.import_bytes(png_bytes(), "duck.png")
    duplicate = library.import_bytes(png_bytes(), "renamed.png")

    assert first.id == duplicate.id
    library.update_metadata(
        first.id,
        description="白鸭躺在水池里",
        semantic_description="表达放松和摆烂",
        emotions=["惬意"],
        scenes=["享受生活"],
        keywords=["白鸭", "水池"],
    )
    result = library.search("摆烂", include_deleted=False)
    assert result.total == 1
    assert result.items[0].id == first.id


def test_soft_deleted_sticker_is_kept_for_history_but_hidden_from_agent(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    sticker = library.import_bytes(png_bytes(), "x.png")
    library.soft_delete(sticker.id)

    assert library.get(sticker.id, include_deleted=True).deleted is True
    assert library.search("", include_deleted=False).total == 0
    with pytest.raises(KeyError):
        library.agent_get(sticker.id)


def test_zip_rejects_path_traversal_before_writing(tmp_path: Path) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.png", png_bytes())
    library = StickerLibrary(tmp_path)

    with pytest.raises(UnsafeArchiveError):
        library.import_zip(archive.getvalue(), "malicious.zip")
    assert list((tmp_path / "assets").glob("**/*")) == []


def test_gif_frame_budget_is_checked_during_import(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    with pytest.raises(ValueError, match="frames"):
        library.import_bytes(gif_bytes(11), "too-many.gif")


def test_backup_restore_rehashes_and_is_atomic(tmp_path: Path) -> None:
    source = StickerLibrary(tmp_path / "source")
    sticker = source.import_bytes(png_bytes(), "safe.png")
    source.update_metadata(sticker.id, description="安全图片", keywords=["安全"])
    backup = source.export_backup()

    manifest = json.loads(backup.manifest.read_text())
    manifest["items"][0]["id"] = "forged-id"
    manifest["items"][0]["path"] = "../../outside.png"
    forged_backup = tmp_path / "forged.zip"
    with zipfile.ZipFile(backup.path) as original, zipfile.ZipFile(forged_backup, "w") as forged:
        forged.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for name in original.namelist():
            if name != "manifest.json":
                forged.writestr(name, original.read(name))

    destination = StickerLibrary(tmp_path / "destination")
    with pytest.raises(ValueError, match="hash|path|id"):
        destination.restore_backup(forged_backup)
    assert destination.search("", include_deleted=True).total == 0


def test_auto_tags_preserve_only_fields_edited_by_user(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    sticker = library.import_bytes(png_bytes(), "x.png")
    library.update_metadata(sticker.id, description="我的描述", keywords=["手动"])

    updated = library.apply_auto_metadata(
        sticker.id,
        {
            "description": "模型描述",
            "semantic_description": "模型语义",
            "emotions": ["惬意"],
            "keywords": ["模型", "手动"],
        },
    )

    assert updated.description == "我的描述"
    assert updated.keywords == ["手动"]
    assert updated.semantic_description == "模型语义"
    assert updated.emotions == ["惬意"]
    library.close()
    reopened = StickerLibrary(tmp_path)
    persisted = reopened.get(sticker.id)
    assert persisted.keywords == ["手动"]
    assert persisted.manual_fields == ["description", "keywords"]


def test_pick_requires_a_relevant_match_and_uses_session_turn_cooldown(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    sticker = library.import_bytes(png_bytes(), "rest.png")
    library.update_metadata(sticker.id, description="表达放松", keywords=["休息"])
    library.update_settings(avoid_recent=0)

    assert library.pick("完全不存在", session_id="s", turn=1, limit=1) == []
    assert library.pick("放松", session_id="s", turn=1, limit=1)[0].id == sticker.id
    assert library.pick("放松", session_id="s", turn=2, limit=1) == []
    assert library.pick("放松", session_id="s", turn=4, limit=1)[0].id == sticker.id


def test_backup_exports_all_items_and_restores_manual_metadata(tmp_path: Path) -> None:
    source = StickerLibrary(tmp_path / "source")
    for index in range(101):
        source.import_bytes(png_bytes((index % 255, 80, 160)), f"{index}.png")
    first = source.search("", page_size=1).items[0]
    source.update_metadata(first.id, description="manual", keywords=["tag"])
    source.feedback(first.id, "like")
    backup = source.export_backup(tmp_path / "all.zip")
    with zipfile.ZipFile(backup.path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert len(manifest["items"]) == 101

    destination = StickerLibrary(tmp_path / "destination")
    report = destination.restore_backup(backup.path)
    assert len(report.imported) == 101
    restored = destination.get(first.id)
    assert restored.description == "manual"
    assert restored.manual_fields == ["description", "keywords"]
    assert restored.last_feedback == "like"


def test_backup_with_1000_items_round_trips_with_portable_entry_budget(tmp_path: Path) -> None:
    source = StickerLibrary(tmp_path / "source")
    for index in range(1000):
        source.import_bytes(png_bytes((index % 256, (index // 256) % 256, 160)), f"{index}.png")
    backup = source.export_backup(tmp_path / "thousand.zip")
    destination = StickerLibrary(tmp_path / "destination")
    report = destination.restore_backup(backup.path)
    assert len(report.imported) == 1000
    assert destination.search("", include_deleted=True, page_size=1).total == 1000


def test_restore_rejects_oversized_member_set_before_import(tmp_path: Path) -> None:
    backup = tmp_path / "many.zip"
    with zipfile.ZipFile(backup, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"schema": 1, "items": []}))
        for index in range(2001):
            archive.writestr(f"assets/{index}.png", b"x")
    destination = StickerLibrary(tmp_path / "destination")
    with pytest.raises(ValueError, match="backup|entry|ZIP"):
        destination.restore_backup(backup)
    assert destination.search("", include_deleted=True).total == 0
