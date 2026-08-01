from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile


def test_github_source_archive_is_clean_and_self_verifying(tmp_path: Path):
    from tools.package_github_source import build

    archive = build(tmp_path)
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert any(name.endswith("/.github/workflows/test.yml") for name in names)
        assert any(name.endswith("/tests/test_core.py") for name in names)
        assert any(name.endswith("/skills/wechat-article-subscriber/SKILL.md") for name in names)
        assert any(name.endswith("/SOURCE-MANIFEST.sha256") for name in names)
        assert not any(
            forbidden in name
            for name in names
            for forbidden in ("__pycache__", ".pytest_cache", "/dist/", "/.git/")
        )
        manifest_name = next(name for name in names if name.endswith("/SOURCE-MANIFEST.sha256"))
        prefix = manifest_name.removesuffix("SOURCE-MANIFEST.sha256")
        manifest = bundle.read(manifest_name).decode("ascii").splitlines()
        for line in manifest:
            expected, relative = line.split("  ", 1)
            assert hashlib.sha256(bundle.read(prefix + relative)).hexdigest() == expected


def test_portable_release_archive_has_a_checksum(tmp_path: Path):
    from tools.package_release import build

    archive, checksum = build(tmp_path)

    assert archive.is_file()
    assert checksum.read_text(encoding="ascii").endswith(f"  {archive.name}\n")
