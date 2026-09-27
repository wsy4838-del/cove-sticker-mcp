
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

import platformdirs
import uvicorn
from filelock import FileLock, Timeout

from . import __version__
from .library import StickerLibrary
from .mcp_server import create_http_app, create_server
from .vision import HttpVisionProvider, TagQueue, VisionConfig, VisionError
from .web import create_app

LOGGER = logging.getLogger("sticker_mcp")


class DisabledVision:
    async def describe(
        self,
        image: bytes,
        mime_type: str,
        filename: str,
    ) -> dict[str, object]:
        raise VisionError("vision provider is not configured")


def data_root() -> Path:
    return Path(
        os.environ.get("STICKER_MCP_DATA_DIR")
        or platformdirs.user_data_dir("sticker-mcp")
    )


def vision_config(
    overrides: dict[str, str] | None = None,
    *,
    api_key: str = "",
) -> VisionConfig:
    overrides = overrides or {}

    provider = str(
        overrides.get("provider")
        or os.environ.get("STICKER_MCP_VISION_PROVIDER", "")
    ).strip().lower()

    default_key = (
        "STICKER_MCP_MINIMAX_API_KEY"
        if provider == "minimax"
        else "STICKER_MCP_VISION_API_KEY"
    )

    return VisionConfig(
        provider=provider,
        model=str(
            overrides.get("model")
            or os.environ.get(
                "STICKER_MCP_VISION_MODEL",
                "MiniMax-M3" if provider == "minimax" else "",
            )
        ).strip(),
        base_url=str(
            overrides.get("base_url")
            or os.environ.get("STICKER_MCP_VISION_BASE_URL", "")
        ).strip(),
        api_key_env=str(
            overrides.get("api_key_env")
            or os.environ.get(
                "STICKER_MCP_VISION_API_KEY_ENV",
                default_key,
            )
        ).strip(),
        timeout_seconds=min(
            120.0,
            max(
                5.0,
                float(
                    os.environ.get(
                        "STICKER_MCP_VISION_TIMEOUT",
                        "45",
                    )
                ),
            ),
        ),
        max_output_tokens=min(
            2000,
            max(
                100,
                int(
                    os.environ.get(
                        "STICKER_MCP_VISION_MAX_OUTPUT",
                        "1000",
                    )
                ),
            ),
        ),
        api_key=api_key,
        provider_name=str(
            overrides.get("provider_name")
            or os.environ.get(
                "STICKER_MCP_VISION_PROVIDER_NAME",
                "",
            )
        ).strip(),
        endpoint_url=str(
            overrides.get("endpoint_url")
            or os.environ.get(
                "STICKER_MCP_VISION_ENDPOINT_URL",
                "",
            )
        ).strip(),
    )


def build_services() -> tuple[
    StickerLibrary,
    TagQueue,
    VisionConfig,
    FileLock,
]:
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)

    lock = FileLock(str(root / "instance.lock"))

    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise SystemExit(
            "another sticker-mcp instance is already running for this "
            "data directory; use one HTTP instance or a different data directory"
        ) from exc

    try:
        library = StickerLibrary(root)
        config = vision_config(
            library.vision_settings(),
            api_key=library.vision_api_key(),
        )

        provider = (
            HttpVisionProvider(config)
            if config.enabled
            else DisabledVision()
        )

        queue = TagQueue(
            library,
            provider,
            enabled=config.enabled,
        )

    except Exception:
        lock.release()
        raise

    return library, queue, config, lock


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def _allow_no_auth() -> bool:
    return (
        os.environ.get(
            "STICKER_MCP_ALLOW_NO_AUTH",
            "",
        )
        .strip()
        .lower()
        in {"1", "true", "yes"}
    )


def _port_available(host: str, port: int) -> bool:
    with socket.socket(
        socket.AF_INET6 if ":" in host else socket.AF_INET,
        socket.SOCK_STREAM,
    ) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False

    return True


def _choose_port(host: str, port: int) -> int:
    if port:
        return port

    with socket.socket(
        socket.AF_INET6 if ":" in host else socket.AF_INET,
        socket.SOCK_STREAM,
    ) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def start_ui_thread(
    library: StickerLibrary,
    host: str,
    port: int,
    token: str | None,
) -> str | None:

    if port == 0:
        LOGGER.info(
            "management UI disabled because --ui-port=0"
        )
        return None

    actual_port = _choose_port(host, port)

    if not _port_available(host, actual_port):
        LOGGER.warning(
            "management UI port %s is unavailable; "
            "continuing with MCP only",
            port,
        )
        return None

    def serve_ui() -> None:
        ui_library = StickerLibrary(library.root)

        config = vision_config(
            ui_library.vision_settings(),
            api_key=ui_library.vision_api_key(),
        )

        provider = (
            HttpVisionProvider(config)
            if config.enabled
            else DisabledVision()
        )

        ui_queue = TagQueue(
            ui_library,
            provider,
            enabled=config.enabled,
            recover=False,
        )

        app = create_app(
            ui_library,
            queue=ui_queue,
            bearer_token=token,
            allowed_hosts={
                host.strip("[]"),
                "127.0.0.1",
                "localhost",
                "::1",
            },
        )

        try:
            uvicorn.run(
                app,
                host=host,
                port=actual_port,
                log_level="warning",
            )
        finally:
            ui_queue.shutdown()
            ui_library.close()

    thread = threading.Thread(
        target=serve_ui,
        name="sticker-web",
        daemon=True,
    )

    thread.start()

    deadline = time.monotonic() + 2.0

    while (
        time.monotonic() < deadline
        and _port_available(host, actual_port)
    ):
        time.sleep(0.01)

    if _port_available(host, actual_port):
        LOGGER.warning(
            "management UI did not become ready on port %s",
            actual_port,
        )
        return None

    return f"http://{host}:{actual_port}/"


