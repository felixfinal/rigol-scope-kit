"""Shared versioned profiles, packaged baselines plus operator-owned JSON files."""
from __future__ import annotations
import hashlib
from importlib.resources import files
import json
from pathlib import Path

from pydantic import ValidationError
from .contracts import Profile
from .errors import ScopeError


def dump_model(model):
    return model.model_dump(mode="json",exclude_none=True)


def digest_profile(profile: dict) -> str:
    return hashlib.sha256(json.dumps(profile,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def profiles(profile_dir: Path | None = None) -> dict:
    roots=[("bundled",Path(str(files("rigol_scope").joinpath("profile_data"))))]
    if profile_dir is not None and profile_dir.exists():roots.append(("operator",profile_dir))
    result={}
    for origin,root in roots:
        for index,path in enumerate(sorted(root.glob("*.json"))):
            if index>=100:raise ScopeError("Too many profile files")
            if path.is_symlink() or path.stat().st_size>65536:raise ScopeError("Profile file must be regular and <=64 KiB")
            try:
                profile=dump_model(Profile.model_validate(json.loads(path.read_text())))
            except (OSError,ValueError,ValidationError) as exc:
                raise ScopeError(f"Invalid profile {path.name}: {exc}") from exc
            pid=profile["profile_id"]
            if pid in result:raise ScopeError(f"Duplicate profile id {pid}; operator profiles must use distinct ids")
            result[pid]={**profile,"digest":digest_profile(profile),"origin":origin}
    return result


def get_profile(profile_dir, profile_id, version=None, digest=None):
    item=profiles(profile_dir).get(profile_id)
    if item is None:raise ScopeError("Unknown profile_id")
    if version is not None and item["version"]!=version:raise ScopeError("PROFILE_VERSION_MISMATCH: fetch the current profile")
    if digest is not None and item["digest"]!=digest:raise ScopeError("PROFILE_DIGEST_MISMATCH: defaults changed; fetch and review the current profile")
    return item


def list_profiles(profile_dir):
    return {"instrument_access":False,"profiles":[{key:profile.get(key) for key in ("profile_id","version","digest","title","description","assumptions","origin","supported_models","allowed_capture_channels")} for profile in profiles(profile_dir).values()]}
