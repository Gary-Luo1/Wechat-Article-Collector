"""Build a portable, allowlisted GitHub release archive."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "wechat-article-subscriber"
TOP_FILES = (
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "install.sh",
    "install.ps1",
)
SKILL_SUFFIXES = {".md", ".txt", ".py", ".sh", ".ps1", ".yaml", ".yml"}


def release_files() -> list[Path]:
    files = [ROOT / name for name in TOP_FILES]
    files.append(ROOT / ".codex-plugin" / "plugin.json")
    for adapter_root in (ROOT / ".agents", ROOT / ".claude", ROOT / ".github" / "skills"):
        files.extend(adapter_root.rglob("SKILL.md"))
    files.extend(
        path
        for path in SKILL.rglob("*")
        if path.is_file()
        and path.suffix.lower() in SKILL_SUFFIXES
        and "__pycache__" not in path.parts
        and ".pytest_cache" not in path.parts
    )
    missing = [path for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"required release files are missing: {missing}")
    return sorted(set(files), key=lambda path: path.relative_to(ROOT).as_posix())


def build(output_dir: Path) -> tuple[Path, Path]:
    manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"wechat-article-subscriber-{version}.zip"
    prefix = f"wechat-article-subscriber-{version}"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in release_files():
            relative = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(f"{prefix}/{relative}", date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if path.suffix == ".sh" else 0o644) << 16
            bundle.writestr(info, path.read_bytes(), compresslevel=9)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum_path = archive.with_suffix(".zip.sha256")
    checksum_path.write_bytes(f"{checksum}  {archive.name}\n".encode("ascii"))
    return archive, checksum_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    arguments = parser.parse_args()
    archive, checksum = build(arguments.output)
    print(archive)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
