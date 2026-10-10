#!/usr/bin/env python3
"""Check to ensure zero plaintext API key columns exist in models/migrations.

Validates that all API key columns use encrypted storage (`api_key_ciphertext`)
or cryptographic digests (`key_hash`). Plaintext `api_key` column definitions
are prohibited.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCHEMA_FILES = [
    ROOT / "models.py",
    ROOT / "redlab" / "models.py",
] + list((ROOT / "migrations" / "versions").glob("*.py"))

violations: list[str] = []

# Column definitions with plain api_key (not api_key_ciphertext or _hash)
BAD_COL_PATTERNS = [
    re.compile(r'^\s*api_key\s*=\s*(?:db\.)?Column\b', re.M),
    re.compile(r'sa\.Column\(\s*["\']api_key["\']', re.M),
    re.compile(r'Column\(\s*["\']api_key["\']', re.M),
]


def main() -> int:
    for path in SCHEMA_FILES:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for pat in BAD_COL_PATTERNS:
            for match in pat.finditer(text):
                lineno = text.count("\n", 0, match.start()) + 1
                violations.append(
                    f"{path.relative_to(ROOT)}:{lineno}: prohibited "
                    "plaintext 'api_key' column definition."
                )

    if violations:
        print("PLAINTEXT KEY LEAK CHECK FAILED:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print(
        "ALL KEY LEAK CHECKS PASSED: zero plaintext api_key database "
        "columns found."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
