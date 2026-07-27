from __future__ import annotations

import base64
import binascii
import io
import json
import re
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError


ASSET_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
IMAGE_MIME_BY_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
EXTENSION_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "text/uri-list": ".url",
}


class AssetError(ValueError):
    """Raised when an asset is missing, unsafe, or invalid."""


class StorageQuota:
    """Thread-safe byte accounting shared by multiple asset stores."""

    def __init__(self, max_bytes: int, used_bytes: int = 0) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if isinstance(used_bytes, bool) or not isinstance(used_bytes, int) or used_bytes < 0:
            raise ValueError("used_bytes must be a non-negative integer")
        if used_bytes > max_bytes:
            raise AssetError(f"storage quota exceeded ({max_bytes} bytes)")
        self._max_bytes = max_bytes
        self._used_bytes = used_bytes
        self._lock = threading.Lock()

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return self._used_bytes

    def reserve(self, size: int) -> None:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("reservation size must be a non-negative integer")
        with self._lock:
            if self._used_bytes + size > self._max_bytes:
                raise AssetError(f"shared asset store quota exceeded ({self._max_bytes} bytes)")
            self._used_bytes += size

    def release(self, size: int) -> None:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("release size must be a non-negative integer")
        with self._lock:
            if size > self._used_bytes:
                raise ValueError("release size exceeds reserved storage")
            self._used_bytes -= size


@dataclass(frozen=True)
class Asset:
    asset_id: str
    name: str
    mime_type: str
    media_type: str
    size: int
    created_at: float
    storage: str
    path: Path

    @property
    def uri(self) -> str:
        return f"image-client://assets/{self.asset_id}"

    def public_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "uri": self.uri,
            "name": self.name,
            "mime_type": self.mime_type,
            "media_type": self.media_type,
            "size": self.size,
            "created_at": self.created_at,
            "storage": self.storage,
        }


