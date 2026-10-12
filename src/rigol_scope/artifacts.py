from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import struct
import uuid
import base64
import zlib

from .errors import ProtocolError, ScopeError


def validate_png(data: bytes) -> tuple[int, int]:
    """Validate PNG framing/CRCs/dimensions; not a replacement for image decoding."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ProtocolError("Instrument did not return PNG data")
    offset = 8
    width = height = 0
    has_data = False
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        if end > len(data):
            raise ProtocolError("Truncated PNG chunk")
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            raise ProtocolError("PNG chunk checksum mismatch")
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                raise ProtocolError("Missing PNG header")
            width, height = struct.unpack_from(">II", payload)
            if not 1 <= width <= 4096 or not 1 <= height <= 4096:
                raise ProtocolError("Unexpected PNG dimensions")
        if kind == b"IDAT":
            has_data = True
        if kind == b"IEND":
            if length or not has_data or end != len(data):
                raise ProtocolError("Invalid PNG ending")
            return width, height
        offset = end
    raise ProtocolError("Missing PNG end chunk")


def save_screenshot(state_dir: Path, png: bytes, identity: dict, device: str) -> dict:
    width, height = validate_png(png)
    capture_id = uuid.uuid4().hex
    directory = state_dir / "captures" / capture_id
    directory.mkdir(parents=True, mode=0o700)
    metadata = {"schema_version": 1, "capture_id": capture_id,
                "device": device, "identity": identity,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "kind": "screenshot", "status": "complete", "read_only": True,
                "width": width, "height": height, "bytes": len(png),
                "sha256": hashlib.sha256(png).hexdigest(),
                "png_validation": "signature, chunk framing, CRCs and dimensions",
                "image_uri": f"scope://artifacts/{capture_id}/screen.png",
                "metadata_uri": f"scope://artifacts/{capture_id}/metadata.json"}
    try:
        (directory / "screen.png").write_bytes(png)
        temporary = directory / "metadata.json.partial"
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(directory / "metadata.json")
    except OSError:
        # A capture without complete metadata must not be exposed as complete.
        raise
    return {**metadata, "directory": str(directory), "image_path": str(directory / "screen.png")}


SOURCE_NAME = r"(?:ch[1-4]\.(?:bin|csv)|screen\.png|metadata\.json)"
DERIVED_NAME = r"(?:plot-[0-9a-f]{32}\.png|analysis-[0-9a-f]{32}\.json|report-[0-9a-f]{32}\.md|window-[0-9a-f]{32}\.csv)"


def capture_directory(state_dir: Path, capture_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", capture_id):
        raise ScopeError("Invalid capture_id")
    root = (state_dir / "captures").resolve()
    directory = state_dir / "captures" / capture_id
    if directory.is_symlink() or directory.resolve().parent != root or not directory.is_dir():
        raise ScopeError("Capture directory is missing or unsafe")
    meta = directory / "metadata.json"
    if meta.is_symlink() or not meta.is_file():
        raise ScopeError("Capture is missing or incomplete")
    return directory


def artifact_path(state_dir: Path, capture_id: str, name: str) -> Path:
    directory = capture_directory(state_dir, capture_id)
    if not isinstance(name, str) or not re.fullmatch(SOURCE_NAME + "|" + DERIVED_NAME, name):
        raise ScopeError("Unknown artifact")
    path = directory / name
    if path.resolve().parent != directory.resolve() or path.is_symlink() or not path.is_file():
        raise ScopeError("Artifact path is not a regular capture file")
    if re.fullmatch(DERIVED_NAME, name):
        analysis_id = name.split("-")[1].split(".")[0]
        manifest = directory / f"analysis-{analysis_id}.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise ScopeError("Derived artifact is not committed")
    return path


def read_artifact(state_dir: Path, capture_id: str, name: str) -> bytes:
    path = artifact_path(state_dir, capture_id, name)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ScopeError("Artifact exceeds delivery limit")
    return path.read_bytes()


def read_artifact_chunk(state_dir: Path, capture_id: str, name: str, offset=0, size=65536):
    if type(offset) is not int or offset < 0 or type(size) is not int or not 1 <= size <= 262144:
        raise ScopeError("Expected nonnegative offset and size between 1 and 262144")
    path = artifact_path(state_dir, capture_id, name)
    total = path.stat().st_size
    if offset > total: raise ScopeError("Offset past end of artifact")
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(size)
    return {"capture_id": capture_id, "file": name, "offset": offset, "bytes": len(data), "total_bytes": total,
            "next_offset": offset + len(data), "eof": offset + len(data) == total,
            "encoding": "base64", "data": base64.b64encode(data).decode(), "chunk_sha256": hashlib.sha256(data).hexdigest()}
