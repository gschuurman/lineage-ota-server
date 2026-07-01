"""
Self-hosted OTA metadata + download server for the LineageOS Updater app.

Implements the JSON contract expected by
packages/apps/Updater's UpdatesNetworkDataSource:

  GET {BASE_URL}/api/v2/devices/{device}/builds
    -> 200, JSON array of:
       {
         "datetime": <unix seconds>,
         "type": "nightly",
         "version": "23.0",
         "os_patch_level": "2026-07-01",   # optional
         "os_sdk_level": 36,                # optional
         "files": [
           {
             "filename": "lineage-23.0-20260701-nightly-vim3-signed.zip",
             "sha256": "...",
             "size": 1234567890,
             "url": "https://updates.schuurman-it.com/download/vim3/lineage-...zip",
             "os_patch_level": "2026-07-01",  # optional
             "os_sdk_level": 36                # optional
           }
         ]
       }

Builds are discovered by scanning BUILDS_DIR/<device>/*.zip. Metadata is
parsed from the filename (lineage-<version>-<YYYYMMDD>-<type>-<device>[-signed].zip)
and can be overridden per-file by dropping a sidecar "<filename>.json" next
to the zip with any subset of: datetime, type, version, os_patch_level,
os_sdk_level.

sha256 hashes are expensive to compute for multi-GB images, so they are
cached on disk (BUILDS_DIR/.sha256_cache.json) keyed by path+size+mtime.
"""
from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

BUILDS_DIR = Path(os.environ.get("BUILDS_DIR", "/builds")).resolve()
BASE_URL = os.environ.get("BASE_URL", "https://updates.schuurman-it.com").rstrip("/")
SCAN_CACHE_TTL = int(os.environ.get("SCAN_CACHE_TTL_SECONDS", "60"))
RETAIN_BUILDS = int(os.environ.get("RETAIN_BUILDS", "5"))
UPLOAD_TOKEN = os.environ.get("UPLOAD_TOKEN")
HASH_CACHE_FILE = BUILDS_DIR / ".sha256_cache.json"

FILENAME_RE = re.compile(
    r"^lineage-(?P<version>[\w.]+)-(?P<date>\d{8})-(?P<type>\w+)-(?P<device>\w+)"
    r"(?:-signed)?\.zip$"
)

app = FastAPI(title="lineage-ota-server")

_hash_cache_lock = threading.Lock()
_hash_cache: dict[str, dict[str, Any]] = {}

_scan_lock = threading.Lock()
_scan_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


class FileEntry(BaseModel):
    filename: str
    sha256: str
    size: int
    url: str
    os_patch_level: str | None = None
    os_sdk_level: int | None = None


class BuildEntry(BaseModel):
    datetime: int
    type: str
    version: str
    os_patch_level: str | None = None
    os_sdk_level: int | None = None
    files: list[FileEntry]


def _load_hash_cache() -> None:
    if not HASH_CACHE_FILE.exists():
        return
    try:
        with _hash_cache_lock:
            _hash_cache.update(json.loads(HASH_CACHE_FILE.read_text()))
    except (json.JSONDecodeError, OSError):
        pass


def _save_hash_cache() -> None:
    tmp = HASH_CACHE_FILE.with_suffix(".tmp")
    with _hash_cache_lock:
        tmp.write_text(json.dumps(_hash_cache))
    tmp.replace(HASH_CACHE_FILE)


def _sha256_of(path: Path) -> str:
    stat = path.stat()
    key = str(path.relative_to(BUILDS_DIR))
    with _hash_cache_lock:
        cached = _hash_cache.get(key)
    if cached and cached["size"] == stat.st_size and cached["mtime"] == stat.st_mtime:
        return cached["sha256"]

    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    sha256 = digest.hexdigest()

    with _hash_cache_lock:
        _hash_cache[key] = {
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "sha256": sha256,
        }
    _save_hash_cache()
    return sha256


