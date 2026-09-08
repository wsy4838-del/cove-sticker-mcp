from __future__ import annotations

import base64
import json
import os
import secrets
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from .assets import AssetError
from .library import StickerLibrary
from .vision import HttpVisionProvider, TagQueue, VisionConfig, VisionError

MAX_UPLOAD_BODY = 100 * 1024 * 1024
MAX_FORM_PART = 50 * 1024 * 1024


class RequestTooLarge(Exception):
    pass


class _DisabledVision:
    async def describe(self, image: bytes, mime_type: str, filename: str) -> dict[str, object]:
        raise VisionError("vision provider is not configured")


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise RequestTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestTooLarge:
            await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"error":"request body is too large"}'})


def _json_sticker(sticker, *, include_deleted: bool = True) -> dict[str, object]:
    value = sticker.metadata()
    value["include_deleted"] = include_deleted
    return value


class WebController:
    def __init__(self, library: StickerLibrary, *, queue: TagQueue | None = None,
                 bearer_token: str | None = None, allowed_hosts: set[str] | None = None):
        self.library = library
        self.queue = queue
        self.bearer_token = bearer_token
        self.allowed_hosts = allowed_hosts or {"127.0.0.1", "localhost", "::1"}
        self.csrf_tokens: set[str] = set()
        self.owns_queue = queue is None

    def _vision_config(self) -> VisionConfig:
        settings = self.library.vision_settings()
        provider = settings.get("provider", "") or os.environ.get("STICKER_MCP_VISION_PROVIDER", "").strip().lower()
        default_env = "STICKER_MCP_MINIMAX_API_KEY" if provider == "minimax" else "STICKER_MCP_VISION_API_KEY"
        return VisionConfig(
            provider=provider, model=settings.get("model", "") or os.environ.get("STICKER_MCP_VISION_MODEL", ""),
            base_url=settings.get("base_url", "") or os.environ.get("STICKER_MCP_VISION_BASE_URL", ""),
            api_key_env=settings.get("api_key_env", "") or os.environ.get("STICKER_MCP_VISION_API_KEY_ENV", default_env),
            api_key=self.library.vision_api_key(),
            provider_name=settings.get("provider_name", "") or os.environ.get("STICKER_MCP_VISION_PROVIDER_NAME", ""),
            endpoint_url=settings.get("endpoint_url", "") or os.environ.get("STICKER_MCP_VISION_ENDPOINT_URL", ""),
        )

    def _refresh_queue(self) -> TagQueue:
        config = self._vision_config()
        provider = HttpVisionProvider(config) if config.enabled else _DisabledVision()
        if self.queue is None:
            self.queue = TagQueue(self.library, provider, enabled=config.enabled)
        elif hasattr(self.queue, "configure"):
            self.queue.configure(provider, enabled=config.enabled)
        else:
            self.queue.enabled = config.enabled
        return self.queue

    def _host_ok(self, request: Request) -> bool:
        return (request.url.hostname or "").lower().strip("[]") in self.allowed_hosts

    def _authorized(self, request: Request) -> bool:
        if not self.bearer_token:
            return True
        value = request.headers.get("authorization", "")
        return secrets.compare_digest(value, f"Bearer {self.bearer_token}")

    def _same_origin(self, request: Request) -> bool:
        source = request.headers.get("origin") or request.headers.get("referer")
        if not source:
            return False
        parsed = urlsplit(source)
        return parsed.scheme == request.url.scheme and parsed.hostname == request.url.hostname and (parsed.port or (443 if parsed.scheme == "https" else 80)) == (request.url.port or (443 if request.url.scheme == "https" else 80))

    def _write_allowed(self, request: Request) -> bool:
        csrf = request.headers.get("x-csrf-token", "")
        return self._authorized(request) and self._same_origin(request) and bool(csrf) and csrf == request.cookies.get("sticker_csrf") and csrf in self.csrf_tokens

    async def home(self, request: Request) -> Response:
        if not self._host_ok(request):
            return JSONResponse({"error": "untrusted host"}, status_code=421)
        index = Path(__file__).parent / "static" / "index.html"
        if index.is_file():
            return FileResponse(index)
        # Keep the API usable while an optional frontend asset is being installed.
        # The normal wheel includes the full UI; this fallback deliberately contains
        # no user data or inline executable content.
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>sticker-mcp</title>"
            "<main><h1>表情包管理</h1><p>管理界面资源尚未安装，请检查软件包内容。</p></main>"
        )

    async def csrf(self, request: Request) -> Response:
        if not self._host_ok(request) or not self._authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        token = secrets.token_urlsafe(24)
        self.csrf_tokens.add(token)
        response = JSONResponse({"token": token})
        response.set_cookie("sticker_csrf", token, httponly=True, samesite="strict", secure=request.url.scheme == "https")
        return response

    async def stickers(self, request: Request) -> Response:
        if not self._host_ok(request) or not self._authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            result = self.library.search(request.query_params.get("q", ""), page=int(request.query_params.get("page", "1")), page_size=int(request.query_params.get("page_size", "50")), include_deleted=request.query_params.get("include_deleted") == "1")
        except (TypeError, ValueError):
            return JSONResponse({"error": "invalid pagination"}, status_code=400)
        return JSONResponse({"items": [_json_sticker(item) for item in result.items], "total": result.total, "page": result.page, "page_size": result.page_size})

    async def sticker(self, request: Request) -> Response:
        if not self._host_ok(request) or not self._authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        sticker_id = request.path_params["sticker_id"]
        try:
            item = self.library.get(sticker_id, include_deleted=True)
        except KeyError:
            return JSONResponse({"error": "not found"}, status_code=404)
        if request.method == "GET":
            return JSONResponse(_json_sticker(item))
        if not self._write_allowed(request):
            return JSONResponse({"error": "same-origin CSRF check failed"}, status_code=403)
        if request.method == "DELETE":
            self.library.soft_delete(sticker_id)
            return JSONResponse({"ok": True})
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "JSON object required"}, status_code=400)
        try:
            updated = self.library.update_metadata(sticker_id, description=payload.get("description"), ocr_text=payload.get("ocr_text"), semantic_description=payload.get("semantic_description"), emotions=payload.get("emotions"), scenes=payload.get("scenes"), keywords=payload.get("keywords"))
        except (KeyError, ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)[:200]}, status_code=400)
        return JSONResponse(_json_sticker(updated))

    async def import_stickers(self, request: Request) -> Response:
        if not self._host_ok(request) or not self._write_allowed(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            if int(request.headers.get("content-length", "0") or 0) > MAX_UPLOAD_BODY:
                return JSONResponse({"error": "upload is too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"error": "invalid content length"}, status_code=400)
        try:
            form = await request.form(max_files=1000, max_fields=20, max_part_size=MAX_FORM_PART)
        except (ValueError, RuntimeError) as exc:
            return JSONResponse({"error": str(exc)[:200]}, status_code=413)
        files = []
        imported = []
        duplicates = []
        rejected = []
        for value in form.getlist("files"):
            if not hasattr(value, "read"):
                continue
            name = getattr(value, "filename", "sticker") or "sticker"
            data = await value.read()
            await value.close()
            if name.lower().endswith(".zip"):
                try:
                    report = self.library.import_zip(data, name)
                except (AssetError, ValueError) as exc:
                    return JSONResponse({"error": str(exc)[:200]}, status_code=400)
                imported.extend(report.imported)
                duplicates.extend(report.duplicates)
                rejected.extend(report.rejected)
                continue
            files.append((name, data))
        if files:
            report = self.library.import_many(files)
            imported.extend(report.imported)
            duplicates.extend(report.duplicates)
            rejected.extend(report.rejected)
        jobs = self._queue_imported(imported)
        return JSONResponse({"imported": [_json_sticker(item) for item in imported], "duplicates": duplicates, "rejected": rejected, "job_ids": jobs}, status_code=201)

    def _queue_imported(self, stickers: list) -> list[int]:
        if not self.library.settings().auto_tag:
            return []
        if self.queue is None:
            if not self._vision_config().enabled:
                return []
            self._refresh_queue()
        if not getattr(self.queue, "enabled", False):
            return []
        jobs: list[int] = []
        for sticker in stickers[:100]:
            try:
                jobs.append(self.queue.submit(sticker.id, source="auto"))
            except (ValueError, KeyError):
                continue
        return jobs

    async def restore_sticker(self, request: Request) -> Response:
        if not self._host_ok(request) or not self._write_allowed(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            sticker = self.library.restore(request.path_params["sticker_id"])
        except KeyError:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(_json_sticker(sticker))

    async def settings(self, request: Request) -> Response:
        if not self._host_ok(request) or not self._authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if request.method == "GET":
            return JSONResponse(self.library.settings().to_dict())
        if not self._write_allowed(request):
            return JSONResponse({"error": "same-origin CSRF check failed"}, status_code=403)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("settings must be an object")
            value = self.library.update_settings(**payload)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)[:200]}, status_code=400)
        return JSONResponse(value.to_dict())

    async def feedback(self, request: Request) -> Response:
        if not self._host_ok(request) or not self._write_allowed(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("JSON object required")
            value = self.library.feedback(request.path_params["sticker_id"], payload.get("value", ""))
        except (KeyError, ValueError) as exc:
            return JSONResponse({"error": str(exc)[:200]}, status_code=400)
        return JSONResponse(_json_sticker(value))

    async def image(self, request: Request) -> Response:
        if not self._host_ok(request) or not self._authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            sticker = self.library.get(request.path_params["sticker_id"], include_deleted=True)
            data = self.library.asset_bytes(sticker.id, include_deleted=True)
        except KeyError:
            return JSONResponse({"error": "not found"}, status_code=404)
        return Response(data, media_type=sticker.mime_type, headers={"Cache-Control": "private, max-age=3600"})

    async def backup(self, request: Request) -> Response:
        if not self._host_ok(request) or not self._write_allowed(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        bundle = self.library.export_backup()
        return FileResponse(bundle.path, filename="sticker-backup.zip", media_type="application/zip")

    async def restore_backup(self, request: Request) -> Response:
        if not self._host_ok(request) or not self._write_allowed(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            if int(request.headers.get("content-length", "0") or 0) > MAX_UPLOAD_BODY:
                return JSONResponse({"error": "backup is too large"}, status_code=413)
            form = await request.form(max_files=2, max_fields=5, max_part_size=MAX_UPLOAD_BODY)
            upload = next((value for value in form.getlist("file") if hasattr(value, "read")), None)
            if upload is None:
                return JSONResponse({"error": "backup file is required"}, status_code=400)
            raw = await upload.read()
            await upload.close()
            with tempfile.NamedTemporaryFile(prefix="sticker-restore-", suffix=".zip", dir=self.library.root, delete=False) as handle:
                handle.write(raw)
                path = Path(handle.name)
            try:
                report = self.library.restore_backup(path)
            finally:
                path.unlink(missing_ok=True)
            return JSONResponse({"imported": [_json_sticker(item) for item in report.imported], "duplicates": report.duplicates}, status_code=200)
        except (AssetError, ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)[:200]}, status_code=400)

    async def vision(self, request: Request) -> Response:
        if not self._host_ok(request) or not self._authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if request.method == "GET":
            return JSONResponse(self._vision_payload())
        if not self._write_allowed(request):
            return JSONResponse({"error": "same-origin CSRF check failed"}, status_code=403)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("JSON object required")
            self.library.update_vision_settings(**payload)
            self._refresh_queue()
            return JSONResponse(self._vision_payload())
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)[:200]}, status_code=400)

    def _vision_payload(self) -> dict[str, object]:
        value: dict[str, object] = dict(self.library.vision_settings())
        config = self._vision_config()
        env_name = str(value.get("api_key_env", "")) or config.api_key_env
        private_configured = bool(self.library.vision_api_key())
        env_configured = bool(env_name and os.environ.get(env_name))
        value["configured"] = config.enabled
        value["api_key"] = "••••" if private_configured else ("env:" + env_name if env_configured else "")
        return value

    async def vision_test(self, request: Request) -> Response:
        if not self._host_ok(request) or not self._write_allowed(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or payload.get("confirm_cost") is not True:
                return JSONResponse({"error": "confirm_cost=true is required; this may call a paid vision provider"}, status_code=400)
            config = self._vision_config()
            if not config.enabled:
                return JSONResponse({"error": "vision provider is not configured"}, status_code=409)
            image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
            result = await HttpVisionProvider(config).describe(image, "image/png", "test.png")
            return JSONResponse({"ok": True, "tags": result})
        except (TimeoutError, ValueError, VisionError) as exc:
            return JSONResponse({"error": str(exc)[:200]}, status_code=502)

    async def tag(self, request: Request) -> Response:
        if not self._host_ok(request) or not self._write_allowed(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if self.queue is None and self._vision_config().enabled:
            self._refresh_queue()
        if self.queue is None or not getattr(self.queue, "enabled", False):
            return JSONResponse({"error": "vision provider is not configured"}, status_code=409)
        try:
            payload = await request.json()
            ids = payload.get("ids", []) if isinstance(payload, dict) else []
            if isinstance(payload, dict) and payload.get("all_pending"):
                ids = self.library.untagged_ids(limit=100)
            if not isinstance(ids, list) or len(ids) > 100:
                raise ValueError("ids must contain at most 100 items")
            jobs = [self.queue.submit(str(sticker_id)) for sticker_id in ids]
            return JSONResponse({"job_ids": jobs}, status_code=202)
        except (KeyError, ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)[:200]}, status_code=400)

    async def jobs(self, request: Request) -> Response:
        if not self._host_ok(request) or not self._authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse({"jobs": self.library.list_tag_jobs()})

    async def retry_job(self, request: Request) -> Response:
        if not self._host_ok(request) or not self._write_allowed(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if self.queue is None and self._vision_config().enabled:
            self._refresh_queue()
        if self.queue is None:
            return JSONResponse({"error": "vision provider is not configured"}, status_code=409)
        try:
            self.queue.retry(int(request.path_params["job_id"]))
            return JSONResponse({"queued": True}, status_code=202)
        except (KeyError, ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)[:200]}, status_code=400)


