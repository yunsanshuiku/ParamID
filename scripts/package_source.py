"""Create a small source archive without local experiments or runtimes.

Only known source directories and file extensions are included. This also works
before git init, so an accidental git add cannot change archive contents.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import stat
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOP_FILES = {
    ".gitignore", ".gitattributes", ".editorconfig", "README.md", "LICENSE",
    "THIRD_PARTY_NOTICES.md", "CONTRIBUTING.md", "CHANGELOG.md",
    "app.py", "desktop.py", "desktop.spec", "启动软件.cmd",
    "requirements.txt", "requirements-desktop.txt", "requirements-tested.txt",
}
SOURCE_TYPES = {
    ".github": {".yml", ".yaml", ".md"},
    "paramid": {".py"}, "tests": {".py"},
    "web": {".html", ".css", ".js"},
    "examples": {".csv", ".json"},
    "scripts": {".py", ".mjs", ".cmd", ".ps1"},
    "installer": {".iss", ".ico", ".txt"},
    "docs": {".md", ".png"},
}
MAX_FILE_BYTES = 5 * 1024 * 1024


def source_files(root: Path) -> list[Path]:
    """Collect regular source files; reject links and oversized additions."""
    selected = [root / name for name in TOP_FILES]
    for directory, suffixes in SOURCE_TYPES.items():
        base = root / directory
        if not base.is_dir():
            raise ValueError(f"Required source directory is missing: {base}")
        for path in base.rglob("*"):
            if "__pycache__" in path.relative_to(root).parts:
                continue
            if path.suffix.lower() in suffixes and path.is_file():
                selected.append(path)
    for path in selected:
        if not path.is_file():
            raise ValueError(f"Required source file is missing: {path}")
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Source path leaves the project: {path}")
        # Check ancestors too; on Windows junctions use the reparse attribute.
        for candidate in (path, *path.parents):
            if candidate == root:
                break
            attributes = getattr(candidate.lstat(), "st_file_attributes", 0)
            if candidate.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise ValueError(f"Source links are not allowed: {candidate}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"Source file exceeds 5 MiB; review before publishing: {path}")
    return sorted(selected, key=lambda path: path.relative_to(root).as_posix())


def main() -> None:
    """Write one ParamID/ archive root and verify all CRC checksums."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "release" / "ParamID-source.zip")
    args = parser.parse_args()
    files = source_files(ROOT)
    output = args.output.expanduser().resolve()
    if output.suffix.lower() != ".zip" or output in files:
        parser.error("--output must name a .zip file outside the source file list")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            archive.write(path, "ParamID/" + path.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(output) as archive:
        bad_file = archive.testzip()
        if bad_file:
            raise RuntimeError(f"Archive checksum failed: {bad_file}")
    size = sum(path.stat().st_size for path in files)
    print(f"{len(files)} source files; {size:,} bytes -> {output.stat().st_size:,} bytes")
    print(output)


if __name__ == "__main__":
    main()
