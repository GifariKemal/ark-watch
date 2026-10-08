"""Write the API's OpenAPI schema (Zonelab generates its client types from it).

Run: python -m arkwatch.server.export_openapi [--out openapi.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .app import app


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch.server.export_openapi")
    p.add_argument("--out", default="openapi.json")
    a = p.parse_args(argv)
    Path(a.out).write_text(json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
