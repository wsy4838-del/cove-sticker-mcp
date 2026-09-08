from __future__ import annotations

from pathlib import Path

import pytest

from sticker_mcp.cli import build_services, vision_config


def _close_services(services) -> None:
    library, queue, _config, lock = services
    queue.shutdown()
    library.close()
    lock.release()


def test_cli_allows_only_one_process_per_data_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STICKER_MCP_DATA_DIR", str(tmp_path))
    first = build_services()
    try:
        with pytest.raises(SystemExit, match="already running"):
            build_services()
    finally:
        _close_services(first)

    second = build_services()
    _close_services(second)


def test_cli_vision_config_can_use_endpoint_without_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    config = vision_config(
        {
            "provider": "openai-compatible",
            "provider_name": "本地网关",
            "model": "vision-model",
            "base_url": "",
            "endpoint_url": "http://127.0.0.1:9000/custom/chat",
        },
        api_key="secret",
    )
    assert config.enabled is True
    assert config.provider_name == "本地网关"
    assert config.endpoint_url == "http://127.0.0.1:9000/custom/chat"