def create_app(library: StickerLibrary, *, queue: TagQueue | None = None,
               bearer_token: str | None = None, allowed_hosts: set[str] | None = None) -> Starlette:
    controller = WebController(library, queue=queue, bearer_token=bearer_token, allowed_hosts=allowed_hosts)
    routes = [
        Route("/", controller.home), Route("/api/csrf", controller.csrf),
        Route("/api/stickers", controller.stickers, methods=["GET"]),
        Route("/api/stickers", controller.import_stickers, methods=["POST"]),
        Route("/api/stickers/{sticker_id}", controller.sticker, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/stickers/{sticker_id}/restore", controller.restore_sticker, methods=["POST"]),
        Route("/api/stickers/{sticker_id}/feedback", controller.feedback, methods=["POST"]),
        Route("/api/settings", controller.settings, methods=["GET", "PATCH"]),
        Route("/api/vision", controller.vision, methods=["GET", "PATCH"]),
        Route("/api/vision/test", controller.vision_test, methods=["POST"]),
        Route("/api/tag", controller.tag, methods=["POST"]),
        Route("/api/jobs", controller.jobs, methods=["GET"]),
        Route("/api/jobs/{job_id}/retry", controller.retry_job, methods=["POST"]),
        Route("/api/backup", controller.backup, methods=["POST"]),
        Route("/api/restore", controller.restore_backup, methods=["POST"]),
        Route("/assets/{sticker_id}", controller.image, methods=["GET"]),
    ]
    @asynccontextmanager
    async def lifespan(app: Starlette):
        try:
            yield
        finally:
            if controller.owns_queue and controller.queue is not None:
                controller.queue.shutdown()

    app = Starlette(routes=routes, middleware=[Middleware(BodyLimitMiddleware, max_bytes=MAX_UPLOAD_BODY)], lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    return app
