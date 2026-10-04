"""Release-gate tests: the skill bundle validates without repository docs."""

from __future__ import annotations

from tools import validate_release


def test_release_validation_passes():
    assert validate_release.main() == 0
