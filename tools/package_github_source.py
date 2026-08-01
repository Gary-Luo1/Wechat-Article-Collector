"""Build one clean GitHub-ready source ZIP with an internal SHA-256 manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
TOP_FILES = (
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "install.sh",
    "install.ps1",
    "requirements-dev.txt",
)
SOURCE_ROOTS = (
    ".agents",
    ".claude",
    ".codex-plugin",
    ".github",
    "skills",
    "tests",
    "tools",
)
ALLOWED_SUFFIXES = {
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".txt",
    ".yaml",
    ".yml",
}
FORBIDDEN_PARTS = {
    ".git",
    ".idea",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
    "venv",
}
FORBIDDEN_NAMES = {
    ".env",
    "config.json",
    "queue.json",
    "queue.lock",
}


def _is_allowed(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if any(part in FORBIDDEN_PARTS for part in relative.parts):
        return False
    if path.name in FORBIDDEN_NAMES or path.name.endswith((".pyc", ".pyo")):
        return False
    if path.name.startswith(".agent-config-") or path.name.startswith("queue.corrupt."):
        return False
    return path.suffix.lower() in ALLOWED_SUFFIXES or path.name in TOP_FILES


def source_files() -> list[Path]:
    files = [ROOT / name for name in TOP_FILES]
    for name in SOURCE_ROOTS:
        source_root = ROOT / name
        if not source_root.is_dir():
            raise FileNotFoundError(f"required source directory is missing: {source_root}")
        files.extend(path for path in source_root.rglob("*") if path.is_file() and _is_allowed(path))
    missing = [path for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"required source files are missing: {missing}")
    selected = sorted(set(files), key=lambda path: path.relative_to(ROOT).as_posix())
    forbidden = [path for path in selected if not _is_allowed(path)]
    if forbidden:
        raise ValueError(f"forbidden files selected for packaging: {forbidden}")
    return selected


def build(output_dir: Path) -> Path:
    plugin = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    version = str(plugin["version"])
    prefix = f"wechat-article-subscriber-{version}"
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"wechat-article-subscriber-{version}-github-source.zip"
    files = source_files()
    manifest_lines: list[str] = []
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            payload = path.read_bytes()
            manifest_lines.append(f"{hashlib.sha256(payload).hexdigest()}  {relative}")
            info = zipfile.ZipInfo(f"{prefix}/{relative}", date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if path.suffix == ".sh" else 0o644) << 16
            bundle.writestr(info, payload, compresslevel=9)
        manifest = ("\n".join(manifest_lines) + "\n").encode("ascii")
        info = zipfile.ZipInfo(
            f"{prefix}/SOURCE-MANIFEST.sha256",
            date_time=(2020, 1, 1, 0, 0, 0),
        )
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        bundle.writestr(info, manifest, compresslevel=9)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    arguments = parser.parse_args()
    print(build(arguments.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
