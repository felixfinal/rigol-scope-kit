"""Export an allowlisted source tree into a new directory; never copy bench state."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "pyproject.toml", "uv.lock", ".python-version", ".gitignore",
    "README.md", "README.zh-CN.md", "LICENSE", "NOTICE",
    "CHANGELOG.md", "CONTRIBUTING.md",
    "scripts/check_readonly.py", "scripts/check_mcp_readonly.py",
}
TREES = {"src", "tests", "skills", "docs", "examples", "tools", ".github"}


def public_files(root: Path):
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in {"__pycache__", ".venv", ".git"} for part in relative.parts):
            continue
        if not path.is_file():
            continue
        name = relative.as_posix()
        if (name in FILES or relative.parts[0] in TREES or
                (relative.parts[0] == "config" and len(relative.parts) == 2
                 and ".example." in path.name)):
            if path.is_symlink():
                raise ValueError(f"Refusing symlink: {relative}")
            if path.name.startswith(".env") or path.name in {"id_rsa", "id_ed25519", "credentials.json", "secrets.toml", "devices.toml"}:
                raise ValueError(f"Refusing private configuration: {relative}")
            if path.suffix in {".pyc", ".pyo", ".bin", ".png", ".csv", ".pdf", ".key", ".pem"}:
                raise ValueError(f"Unexpected public artifact: {relative}")
            yield path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    target = args.destination.resolve()
    if target == ROOT or ROOT in target.parents:
        parser.error("Export destination must be outside the source tree")
    sources = list(public_files(ROOT))
    target.mkdir(parents=True, exist_ok=False)
    for source in sources:
        destination = target / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    print(f"Exported {len(sources)} public files to {target}")


if __name__ == "__main__":
    main()
