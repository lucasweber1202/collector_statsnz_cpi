"""Guard standalone boundaries and immutable Masuko fleet files."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_SHA = "8e4613b36c2808a7de234934a81bb26f7a22d367"
VERBATIM_BLOBS = {
    ".gitignore": "ed2243b8bd19cd8ce7155e1479d09a194a846709",
    "scripts/databricks_engine.py": "73821f7a530ab5cca2f5313180d71c17173e6e59",
    ".vscode/launch.json": "430b80db4450110af11567b260fa4658f12c1f62",
    ".vscode/settings.json": "0facdc9526ed15fae0c85ec6434d9248c73ab84f",
    ".github/copilot-instructions.md": "d58921719c9cf0fd8cf442f46d1b9b21113d28a0",
    ".github/prompts/onboard-new-api.prompt.md": "979cae7629beaa112ba8653c4ce13510779ae80e",
    ".github/skills/build-collector/SKILL.md": "a6fcf090c72422b299d8e45eec99e4a4db7b18fd",
    ".github/skills/test-driven-development/SKILL.md": "19a8e3ac7937ac5799c7c9ef4ffa94d4fdbe6f3e",
    ".github/skills/security-review/SKILL.md": "5857739acddfd1f1bb8b6f9fe5272a9d592c3a44",
    ".github/skills/verification-loop/SKILL.md": "53827cbc812b1105abf356d633d4e3e5329d2c2f",
    ".github/skills/get-api-docs/SKILL.md": "d2834b3de2fab7ed853066af934777a8707e43d7",
    ".github/skills/audit-collector/SKILL.md": "321f4f945e500c8d2ce9702aceb64d203bcc8f69",
    ".github/skills/systematic-debugging/SKILL.md": "df313cb3d591ecf742d58ee0f1dfa15f6e35974d",
    ".github/skills/research-first/SKILL.md": "c8a9d4f6492b3f82dfc6ea8f449ba58f2f765b48",
    ".github/skills/planning-and-design/SKILL.md": "0a8f47fa2ee3fe6c8b09662fee0132d9fb98d865",
    ".github/skills/series-selection/SKILL.md": "b58860245f6fbb6af123bf8587224b7f2ace18de",
    ".github/skills/code-review/SKILL.md": "292c6ff9c74ac0d3cf88d60d855deed6fa5fe36f",
}


def test_verbatim_blobs() -> None:
    """Protect Git blob identity against the pinned template commit."""
    for path, expected in VERBATIM_BLOBS.items():
        data = (ROOT / path).read_bytes()
        assert hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest() == expected, path


def test_standalone_and_no_raw() -> None:
    """Reject sibling imports and committed source payloads."""
    for path in [ROOT / "main.py", *(ROOT / "scripts").glob("*.py")]:
        source = path.read_text()
        assert "from collector_" not in source
        assert "import collector_" not in source
        assert "../collector_" not in source
        assert "..\\collector_" not in source
        assert "sys.path" not in source
    forbidden = {".xlsx", ".xls", ".ods", ".csv", ".parquet"}
    assert not [p for p in ROOT.rglob("*") if p.is_file() and p.suffix.lower() in forbidden]