def _public_url(args: argparse.Namespace) -> str:
    if args.public_url:
        value = str(args.public_url).strip().rstrip("/")
        parsed = urlsplit(value)

        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise SystemExit(
                "--public-url must be an absolute http(s) URL"
            )

        return value + "/"

    return f"http://{args.host}:{args.port}/"


def run_stdio(args: argparse.Namespace) -> None:
    library, queue, config, lock = build_services()

    try:
        token = os.environ.get("STICKER_MCP_BEARER_TOKEN")

        ui_url = start_ui_thread(
            library,
            "127.0.0.1",
            args.ui_port,
            token,
        )

        if config.enabled:
            LOGGER.info(
                "vision provider configured; tagging remains explicit and queued"
            )
        else:
            LOGGER.info(
                "no vision provider configured; manual library use remains available"
            )

        server = create_server(
            library,
            management_url=ui_url or "unavailable://management-ui",
        )

        server.run(transport="stdio")

    finally:
        try:
            queue.shutdown()
            library.close()
        finally:
            lock.release()


def run_http(args: argparse.Namespace) -> None:
    if (
        not _is_loopback(args.host)
        and not args.bearer_token
        and not _allow_no_auth()
    ):
        raise SystemExit(
            "--bearer-token or STICKER_MCP_BEARER_TOKEN "
            "is required for non-loopback HTTP"
        )

    trusted_hosts = (
        set(
            args.allowed_host
            or (
                []
                if args.host in {"0.0.0.0", "::"}
                else [args.host]
            )
        )
        | {
            "127.0.0.1",
            "localhost",
            "::1",
        }
    )

    if args.host in {"0.0.0.0", "::"} and not args.allowed_host:
        raise SystemExit(
            "--allowed-host is required when binding a wildcard address"
        )

    public_url = _public_url(args)

    library, queue, _, lock = build_services()

    try:
        server = create_server(
            library,
            management_url=public_url,
        )

        app = create_http_app(
            server,
            library,
            host=args.host,
            port=args.port,
            bearer_token=args.bearer_token,
            queue=queue,
            allowed_hosts=trusted_hosts,
        )

        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="info",
        )

    finally:
        try:
            queue.shutdown()
            library.close()
        finally:
            lock.release()


def run_web(args: argparse.Namespace) -> None:
    if (
        not _is_loopback(args.host)
        and not args.bearer_token
        and not _allow_no_auth()
    ):
        raise SystemExit(
            "--bearer-token or STICKER_MCP_BEARER_TOKEN "
            "is required for non-loopback web"
        )

    trusted_hosts = (
        set(
            args.allowed_host
            or (
                []
                if args.host in {"0.0.0.0", "::"}
                else [args.host]
            )
        )
        | {
            "127.0.0.1",
            "localhost",
            "::1",
        }
    )

    if args.host in {"0.0.0.0", "::"} and not args.allowed_host:
        raise SystemExit(
            "--allowed-host is required when binding a wildcard address"
        )

    public_url = _public_url(args)

    library, queue, _, lock = build_services()

    try:
        app = create_app(
            library,
            queue=queue,
            bearer_token=args.bearer_token,
            allowed_hosts=trusted_hosts,
        )

        if args.open:
            threading.Timer(
                0.5,
                lambda: webbrowser.open(public_url),
            ).start()

        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="info",
        )

    finally:
        try:
            queue.shutdown()
            library.close()
        finally:
            lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sticker-mcp"
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"sticker-mcp {__version__}",
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    serve = sub.add_parser(
        "serve",
        help="run MCP over stdio",
    )

    serve.add_argument(
        "--ui-port",
        type=int,
        default=8765,
    )

    http = sub.add_parser(
        "serve-http",
        help="run MCP and manager over Streamable HTTP",
    )

    http.add_argument(
        "--host",
        default="127.0.0.1",
    )

    http.add_argument(
        "--port",
        type=int,
        default=8765,
    )

    http.add_argument(
        "--bearer-token",
        default=os.environ.get(
            "STICKER_MCP_BEARER_TOKEN"
        ),
    )

    http.add_argument(
        "--allowed-host",
        action="append",
        default=[],
    )

    http.add_argument(
        "--public-url"
    )

    web = sub.add_parser(
        "web",
        help="run only the local manager",
    )

    web.add_argument(
        "--host",
        default="127.0.0.1",
    )

    web.add_argument(
        "--port",
        type=int,
        default=8765,
    )

    web.add_argument(
        "--open",
        action="store_true",
    )

    web.add_argument(
        "--bearer-token",
        default=os.environ.get(
            "STICKER_MCP_BEARER_TOKEN"
        ),
    )

    web.add_argument(
        "--allowed-host",
        action="append",
        default=[],
    )

    web.add_argument(
        "--public-url"
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )

    if args.command == "serve":
        run_stdio(args)
    elif args.command == "serve-http":
        run_http(args)
    else:
        run_web(args)

    return 0