def _read_sidecar(zip_path: Path) -> dict[str, Any]:
    sidecar = zip_path.with_suffix(zip_path.suffix + ".json")
    if not sidecar.exists():
        return {}
    try:
        return json.loads(sidecar.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _parse_build(device: str, zip_path: Path) -> dict[str, Any] | None:
    match = FILENAME_RE.match(zip_path.name)
    if not match:
        return None

    overrides = _read_sidecar(zip_path)

    version = overrides.get("version", match.group("version"))
    build_type = overrides.get("type", match.group("type"))

    if "datetime" in overrides:
        build_datetime = int(overrides["datetime"])
    else:
        stat = zip_path.stat()
        build_datetime = int(stat.st_mtime)

    sha256 = _sha256_of(zip_path)
    size = zip_path.stat().st_size

    file_entry = {
        "filename": zip_path.name,
        "sha256": sha256,
        "size": size,
        "url": f"{BASE_URL}/download/{device}/{zip_path.name}",
        "os_patch_level": overrides.get("os_patch_level"),
        "os_sdk_level": overrides.get("os_sdk_level"),
    }

    return {
        "datetime": build_datetime,
        "type": build_type,
        "version": version,
        "os_patch_level": overrides.get("os_patch_level"),
        "os_sdk_level": overrides.get("os_sdk_level"),
        "files": [file_entry],
    }


def _scan_device(device: str) -> list[dict[str, Any]]:
    now = time.monotonic()
    with _scan_lock:
        cached = _scan_cache.get(device)
    if cached and now - cached[0] < SCAN_CACHE_TTL:
        return cached[1]

    device_dir = BUILDS_DIR / device
    builds: list[dict[str, Any]] = []
    if device_dir.is_dir():
        for zip_path in sorted(device_dir.glob("*.zip")):
            build = _parse_build(device, zip_path)
            if build is not None:
                builds.append(build)
        builds.sort(key=lambda b: b["datetime"], reverse=True)

    with _scan_lock:
        _scan_cache[device] = (now, builds)
    return builds


def _build_datetime(zip_path: Path, overrides: dict[str, Any]) -> int:
    if "datetime" in overrides:
        return int(overrides["datetime"])
    return int(zip_path.stat().st_mtime)


def _delete_build(zip_path: Path) -> None:
    sidecar = zip_path.with_suffix(zip_path.suffix + ".json")
    key = str(zip_path.relative_to(BUILDS_DIR))
    for p in (zip_path, sidecar):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    with _hash_cache_lock:
        _hash_cache.pop(key, None)


def _enforce_retention(device: str, retain: int) -> None:
    device_dir = BUILDS_DIR / device
    if not device_dir.is_dir():
        return

    entries: list[tuple[int, Path]] = []
    for zip_path in device_dir.glob("*.zip"):
        if not FILENAME_RE.match(zip_path.name):
            continue
        overrides = _read_sidecar(zip_path)
        entries.append((_build_datetime(zip_path, overrides), zip_path))
    entries.sort(key=lambda e: e[0], reverse=True)

    if len(entries) <= retain:
        return
    for _, zip_path in entries[retain:]:
        _delete_build(zip_path)
    _save_hash_cache()


_bearer_scheme = HTTPBearer()


def _check_upload_auth(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> None:
    if not UPLOAD_TOKEN:
        raise HTTPException(
            status_code=503, detail="uploads disabled: UPLOAD_TOKEN not configured"
        )
    if not hmac.compare_digest(credentials.credentials, UPLOAD_TOKEN):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.on_event("startup")
def _startup() -> None:
    BUILDS_DIR.mkdir(parents=True, exist_ok=True)
    _load_hash_cache()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v2/devices/{device}/builds", response_model=list[BuildEntry])
def get_builds(device: str) -> list[dict[str, Any]]:
    return _scan_device(device)


@app.get("/download/{device}/{filename}")
def download(device: str, filename: str) -> FileResponse:
    if "/" in device or "/" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="invalid path")

    zip_path = (BUILDS_DIR / device / filename).resolve()
    device_dir = (BUILDS_DIR / device).resolve()
    if device_dir not in zip_path.parents or not zip_path.is_file():
        raise HTTPException(status_code=404, detail="not found")

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=filename,
    )


@app.post("/api/v2/devices/{device}/builds", status_code=201)
async def upload_build(
    device: str,
    file: UploadFile = File(...),
    os_patch_level: str | None = Form(default=None),
    os_sdk_level: int | None = Form(default=None),
    build_datetime: int | None = Form(default=None, alias="datetime"),
    _auth: None = Depends(_check_upload_auth),
) -> dict[str, Any]:
    if not device or "/" in device:
        raise HTTPException(status_code=400, detail="invalid device")

    filename = Path(file.filename or "").name
    match = FILENAME_RE.match(filename)
    if not match:
        raise HTTPException(
            status_code=400,
            detail="filename must match lineage-<version>-<YYYYMMDD>-<type>-<device>[-signed].zip",
        )
    if match.group("device") != device:
        raise HTTPException(status_code=400, detail="filename device does not match URL device")

    device_dir = BUILDS_DIR / device
    device_dir.mkdir(parents=True, exist_ok=True)
    dest = device_dir / filename

    tmp_fd, tmp_name = tempfile.mkstemp(dir=device_dir, prefix=f".upload-{filename}-")
    try:
        with os.fdopen(tmp_fd, "wb") as tmp:
            while chunk := await file.read(1024 * 1024):
                tmp.write(chunk)
        os.replace(tmp_name, dest)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    overrides: dict[str, Any] = {}
    if os_patch_level is not None:
        overrides["os_patch_level"] = os_patch_level
    if os_sdk_level is not None:
        overrides["os_sdk_level"] = os_sdk_level
    if build_datetime is not None:
        overrides["datetime"] = build_datetime
    sidecar = dest.with_suffix(dest.suffix + ".json")
    if overrides:
        sidecar.write_text(json.dumps(overrides))
    else:
        sidecar.unlink(missing_ok=True)

    # Hash now so it's ready before the next API read, and to fail fast on
    # a corrupt/truncated upload rather than on the head unit's next poll.
    _sha256_of(dest)

    with _scan_lock:
        _scan_cache.pop(device, None)

    _enforce_retention(device, RETAIN_BUILDS)

    return {"status": "ok", "filename": filename}
