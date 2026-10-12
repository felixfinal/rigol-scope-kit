"""Skill discovery/export pattern adapted from scopebench-mcp 0.3.1 (Apache-2.0)."""
from importlib.resources import files
from pathlib import Path
import argparse
import shutil


def skill_root() -> Path:
    bundled = Path(str(files("rigol_scope").joinpath("_bundled_skill/rigol-oscilloscope")))
    if bundled.is_dir():
        return bundled
    source = Path(__file__).resolve().parents[2] / "skills/rigol-oscilloscope"
    if source.is_dir():
        return source
    raise FileNotFoundError("Bundled Skill missing")


def main():
    parser = argparse.ArgumentParser(description="Export the bundled Skill without overwriting an existing directory")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    shutil.copytree(skill_root(), args.destination)
    print(args.destination.resolve())
