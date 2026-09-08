from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

from sticker_mcp.library import StickerLibrary
from sticker_mcp.vision import (
    HttpVisionProvider,
    TagQueue,
    VisionConfig,
    VisionError,
    normalize_tags,
    prepare_vision_image,
)


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (120, 90), (20, 80, 150, 255)).save(output, format="PNG")
    return output.getvalue()


class FakeVision:
    async def describe(self, image: bytes, mime_type: str, filename: str) -> dict[str, object]:
        assert image
        assert mime_type == "image/png"
        return {"description": "一张蓝色图片", "keywords": ["蓝色"], "emotions": ["平静"]}


class CountingVision(FakeVision):
    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    async def describe(self, image: bytes, mime_type: str, filename: str) -> dict[str, object]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider failed")
        return await super().describe(image, mime_type, filename)


class SlowFailVision(CountingVision):
    def __init__(self):
        super().__init__(fail=True)
        self.started = threading.Event()
        self.release = threading.Event()

    async def describe(self, image: bytes, mime_type: str, filename: str) -> dict[str, object]:
        self.calls += 1
        self.started.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        raise RuntimeError("provider failed")


def test_normalize_tags_accepts_fenced_json_and_caps_untrusted_fields() -> None:
    result = normalize_tags('```json\n{"description":"x","keywords":["a"]}\n```')
    assert result["description"] == "x"
    assert result["keywords"] == ["a"]


def test_normalize_tags_requires_description_and_rejects_wrong_types() -> None:
    with pytest.raises(VisionError):
        normalize_tags("{}")
    with pytest.raises(VisionError):
        normalize_tags(json.dumps({"description": "ok", "keywords": "execute this"}))


def test_prepare_vision_image_is_bounded() -> None:
    data, mime = prepare_vision_image(png_bytes(), "image/png")
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(data)) as image:
        assert max(image.size) <= 768