class AssetStore:
    def __init__(
        self,
        root: Path,
        allowed_input_roots: tuple[Path, ...],
        *,
        max_reference_bytes: int,
        max_output_bytes: int,
        max_store_bytes: int = 2 * 1024 * 1024 * 1024,
        max_image_pixels: int = 100_000_000,
        shared_quota: StorageQuota | None = None,
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.allowed_input_roots = tuple(path.resolve() for path in allowed_input_roots)
        self.max_reference_bytes = max_reference_bytes
        self.max_output_bytes = max_output_bytes
        self.max_store_bytes = max_store_bytes
        self.max_image_pixels = max_image_pixels
        self.shared_quota = shared_quota
        self._lock = threading.RLock()
        self._stored_bytes = sum(
            path.stat().st_size
            for path in self.root.iterdir()
            if path.is_file() and path.suffix != ".json"
        )

    def _asset_id(self, asset_id: str) -> str:
        value = str(asset_id).strip().lower()
        if not ASSET_ID_PATTERN.fullmatch(value):
            raise AssetError("asset_id is invalid")
        return value

    def _metadata_path(self, asset_id: str) -> Path:
        return self.root / f"{self._asset_id(asset_id)}.json"

    def _is_allowed_input(self, path: Path) -> bool:
        for root in self.allowed_input_roots:
            try:
                path.relative_to(root)
            except ValueError:
                continue
            return True
        return False

    def _read_limited(self, path: Path, limit: int) -> bytes:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise AssetError("cannot stat input file") from exc
        if size > limit:
            raise AssetError(f"file exceeds the {limit}-byte limit")
        try:
            with path.open("rb") as handle:
                data = handle.read(limit + 1)
        except OSError as exc:
            raise AssetError("cannot read input file") from exc
        if len(data) > limit:
            raise AssetError(f"file exceeds the {limit}-byte limit")
        return data

    def _validate_image(self, data: bytes) -> tuple[str, int, int]:
        if len(data) > self.max_output_bytes:
            raise AssetError(f"image exceeds the {self.max_output_bytes}-byte output limit")
        try:
            with Image.open(io.BytesIO(data)) as image:
                image_format = str(image.format or "").upper()
                width, height = image.size
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise AssetError("file is not a valid PNG, JPEG, or WebP image") from exc
        mime_type = IMAGE_MIME_BY_FORMAT.get(image_format)
        if mime_type is None:
            raise AssetError("only PNG, JPEG, and WebP images are supported")
        if width <= 0 or height <= 0 or width * height > self.max_image_pixels:
            raise AssetError(f"image exceeds the {self.max_image_pixels}-pixel limit")
        return mime_type, width, height

    def _write_asset(
        self,
        data: bytes,
        *,
        mime_type: str,
        media_type: str,
        name: str,
        storage: str,
        extra: dict[str, Any] | None = None,
    ) -> Asset:
        if len(data) > self.max_output_bytes:
            raise AssetError(f"asset exceeds the {self.max_output_bytes}-byte output limit")
        asset_id = uuid.uuid4().hex
        safe_name = Path(name).name or f"{media_type}-{asset_id}"
        extension = EXTENSION_BY_MIME.get(mime_type, Path(safe_name).suffix[:10])
        if not extension or "/" in extension or "\\" in extension:
            extension = ".bin"
        file_name = f"{asset_id}{extension.lower()}"
        asset_path = self.root / file_name
        created_at = time.time()
        metadata = {
            "asset_id": asset_id,
            "name": safe_name,
            "mime_type": mime_type,
            "media_type": media_type,
            "size": len(data),
            "created_at": created_at,
            "storage": storage,
            "file_name": file_name,
        }
        if extra:
            metadata.update(extra)
        with self._lock:
            if self._stored_bytes + len(data) > self.max_store_bytes:
                raise AssetError(
                    f"asset store quota exceeded ({self.max_store_bytes} bytes)"
                )
            metadata_path = self.root / f"{asset_id}.json"
            if self.shared_quota is not None:
                self.shared_quota.reserve(len(data))
            try:
                asset_path.write_bytes(data)
                asset_path.chmod(0o600)
                metadata_path.write_text(
                    json.dumps(metadata, ensure_ascii=True, sort_keys=True),
                    encoding="utf-8",
                )
                metadata_path.chmod(0o600)
            except BaseException as exc:
                asset_removed = False
                try:
                    asset_path.unlink(missing_ok=True)
                except OSError:
                    pass
                else:
                    asset_removed = True
                try:
                    metadata_path.unlink(missing_ok=True)
                except OSError:
                    pass
                if asset_removed:
                    if self.shared_quota is not None:
                        self.shared_quota.release(len(data))
                else:
                    self._stored_bytes += len(data)
                if isinstance(exc, OSError):
                    raise AssetError("asset storage is unavailable") from exc
                raise
            self._stored_bytes += len(data)
        return Asset(
            asset_id=asset_id,
            name=safe_name,
            mime_type=mime_type,
            media_type=media_type,
            size=len(data),
            created_at=created_at,
            storage=storage,
            path=asset_path,
        )

    def import_reference(self, raw_path: str) -> Asset:
        try:
            path = Path(raw_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AssetError(f"input file does not exist: {raw_path}") from exc
        if not path.is_file():
            raise AssetError("input path must be a regular file")
        if not self._is_allowed_input(path):
            roots = ", ".join(str(item) for item in self.allowed_input_roots)
            raise AssetError(f"input path is outside allowed roots: {roots}")
        data = self._read_limited(path, self.max_reference_bytes)
        mime_type, width, height = self._validate_image(data)
        return self._write_asset(
            data,
            mime_type=mime_type,
            media_type="image",
            name=path.name,
            storage="file",
            extra={"width": width, "height": height, "source": "import"},
        )

    def upload_reference(self, data_base64: str, *, name: str = "reference-image") -> Asset:
        encoded = str(data_base64).strip()
        if not encoded:
            raise AssetError("reference image base64 is empty")
        if encoded.startswith("data:"):
            raise AssetError("reference image must be raw base64 without a data URL prefix")
        max_encoded = ((self.max_reference_bytes + 2) // 3) * 4
        if len(encoded) > max_encoded:
            raise AssetError(
                f"reference image exceeds the {self.max_reference_bytes}-byte limit"
            )
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise AssetError("reference image contains invalid base64") from exc
        if not data:
            raise AssetError("reference image is empty")
        if len(data) > self.max_reference_bytes:
            raise AssetError(
                f"reference image exceeds the {self.max_reference_bytes}-byte limit"
            )
        mime_type, width, height = self._validate_image(data)
        return self._write_asset(
            data,
            mime_type=mime_type,
            media_type="image",
            name=name,
            storage="file",
            extra={"width": width, "height": height, "source": "upload"},
        )

    def store_data_url(self, value: str, *, media_type: str, name: str) -> Asset:
        header, separator, encoded = value.partition(",")
        if not separator or not header.startswith("data:") or ";base64" not in header:
            raise AssetError("media result is not a base64 data URL")
        claimed_mime = header[5:].split(";", 1)[0].strip().lower()
        max_encoded = ((self.max_output_bytes + 2) // 3) * 4 + 8
        if len(encoded) > max_encoded:
            raise AssetError(f"asset exceeds the {self.max_output_bytes}-byte output limit")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise AssetError("media result contains invalid base64") from exc
        extra: dict[str, Any] = {"source": "generated"}
        if media_type == "image":
            mime_type, width, height = self._validate_image(data)
            if claimed_mime.startswith("image/") and claimed_mime != mime_type:
                raise AssetError("media MIME type does not match its image data")
            extra.update({"width": width, "height": height})
        else:
            mime_type = claimed_mime if claimed_mime.startswith("video/") else "video/mp4"
        return self._write_asset(
            data,
            mime_type=mime_type,
            media_type=media_type,
            name=name,
            storage="file",
            extra=extra,
        )

    def store_url_pointer(self, value: str, *, media_type: str, name: str) -> Asset:
        url = value.strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or len(url) > 8192:
            raise AssetError("media result URL is invalid")
        return self._write_asset(
            (url + "\n").encode("utf-8"),
            mime_type="text/uri-list",
            media_type=media_type,
            name=name,
            storage="url-pointer",
            extra={"source": "generated"},
        )

    def store_media_value(self, value: str, *, media_type: str, index: int) -> Asset:
        name = f"generated-{media_type}-{index}"
        if value.startswith("data:"):
            return self.store_data_url(value, media_type=media_type, name=name)
        if value.startswith(("http://", "https://")):
            return self.store_url_pointer(value, media_type=media_type, name=name)
        raise AssetError("upstream returned an unsupported media value")

    def get(self, asset_id: str) -> Asset:
        metadata_path = self._metadata_path(asset_id)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AssetError("asset does not exist") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise AssetError("asset metadata is unreadable") from exc
        file_name = str(metadata.get("file_name", ""))
        path = (self.root / file_name).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise AssetError("asset metadata points outside the asset store") from exc
        if not path.is_file():
            raise AssetError("asset content does not exist")
        try:
            actual_size = path.stat().st_size
        except OSError as exc:
            raise AssetError("asset content is unreadable") from exc
        return Asset(
            asset_id=self._asset_id(str(metadata.get("asset_id", ""))),
            name=str(metadata.get("name", "asset")),
            mime_type=str(metadata.get("mime_type", "application/octet-stream")),
            media_type=str(metadata.get("media_type", "file")),
            size=int(metadata.get("size", actual_size)),
            created_at=float(metadata.get("created_at", 0)),
            storage=str(metadata.get("storage", "file")),
            path=path,
        )

    def read(self, asset_id: str) -> bytes:
        asset = self.get(asset_id)
        return self._read_limited(asset.path, self.max_output_bytes)

    def as_data_url(self, asset_id: str) -> dict[str, str]:
        asset = self.get(asset_id)
        if asset.media_type != "image" or not asset.mime_type.startswith("image/"):
            raise AssetError(f"asset {asset_id!r} is not an image file")
        data = self._read_limited(asset.path, self.max_reference_bytes)
        return {
            "name": asset.name,
            "content_type": asset.mime_type,
            "data_url": f"data:{asset.mime_type};base64,{base64.b64encode(data).decode('ascii')}",
        }

    def delete(self, asset_id: str) -> int:
        """Remove an asset and release its bytes. Returns the bytes reclaimed."""
        normalized = self._asset_id(asset_id)
        metadata_path = self._metadata_path(normalized)
        with self._lock:
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return 0
            except (OSError, json.JSONDecodeError):
                metadata = {}
            reclaimed = 0
            file_name = str(metadata.get("file_name", ""))
            if file_name:
                path = (self.root / file_name).resolve()
                try:
                    path.relative_to(self.root)
                except ValueError:
                    path = None  # metadata points outside the store; never touch it
                if path is not None:
                    try:
                        reclaimed = path.stat().st_size
                    except OSError:
                        reclaimed = 0
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as exc:
                        raise AssetError("asset storage is unavailable") from exc
            try:
                metadata_path.unlink(missing_ok=True)
            except OSError as exc:
                raise AssetError("asset storage is unavailable") from exc
            # Never let the counters drift below zero if the file vanished behind us.
            reclaimed = min(reclaimed, self._stored_bytes)
            self._stored_bytes -= reclaimed
            if self.shared_quota is not None and reclaimed:
                try:
                    self.shared_quota.release(reclaimed)
                except ValueError:
                    pass
            return reclaimed

    def delete_many(self, asset_ids: Iterable[str]) -> int:
        total = 0
        for asset_id in asset_ids:
            try:
                total += self.delete(asset_id)
            except AssetError:
                continue
        return total

    def prune(self, *, max_age_seconds: float | None = None, now: float | None = None) -> int:
        """Evict assets past their TTL, then oldest-first until under the store cap."""
        current = time.time() if now is None else now
        with self._lock:
            entries: list[tuple[float, str]] = []
            for path in self.root.glob("*.json"):
                try:
                    entries.append((path.stat().st_mtime, path.stem))
                except OSError:
                    continue
            entries.sort(key=lambda item: item[0])
            reclaimed = 0
            if max_age_seconds is not None:
                for mtime, asset_id in list(entries):
                    if current - mtime <= max_age_seconds:
                        break
                    try:
                        reclaimed += self.delete(asset_id)
                    except AssetError:
                        continue
                    entries.remove((mtime, asset_id))
            # Keep headroom so a full store cannot permanently reject new writes.
            target = int(self.max_store_bytes * 0.9)
            for mtime, asset_id in entries:
                if self._stored_bytes <= target:
                    break
                try:
                    reclaimed += self.delete(asset_id)
                except AssetError:
                    continue
            return reclaimed

    def list(self, limit: int = 100) -> list[Asset]:
        assets: list[Asset] = []
        metadata_paths: list[tuple[float, Path]] = []
        for path in self.root.glob("*.json"):
            try:
                metadata_paths.append((path.stat().st_mtime, path))
            except OSError:
                continue
        for _, path in sorted(metadata_paths, key=lambda item: item[0], reverse=True):
            try:
                assets.append(self.get(path.stem))
            except AssetError:
                continue
            if len(assets) >= limit:
                break
        return assets
