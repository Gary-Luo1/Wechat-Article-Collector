from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "wechat-article-subscriber" / "scripts"
sys.path.insert(0, str(SCRIPTS))
