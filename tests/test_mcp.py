from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client
from PIL import Image
from starlette.testclient import TestClient

from sticker_mcp.library import StickerLibrary
from sticker_mcp.mcp_server import create_http_app, create_server


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 16), (20, 80, 150)).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_server_exposes_two_tools_and_a_protected_asset_resource(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    sticker = library.import_bytes(png_bytes(), "blue.png")
    library.update_metadata(sticker.id, description="平静放松", keywords=["蓝色"])
    server = create_server(library)

    tools = await server.list_tools()
    assert {tool.name for tool in tools} == {"express", "sticker_library"}
    result = await server.call_tool("express", {"intent": "放松", "turns_since": 3, "include_image": True})
    assert result.is_error is False
    assert any(item.type == "image" for item in result.content)
    resources = await server.list_resource_templates()
    assert any("sticker://" in str(resource.uri_template) for resource in resources)
    contents = await server.read_resource(f"sticker://{sticker.id}")
    assert contents[0].content


def test_http_app_mounts_mcp_and_requires_bearer_when_remote(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    server = create_server(library)
    app = create_http_app(server, library, host="0.0.0.0", port=9876, bearer_token="secret")
    assert app is not None


def test_remote_http_requires_bearer_for_mcp_and_manager(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    app = create_http_app(
        create_server(library), library, host="0.0.0.0", port=9876,
        bearer_token="secret", allowed_hosts={"agent.example"},
    )
    headers = {"Host": "agent.example:9876", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
    with TestClient(app) as client:
        assert client.get("/", headers={"Host": "agent.example:9876"}).status_code == 200
        assert client.get("/api/csrf", headers={"Host": "agent.example:9876"}).status_code == 403
        assert client.post("/mcp", headers=headers, json=payload).status_code == 401
        authorized = client.post("/mcp", headers={**headers, "Authorization": "Bearer secret"}, json=payload)
    assert authorized.status_code == 200


def test_streamable_http_protocol_initialize_and_list_tools(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    server = create_server(library)
    app = create_http_app(server, library, host="127.0.0.1", port=9876)
    headers = {"Host": "127.0.0.1:9876", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(app) as client:
        initialize = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        assert initialize.status_code == 200
        session_id = initialize.headers["mcp-session-id"]
        payload = json.loads(next(line[6:] for line in initialize.text.splitlines() if line.startswith("data: ")))
        assert payload["result"]["serverInfo"]["name"] == "sticker-mcp"
        listed = client.post("/mcp", headers={**headers, "Mcp-Session-Id": session_id}, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert listed.status_code == 200
        body = json.loads(next(line[6:] for line in listed.text.splitlines() if line.startswith("data: ")))
        assert {item["name"] for item in body["result"]["tools"]} == {"express", "sticker_library"}


def test_streamable_http_rejects_cross_origin_request(tmp_path: Path) -> None:
    library = StickerLibrary(tmp_path)
    app = create_http_app(create_server(library), library, host="127.0.0.1", port=9876)
    headers = {"Host": "127.0.0.1:9876", "Origin": "https://evil.example", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(app) as client:
        response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_stdio_protocol_lists_tools_without_polluting_stdout(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    env["STICKER_MCP_DATA_DIR"] = str(tmp_path)
    params = StdioServerParameters(command=sys.executable, args=["-m", "sticker_mcp", "serve", "--ui-port", "0"], env=env)
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        listed = await session.list_tools()
        assert {item.name for item in listed.tools} == {"express", "sticker_library"}
