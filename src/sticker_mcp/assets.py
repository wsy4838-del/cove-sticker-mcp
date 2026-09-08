from __future__ import annotations

import hashlib
import io
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_IMAGE_DIMENSION = 8192
MAX_ZIP_ENTRIES = 1000
MAX_ZIP_BYTES = 100 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 200
SUPPORTED_FORMATS = {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"),
                     "WEBP": ("image/webp", ".webp"), "GIF": ("image/gif", ".gif")}
MAX_GIF_FRAMES = 10
MAX_GIF_TOTAL_PIXELS = 50_000_000


class AssetError(ValueError):
    """A supplied image cannot be safely stored."""


class UnsafeArchiveError(AssetError):
    """A ZIP violates archive safety limits or contains unsafe names."""


@dataclass(frozen=True)
class ImageInfo:
    sha256: str
    mime_type: str
    extension: str
    byte_size: int
    width: int
    height: int


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_filename(filename: str) -> str:
    name = Path(filename or "sticker").name.strip() or "sticker"
    if name in {".", ".."} or "\x00" in name:
        raise AssetError("invalid filename")
    return name[:200]


def validate_image(data: bytes, filename: str = "sticker") -> ImageInfo:
    if not isinstance(data, bytes) or not data:
        raise AssetError("image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise AssetError("image exceeds 10 MiB limit")
    _safe_filename(filename)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image_format = (image.format or "").upper()
                width, height = image.size
                if image_format not in SUPPORTED_FORMATS:
                    raise AssetError("image format is not supported")
                if width <= 0 or height <= 0 or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                    raise AssetError("image dimensions are unsafe")
                if width * height > MAX_IMAGE_PIXELS:
                    raise AssetError("image pixel count is unsafe")
                if image_format == "GIF":
                    frames = int(getattr(image, "n_frames", 1))
                    if frames > MAX_GIF_FRAMES:
                        raise AssetError("GIF contains too many frames")
                    if width * height * frames > MAX_GIF_TOTAL_PIXELS:
                        raise AssetError("GIF pixel budget is unsafe")
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
    except AssetError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise AssetError("unsupported or invalid image") from exc
    mime_type, extension = SUPPORTED_FORMATS[image_format]
    return ImageInfo(content_hash(data), mime_type, extension, len(data), width, height)


def asset_path(root: Path, info: ImageInfo) -> Path:
    return root / "assets" / f"{info.sha256}{info.extension}"


def _safe_zip_name(name: str) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise UnsafeArchiveError("unsafe ZIP path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafeArchiveError("unsafe ZIP path")
    return "/".join(path.parts)


def safe_zip_members(raw: bytes, *, max_entries: int = MAX_ZIP_ENTRIES,
                     max_total_bytes: int = MAX_ZIP_BYTES,
                     max_compression_ratio: int = MAX_ZIP_COMPRESSION_RATIO) -> list[tuple[zipfile.ZipInfo, str]]:
    if len(raw) > MAX_ZIP_BYTES:
        raise UnsafeArchiveError("ZIP exceeds 100 MiB limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise UnsafeArchiveError("invalid ZIP") from exc
    members: list[tuple[zipfile.ZipInfo, str]] = []
    total = 0
    try:
        infos = archive.infolist()
        if len(infos) > max_entries:
            raise UnsafeArchiveError("ZIP contains too many entries")
        for info in infos:
            safe_name = _safe_zip_name(info.filename)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise UnsafeArchiveError("ZIP symlink is not allowed")
            if info.is_dir():
                continue
            if info.file_size > MAX_IMAGE_BYTES:
                raise UnsafeArchiveError("ZIP member exceeds image limit")
            if info.compress_size and info.file_size / info.compress_size > max_compression_ratio:
                raise UnsafeArchiveError("ZIP compression ratio is unsafe")
            total += info.file_size
            if total > max_total_bytes:
                raise UnsafeArchiveError("ZIP expands beyond 100 MiB limit")
            members.append((info, safe_name))
    except Exception:
        archive.close()
        raise
    archive.close()
    return members
