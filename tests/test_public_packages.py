"""Release checks must reject extra private files and changed runtime source."""
import contextlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import zipfile

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from verify_public import verify_packages
sys.path.pop(0)

VERSION = "0.6.1"
SOURCES = {
    "uv.lock": b"fixture lock",
    "pyproject.toml": b"fixture project",
    "README.md": b"fixture documentation",
    "LICENSE": b"Apache License",
    "NOTICE": b"upstream attribution",
    "src/rigol_scope/__init__.py": b'__version__ = "0.6.1"\n',
    "src/rigol_scope/profile_data/passive-10x.json": b"{}",
    "skills/rigol-oscilloscope/SKILL.md": b"fixture skill",
}


def packages(root, *, extra_sdist=False, changed_wheel=False):
    info = f"rigol_scope_kit-{VERSION}.dist-info/"
    with zipfile.ZipFile(root / f"rigol_scope_kit-{VERSION}-py3-none-any.whl", "w") as archive:
        for name, data in SOURCES.items():
            if name.startswith("src/"):
                archive.writestr(name.removeprefix("src/"), b"changed runtime" if changed_wheel else data)
            elif name.startswith("skills/"):
                archive.writestr("rigol_scope/_bundled_skill/" + name.removeprefix("skills/"), data)
        for name in ("LICENSE", "NOTICE"):
            archive.writestr(info + "licenses/" + name, SOURCES[name])
        archive.writestr(info + "METADATA", f"Metadata-Version: 2.4\nVersion: {VERSION}\nLicense-Expression: Apache-2.0\n")
        for name in ("WHEEL", "entry_points.txt", "RECORD"):
            archive.writestr(info + name, b"fixture")
    contents = {**SOURCES, "PKG-INFO": b"fixture metadata"}
    if extra_sdist:
        contents["docs/.env"] = b"private configuration must not be published"
    with tarfile.open(root / f"rigol_scope_kit-{VERSION}.tar.gz", "w:gz") as archive:
        for name, data in contents.items():
            member = tarfile.TarInfo(f"rigol_scope_kit-{VERSION}/{name}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


class PublicPackageTests(unittest.TestCase):
    def test_clean_fixture_passes(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            packages(root)
            verify_packages(root, VERSION, SOURCES)

    def test_private_file_under_allowed_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packages(root, extra_sdist=True)
            with self.assertRaisesRegex(ValueError, "Unexpected sdist content"):
                verify_packages(root, VERSION, SOURCES)

    def test_modified_runtime_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packages(root, changed_wheel=True)
            with self.assertRaisesRegex(ValueError, "Wheel source mismatch"):
                verify_packages(root, VERSION, SOURCES)
