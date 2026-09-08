from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from PIL import Image

import sticker_mcp.web as web_module
from sticker_mcp.library import StickerLibrary
from sticker_mcp.vision import TagQueue
from sticker_mcp.web import create_app


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 16), (20, 80, 150)).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_web_api_requires_same_origin_csrf_for_writes(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    app = create_app(library)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        page = await client.get("/")
        assert page.status_code == 200
        assert "表情包" in page.text
        forbidden = await client.post("/api/stickers", files={"files": ("x.png", png_bytes(), "image/png")})
        assert forbidden.status_code == 403
        csrf = await client.get("/api/csrf")
        token = csrf.json()["token"]
        allowed = await client.post(
            "/api/stickers",
            headers={"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": token},
            files={"files": ("x.png", png_bytes(), "image/png")},
        )
        assert allowed.status_code == 201


@pytest.mark.asyncio
async def test_web_rejects_untrusted_host(tmp_path: Path) -> None:
    app = create_app(StickerLibrary(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://evil.example") as client:
        response = await client.get("/")
    assert response.status_code in {400, 421}


@pytest.mark.asyncio
async def test_web_imports_zip_and_queues_all_imported_images(tmp_path: Path) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("one.png", png_bytes())
    library = StickerLibrary(tmp_path)

    class Queue:
        enabled = True

        def __init__(self) -> None:
            self.submitted: list[str] = []

        def submit(self, sticker_id: str, *, source: str) -> int:
            self.submitted.append(sticker_id)
            assert source == "auto"
            return len(self.submitted)

    queue = Queue()
    app = create_app(library, queue=queue)  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        csrf = await client.get("/api/csrf")
        token = csrf.json()["token"]
        response = await client.post(
            "/api/stickers",
            headers={"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": token},
            files={"files": ("library.zip", archive.getvalue(), "application/zip")},
        )
    assert response.status_code == 201
    assert response.json()["job_ids"] == [1]
    assert len(queue.submitted) == 1


@pytest.mark.asyncio
async def test_web_vision_settings_are_masked_and_settings_types_are_strict(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    app = create_app(library)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        csrf = await client.get("/api/csrf")
        token = csrf.json()["token"]
        headers = {"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": token}
        invalid = await client.patch("/api/settings", headers=headers, json={"enabled": "false"})
        assert invalid.status_code == 400
        saved = await client.patch(
            "/api/vision", headers=headers,
            json={
                "provider": "openai-compatible",
                "provider_name": "团队视觉网关",
                "model": "org/vision-model",
                "base_url": "https://api.example/v1",
                "endpoint_url": "https://api.example/custom/chat",
                "api_key": "secret",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["api_key"] == "••••"
        assert "secret" not in json.dumps(saved.json(), ensure_ascii=False)
        fetched = await client.get("/api/vision")
        assert fetched.json()["configured"] is True
        assert fetched.json()["api_key"] == "••••"
        assert fetched.json()["provider_name"] == "团队视觉网关"
        assert fetched.json()["endpoint_url"] == "https://api.example/custom/chat"


@pytest.mark.asyncio
async def test_vision_configuration_reloads_queue_for_auto_and_manual_tagging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured_configs = []

    class FakeVision:
        async def describe(self, image: bytes, mime_type: str, filename: str) -> dict[str, object]:
            return {"description": "自动识别完成", "keywords": ["测试"]}

    def fake_provider(config):
        captured_configs.append(config)
        return FakeVision()

    monkeypatch.setattr(web_module, "HttpVisionProvider", fake_provider)
    library = StickerLibrary(tmp_path)
    queue = TagQueue(library, FakeVision(), enabled=False)
    app = create_app(library, queue=queue)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        csrf = await client.get("/api/csrf")
        token = csrf.json()["token"]
        headers = {"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": token}
        configured = await client.patch(
            "/api/vision", headers=headers,
            json={
                "provider": "anthropic-compatible",
                "provider_name": "自定义 Anthropic 网关",
                "model": "org/vision-model",
                "base_url": "https://api.example/v1",
                "endpoint_url": "https://api.example/custom/messages",
                "api_key": "secret",
            },
        )
        assert configured.status_code == 200
        assert captured_configs[-1].provider == "anthropic-compatible"
        assert captured_configs[-1].provider_name == "自定义 Anthropic 网关"
        assert captured_configs[-1].endpoint_url == "https://api.example/custom/messages"
        imported = await client.post(
            "/api/stickers", headers=headers,
            files={"files": ("x.png", png_bytes(), "image/png")},
        )
        assert imported.status_code == 201
        sticker_id = imported.json()["imported"][0]["id"]
        assert imported.json()["job_ids"]
        deadline = 3.0
        while deadline > 0 and library.get(sticker_id).description != "自动识别完成":
            await asyncio.sleep(0.02)
            deadline -= 0.02
        assert library.get(sticker_id).description == "自动识别完成"
        tested = await client.post("/api/vision/test", headers=headers, json={"confirm_cost": True})
        assert tested.status_code == 200
        assert captured_configs[-1].provider == "anthropic-compatible"
        assert captured_configs[-1].endpoint_url == "https://api.example/custom/messages"
        manual = await client.post("/api/tag", headers=headers, json={"ids": [sticker_id]})
        assert manual.status_code == 202
    queue.shutdown()
