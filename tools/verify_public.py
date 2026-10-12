"""Check publication boundaries, relative links and built package contents."""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import json
from pathlib import Path
import re
import tarfile
import tomllib
import zipfile

from export_public import FILES, ROOT, public_files


def check(condition, message):
    if not condition:
        raise ValueError(message)


def verify_source(strict=False):
    paths = list(public_files(ROOT))
    names = {p.relative_to(ROOT).as_posix() for p in paths}
    check(FILES <= names, f"Missing public files: {FILES - names}")
    private = ["/home/" + "felix", "claw" + "_pi", "MHO9" + "B"]
    for path in paths:
        content = path.read_text(encoding="utf-8")
        check(not any(marker in content for marker in private), f"Private identifier in {path}")
        check(not re.search(r"\b(?:192\.168\.|10\.(?:\d{1,3}\.){2})\d", content), f"Private IPv4 in {path}")
        check("-----BEGIN " + "PRIVATE KEY-----" not in content, f"Private key in {path}")
        if path.suffix == ".md":
            for link in re.findall(r"\]\(([^)]+)\)", content):
                target = link.strip().split("#", 1)[0]
                if not target or ":" in target or target.startswith("/"):
                    continue
                check((path.parent / target).exists(), f"Broken link in {path}: {target}")
    if strict:
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT)
            if any(part in {".git", ".venv", "__pycache__", "dist", "build"} for part in relative.parts):
                continue
            if path.is_file():
                check(relative.as_posix() in names, f"Unpublished file in clean tree: {relative}")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = project["version"]
    check(f'__version__ = "{version}"' in (ROOT / "src/rigol_scope/__init__.py").read_text(), "Version mismatch")
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    package = next(p for p in lock["package"] if p["name"] == project["name"])
    check(package["version"] == version, "Lock version mismatch")
    for path in ROOT.glob("config/*.example.json"):
        json.loads(path.read_text())
    print(f"Public source: {len(paths)} files, version {version}, links/privacy checks passed")
    return version


def verify_packages(directory, version, source_files=None):
    sources_by_name = (source_files if source_files is not None else
                       {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in public_files(ROOT)})
    wheels = list(directory.glob(f"rigol_scope_kit-{version}-*.whl"))
    sources = list(directory.glob(f"rigol_scope_kit-{version}.tar.gz"))
    check(len(wheels) == len(sources) == 1, "Expected one wheel and one sdist for this version")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        check(len(names) == len(archive.namelist()), "Wheel contains duplicate member names")
        expected = {}
        for name, data in sources_by_name.items():
            if name.startswith("src/rigol_scope/"):
                expected[name.removeprefix("src/")] = data
            elif name.startswith("skills/rigol-oscilloscope/"):
                expected["rigol_scope/_bundled_skill/" + name.removeprefix("skills/")] = data
        info = f"rigol_scope_kit-{version}.dist-info/"
        for name in ("LICENSE", "NOTICE"):
            expected[info + "licenses/" + name] = sources_by_name[name]
        generated = {info + name for name in ("METADATA", "WHEEL", "entry_points.txt", "RECORD")}
        check(names == set(expected) | generated, "Wheel contents differ from public runtime source")
        for name, data in expected.items():
            check(archive.read(name) == data, f"Wheel source mismatch: {name}")
        check("rigol_scope/_bundled_skill/rigol-oscilloscope/SKILL.md" in names, "Wheel has no Skill")
        check("rigol_scope/profile_data/passive-10x.json" in names, "Wheel has no default profile")
        metadata = next(n for n in names if n.endswith(".dist-info/METADATA"))
        parsed = BytesParser().parsebytes(archive.read(metadata))
        check(parsed["Version"] == version and parsed["License-Expression"] == "Apache-2.0", "Wrong package metadata")
        for name in ("LICENSE", "NOTICE"):
            check(any(n.endswith("/licenses/" + name) for n in names), f"Wheel missing {name}")
        check(not any("__pycache__" in n or n.startswith(("tests/", "scripts/", "config/")) for n in names), "Wheel contains non-runtime files")
    with tarfile.open(sources[0]) as archive:
        names = {"/".join(Path(n).parts[1:]) for n in archive.getnames()}
        required = {"uv.lock", "pyproject.toml", "README.md", "LICENSE", "NOTICE", "skills/rigol-oscilloscope/SKILL.md"}
        check(required <= names, f"Sdist missing {required - names}")
        seen = set()
        for member in archive.getmembers():
            parts = Path(member.name).parts
            check(not Path(member.name).is_absolute() and ".." not in parts and
                  parts[0] == f"rigol_scope_kit-{version}", "Unsafe sdist member path")
            check(not member.issym() and not member.islnk(), "Sdist contains a link")
            if not member.isfile():
                continue
            name = "/".join(Path(member.name).parts[1:])
            check(name not in seen, f"Duplicate sdist member: {name}")
            seen.add(name)
            check(name in sources_by_name or name == "PKG-INFO", f"Unexpected sdist content: {name}")
            if name != "PKG-INFO":
                check(archive.extractfile(member).read() == sources_by_name[name], f"Sdist source mismatch: {name}")
        check(set(sources_by_name) <= seen, "Sdist is missing public source files")
    print("Wheel and sdist: Skill/profile/license/public boundaries passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path)
    parser.add_argument("--strict", action="store_true", help="Reject unpublished files in a clean export")
    args = parser.parse_args()
    version = verify_source(args.strict)
    if args.dist:
        verify_packages(args.dist, version)


if __name__ == "__main__":
    main()