def test_tag_queue_is_single_worker_and_recoverable(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    sticker = library.import_bytes(png_bytes(), "blue.png")
    queue = TagQueue(library, FakeVision())
    job_id = queue.submit(sticker.id)
    deadline = time.time() + 3
    while time.time() < deadline:
        job = library.get_tag_job(job_id)
        if job["status"] == "done":
            break
        time.sleep(0.02)
    assert library.get(sticker.id).keywords == ["蓝色"]
    assert library.get_tag_job(job_id)["status"] == "done"
    queue.shutdown()


def test_manual_tag_works_when_auto_tag_is_off(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    sticker = library.import_bytes(png_bytes(), "blue.png")
    library.update_settings(auto_tag=False)
    queue = TagQueue(library, FakeVision(), enabled=True)
    job_id = queue.submit(sticker.id, source="manual")
    deadline = time.time() + 3
    while time.time() < deadline and library.get_tag_job(job_id)["status"] not in {"done", "failed"}:
        time.sleep(0.02)
    assert library.get_tag_job(job_id)["status"] == "done"
    queue.shutdown()


def test_failed_job_retry_is_deduplicated(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    sticker = library.import_bytes(png_bytes(), "blue.png")
    provider = SlowFailVision()
    queue = TagQueue(library, provider, enabled=True)
    job_id = queue.submit(sticker.id)
    assert provider.started.wait(3)
    provider.release.set()
    deadline = time.time() + 3
    while time.time() < deadline and library.get_tag_job(job_id)["status"] != "failed":
        time.sleep(0.02)
    provider.started.clear()
    provider.release.clear()
    queue.retry(job_id)
    assert provider.started.wait(3)
    with pytest.raises(ValueError):
        queue.retry(job_id)
    provider.release.set()
    queue.shutdown()
    assert provider.calls == 2


def test_explicit_retry_is_manual_when_auto_tag_is_disabled(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    sticker = library.import_bytes(png_bytes(), "blue.png")
    provider = CountingVision(fail=True)
    queue = TagQueue(library, provider, enabled=True)
    job_id = queue.submit(sticker.id, source="auto")
    deadline = time.time() + 3
    while time.time() < deadline and library.get_tag_job(job_id)["status"] != "failed":
        time.sleep(0.02)
    library.update_settings(auto_tag=False)
    queue.retry(job_id)
    assert library.get_tag_job(job_id)["source"] == "manual"
    queue.shutdown()


def test_unconfigured_provider_has_no_network_side_effect() -> None:
    config = VisionConfig()
    assert config.enabled is False


@pytest.mark.asyncio
async def test_minimax_provider_uses_anthropic_image_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_STICKER_KEY", "secret")
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, json={"content": [{"text": '{"description":"蓝色图片"}'}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpVisionProvider(VisionConfig("minimax", "MiniMax-M3", "https://api.example", "TEST_STICKER_KEY"), client)
    result = await provider.describe(png_bytes(), "image/png", "x.png")
    await client.aclose()
    assert result["description"] == "蓝色图片"
    assert seen["json"]["messages"][0]["content"][1]["source"]["type"] == "base64"


@pytest.mark.asyncio
async def test_openai_compatible_provider_uses_image_url_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_STICKER_KEY", "secret")
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"description":"平静"}'}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpVisionProvider(VisionConfig("openai-compatible", "vision", "https://api.example/v1", "TEST_STICKER_KEY"), client)
    result = await provider.describe(png_bytes(), "image/png", "x.png")
    await client.aclose()
    assert result["description"] == "平静"
    assert seen["json"]["messages"][0]["content"][1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_openai_compatible_provider_uses_exact_custom_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_STICKER_KEY", "secret")
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        assert request.headers["authorization"] == "Bearer secret"
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"description":"自定义接口"}'}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpVisionProvider(
        VisionConfig(
            provider="openai-compatible",
            provider_name="Local OpenAI gateway",
            model="org/vision-model",
            base_url="https://ignored.example/v1",
            endpoint_url="http://127.0.0.1:9000/custom/completions",
            api_key="secret",
        ),
        client,
    )
    result = await provider.describe(png_bytes(), "image/png", "x.png")
    await client.aclose()
    assert result["description"] == "自定义接口"
    assert seen["url"] == "http://127.0.0.1:9000/custom/completions"
    assert seen["json"]["model"] == "org/vision-model"


@pytest.mark.asyncio
async def test_anthropic_compatible_provider_uses_messages_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_STICKER_KEY", "secret")
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        assert request.headers["x-api-key"] == "secret"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"content": [{"type": "text", "text": '{"description":"Anthropic视觉"}'}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpVisionProvider(
        VisionConfig(
            provider="anthropic-compatible",
            provider_name="自托管 Anthropic 网关",
            model="claude-vision-compatible",
            base_url="https://anthropic.example/v1",
            api_key="secret",
        ),
        client,
    )
    result = await provider.describe(png_bytes(), "image/png", "x.png")
    await client.aclose()
    assert result["description"] == "Anthropic视觉"
    assert seen["url"] == "https://anthropic.example/v1/messages"
    assert seen["json"]["messages"][0]["content"][1]["source"]["type"] == "base64"


@pytest.mark.asyncio
async def test_anthropic_compatible_provider_keeps_exact_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_STICKER_KEY", "secret")
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"content": [{"type": "text", "text": '{"description":"ok"}'}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpVisionProvider(
        VisionConfig(
            provider="anthropic-compatible",
            model="vision",
            base_url="https://ignored.example",
            endpoint_url="https://proxy.example/custom/messages",
            api_key="secret",
        ),
        client,
    )
    await provider.describe(png_bytes(), "image/png", "x.png")
    await client.aclose()
    assert seen == ["https://proxy.example/custom/messages"]


@pytest.mark.asyncio
async def test_unknown_vision_protocol_is_rejected_without_openai_fallback() -> None:
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpVisionProvider(
        VisionConfig(
            provider="unknown-compatible",
            model="vision",
            base_url="https://api.example",
            api_key="secret",
        ),
        client,
    )
    with pytest.raises(VisionError, match="unsupported vision provider"):
        await provider.describe(png_bytes(), "image/png", "x.png")
    await client.aclose()
    assert called is False


@pytest.mark.asyncio
async def test_invalid_provider_json_is_not_reported_as_url_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json", headers={"content-type": "text/plain"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpVisionProvider(
        VisionConfig(
            provider="openai-compatible",
            model="vision",
            base_url="https://api.example/v1",
            api_key="secret",
        ),
        client,
    )
    with pytest.raises(VisionError, match="invalid JSON"):
        await provider.describe(png_bytes(), "image/png", "x.png")
    await client.aclose()


def test_custom_vision_settings_roundtrip_and_invalid_update_is_atomic(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    library.update_vision_settings(
        provider="anthropic-compatible",
        provider_name="我的视觉服务",
        model="org/model-with-vision",
        base_url="http://127.0.0.1:9000/v1",
        endpoint_url="http://127.0.0.1:9000/custom/messages",
        api_key="secret",
    )
    assert library.vision_settings() == {
        "provider": "anthropic-compatible",
        "provider_name": "我的视觉服务",
        "model": "org/model-with-vision",
        "base_url": "http://127.0.0.1:9000/v1",
        "endpoint_url": "http://127.0.0.1:9000/custom/messages",
    }
    assert library.vision_api_key() == "secret"

    with pytest.raises(ValueError, match="unsupported vision provider"):
        library.update_vision_settings(provider="gemini", provider_name="被拒绝")
    assert library.vision_settings()["provider"] == "anthropic-compatible"
    assert library.vision_settings()["provider_name"] == "我的视觉服务"
    assert library.vision_api_key() == "secret"

    with pytest.raises(ValueError, match="http"):
        library.update_vision_settings(endpoint_url="file:///tmp/model")
    assert library.vision_settings()["endpoint_url"] == "http://127.0.0.1:9000/custom/messages"
    assert library.vision_api_key() == "secret"
