"""Guard standalone boundaries and immutable Masuko fleet files."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Masuko's current authority: physical files in the Git tree and
# canonical fenced sections 8.1 and 8.9 in GUIDELINES.md.
AUTHORITY_SHA = "4bc65765cedd9c14aec196cff382df6dfb318c77"
# Pins that must never come back: the pre-AUD/NZD template.
OBSOLETE_AUTHORITIES = frozenset(
    {"8e4613b36c2808a7de234934a81bb26f7a22d367", "723f8633bbd367ad9cca0a199e84b10fd355da36"}
)
EXPECTED_COUNTRY = "NZD"
STALE_PHRASES = (
    "vocabulary pending",
    "vocabulary awaits",
    "awaits upstream template",
    "pending upstream pr",
    "pending template pr",
    "awaits template pr",
)
# Section 8.1 and 8.9 canonical code blocks are not physical template paths.
GUIDELINES_BLOB = "089fbbca6a2241d3f02777b82631fbf81d49f6e0"
GUIDELINE_SECTION_BLOBS = {
    ".gitignore": "f0d1368264d24d7959d3137d618930a06f33795e",
    "scripts/databricks_engine.py": "73821f7a530ab5cca2f5313180d71c17173e6e59",
}
VERBATIM_BLOBS = {
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


def _tracked() -> list[str]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode()
    return [path for path in output.split("\0") if path]


def test_verbatim_blobs() -> None:
    """Protect Git blob identity of every file copied verbatim from the template."""
    for path, expected in VERBATIM_BLOBS.items():
        data = (ROOT / path).read_bytes()
        assert hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest() == expected, path


def test_verbatim_authority() -> None:
    """Always check local blobs; rederive source blocks when a template is supplied."""
    for path, expected in {**VERBATIM_BLOBS, **GUIDELINE_SECTION_BLOBS}.items():
        data = (ROOT / path).read_bytes()
        assert hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest() == expected, path

    template = os.getenv("MASUKO_TEMPLATE_DIR")
    if template is None:
        return  # Local hashes still gate every test run. Source proof: set the variable.
    for path, expected in VERBATIM_BLOBS.items():
        blob = subprocess.check_output(
            ["git", "-C", template, "rev-parse", f"{AUTHORITY_SHA}:{path}"], text=True
        ).strip()
        assert blob == expected, path
    guideline = subprocess.check_output(
        ["git", "-C", template, "show", f"{AUTHORITY_SHA}:GUIDELINES.md"]
    )
    assert (
        hashlib.sha1(f"blob {len(guideline)}\0".encode() + guideline).hexdigest() == GUIDELINES_BLOB
    )
    text = guideline.decode("utf-8")
    for heading, path in (
        ("### 8.1 `.gitignore`", ".gitignore"),
        ("### 8.9 `scripts/databricks_engine.py`", "scripts/databricks_engine.py"),
    ):
        section = text.split(heading, 1)[1].split("```", 2)[1]
        if path == ".gitignore":
            section = section.removeprefix("\n")
        else:
            section = section.removeprefix("python\n")
        data = section.encode("utf-8")
        assert (
            hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            == GUIDELINE_SECTION_BLOBS[path]
        )


def test_authority_is_current_everywhere() -> None:
    """No tracked file may cite an obsolete pin or call AUD/NZD pending."""
    assert AUTHORITY_SHA not in OBSOLETE_AUTHORITIES
    assert len(AUTHORITY_SHA) == 40 and int(AUTHORITY_SHA, 16) >= 0
    this_file = Path(__file__).resolve().relative_to(ROOT).as_posix()
    for path in _tracked():
        if path == this_file or path in VERBATIM_BLOBS or path in GUIDELINE_SECTION_BLOBS:
            continue
        try:
            text = (ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        for obsolete in OBSOLETE_AUTHORITIES:
            assert obsolete not in text, f"{path} cites obsolete authority {obsolete}"
        lowered = text.lower()
        for phrase in STALE_PHRASES:
            assert phrase not in lowered, f"{path} still says '{phrase}'"


def test_country_uses_the_template_currency_code() -> None:
    from scripts import config

    assert config.COUNTRY_CURRENCY == EXPECTED_COUNTRY


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
    assert not [p for p in _tracked() if Path(p).suffix.lower() in forbidden]
