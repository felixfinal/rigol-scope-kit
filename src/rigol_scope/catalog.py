"""Bounded on-demand index over immutable capture metadata; no daemon/database."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import math
from itertools import islice
import os
from pathlib import Path
import re
import time

from .artifacts import artifact_path, capture_directory, SOURCE_NAME, DERIVED_NAME
from .errors import ScopeError

MAX_METADATA = 8 * 1024 * 1024


def load_metadata(state_dir: Path, capture_id: str) -> dict:
    path = artifact_path(state_dir, capture_id, "metadata.json")
    if path.stat().st_size > MAX_METADATA:
        raise ScopeError("Capture metadata exceeds 8 MiB limit")
    try:
        with path.open("rb") as stream:
            raw=stream.read(MAX_METADATA+1)
        if len(raw)>MAX_METADATA:raise ValueError("metadata limit exceeded")
        result = json.loads(raw)
        if result.get("capture_id") != capture_id or result.get("kind") not in {"waveform", "screenshot"}:
            raise ValueError("identity/kind mismatch")
        if result.get("status") not in {"complete", "partial"}:
            raise ValueError("unknown capture status")
        result["_metadata_sha256"]=hashlib.sha256(raw).hexdigest()
        return result
    except (ValueError, AttributeError) as exc:
        raise ScopeError(f"Invalid capture metadata: {exc}") from exc


def summary(meta: dict) -> dict:
    return {key: meta.get(key) for key in ("capture_id", "created_at", "kind", "status", "identity")} | {
        "channels": sorted(meta.get("channels", {})),
        "points": {key: value.get("points_written") for key, value in meta.get("channels", {}).items()},
        "metadata_uri": f"scope://files/{meta['capture_id']}/metadata.json",
    }


def timestamp(value):
    if value is None:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.timestamp()
    except (ValueError, TypeError, AttributeError) as exc:
        raise ScopeError("Expected ISO date/time") from exc


def list_captures(state_dir: Path, limit=20, cursor=None, serial=None, channel=None,
                  kind=None, status="complete", created_after=None, created_before=None) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ScopeError("limit must be 1..100")
    if channel is not None and (type(channel) is not int or not 1 <= channel <= 4):
        raise ScopeError("channel must be 1..4")
    if kind not in {None, "waveform", "screenshot"} or status not in {None, "complete", "partial"}:
        raise ScopeError("Unsupported kind/status filter")
    lower, upper = timestamp(created_after), timestamp(created_before)
    if lower is not None and upper is not None and lower > upper:
        raise ScopeError("created_after exceeds created_before")
    filters = [serial, channel, kind, status, lower, upper]
    fingerprint = hashlib.sha256(json.dumps(filters).encode()).hexdigest()
    snapshot = datetime.now(timezone.utc).timestamp()
    last = None
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 2048: raise ValueError()
            decoded = json.loads(base64.urlsafe_b64decode(cursor))
            if decoded["filters"] != fingerprint: raise ValueError()
            snapshot = float(decoded["snapshot"])
            last = (float(decoded["last"][0]), str(decoded["last"][1]))
            if not math.isfinite(snapshot) or not math.isfinite(last[0]): raise ValueError()
        except (ValueError, TypeError, KeyError) as exc:
            raise ScopeError("Invalid cursor or changed filters") from exc
    records, skipped, examined = [], 0, 0
    root = state_dir / "captures"
    deadline = time.monotonic() + 10
    if root.exists():
        with os.scandir(root) as entries:
            for entry in entries:
                examined += 1
                if examined > 10000 or time.monotonic() > deadline:
                    raise ScopeError("Catalog scan exceeded 10,000 entries/10 seconds; no truncated result returned")
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False) or not re.fullmatch(r"[0-9a-f]{32}", entry.name):
                    skipped += 1
                    continue
                try:
                    meta = load_metadata(state_dir, entry.name)
                    created = timestamp(meta.get("created_at"))
                    if created is None: raise ScopeError("Missing creation timestamp")
                    item = summary(meta)
                except (ScopeError, OSError, TypeError, AttributeError):
                    skipped += 1
                    continue
                key = (created, entry.name)
                if created > snapshot or (last is not None and key >= last): continue
                if lower is not None and created < lower: continue
                if upper is not None and created > upper: continue
                if kind is not None and meta["kind"] != kind: continue
                if status is not None and meta["status"] != status: continue
                if serial is not None and meta.get("identity", {}).get("serial") != serial: continue
                if channel is not None and str(channel) not in meta.get("channels", {}): continue
                records.append((key, item))
    records.sort(key=lambda pair: pair[0], reverse=True)
    page = records[:limit]
    next_cursor = None
    if len(records) > limit:
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            {"filters": fingerprint, "snapshot": snapshot, "last": page[-1][0]}).encode()).decode()
    return {"captures": [item for _, item in page], "next_cursor": next_cursor,
            "examined_entries": examined, "skipped_invalid_or_incomplete_entries": skipped,
            "instrument_access": False, "index": "bounded on-demand metadata scan"}


def capture_info(state_dir: Path, capture_id: str) -> dict:
    meta = load_metadata(state_dir, capture_id)
    directory = capture_directory(state_dir, capture_id)
    files = []
    paths=list(islice(directory.iterdir(),2001))
    if len(paths)>2000: raise ScopeError("Too many artifacts in this capture")
    for path in sorted(paths):
        if not re.fullmatch(SOURCE_NAME + "|" + DERIVED_NAME, path.name): continue
        try:
            safe = artifact_path(state_dir, capture_id, path.name)
        except ScopeError:
            continue
        expected = None
        if path.name == "screen.png": expected = meta.get("sha256")
        if re.fullmatch(r"ch[1-4]\.bin", path.name):
            expected = meta.get("channels", {}).get(path.name[2], {}).get("sha256")
        files.append({"name": path.name, "bytes": safe.stat().st_size, "expected_sha256": expected,
                      "uri": f"scope://files/{capture_id}/{path.name}"})
    return {**summary(meta), "artifacts": files, "instrument_access": False,
            "hash_note": "Expected hashes are metadata claims; analysis verifies source bytes before use."}
