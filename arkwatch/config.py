"""config.py — loads the seed config/ files (registry, anchors, calendar, params, COT)."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

CONFIG_DIR = Path(
    os.environ.get("ARKWATCH_CONFIG", Path(__file__).resolve().parent.parent / "config")
)

REGISTRY_FILES = ["series_registry.yaml", "series_registry_blocks_d-f.yaml"]

# Optional paid/credentialed providers by registry prefix: unset env = SKIPPED
# (unconfigured), never ERROR. Set-but-failing stays ERROR.
PROVIDER_ENV = {"EODHD:": ("EODHD_API_TOKEN",)}


def missing_env(*names: str) -> str | None:
    """'unconfigured: A, B' for unset/blank env vars, None when all are set."""
    miss = [n for n in names if not os.environ.get(n, "").strip()]
    return f"unconfigured: {', '.join(miss)}" if miss else None


# any one of these selects an NLP provider (see fetchers/nlp._config)
NLP_KEYS = ("NLP_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def nlp_missing() -> str | None:
    """'unconfigured: NLP_API_KEY' only when NO provider key is set."""
    if any(os.environ.get(k, "").strip() for k in NLP_KEYS):
        return None
    return "unconfigured: NLP_API_KEY"


class PlanLimited(RuntimeError):
    """HTTP 402: endpoint outside the subscribed plan -> SKIPPED, not ERROR."""


def _load_yaml(name: str) -> dict | list:
    # An empty or comment-only file safe_loads to None → an opaque
    # AttributeError in the caller; fail loudly with the file name instead
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        raise ValueError(f"{name}: empty or comments only")
    return data


def load_registry(*, active_only: bool = True) -> list[dict]:
    """Merge both registry files into one list; reject duplicate series_ids."""
    entries: list[dict] = []
    seen: set[str] = set()
    for fname in REGISTRY_FILES:
        data = _load_yaml(fname)
        rows = data if isinstance(data, list) else data.get("series", data.get("entries", []))
        if not isinstance(rows, list):
            raise ValueError(f"{fname}: unrecognized structure (expected list/series)")
        for row in rows:
            sid = row.get("series_id")
            if not sid:
                raise ValueError(f"{fname}: entry without series_id")
            if sid in seen:
                raise ValueError(f"duplicate series_id across files: {sid}")
            seen.add(sid)
            entries.append(row)
    if active_only:
        entries = [e for e in entries if e.get("active", 1)]
    return entries


def load_anchors() -> list[dict]:
    data = _load_yaml("golden_anchors.yaml")
    if isinstance(data, list):
        return data
    if "anchors" in data:
        return data["anchors"]
    if "golden_anchors" in data:  # actual seed format: per-key dict {golden_anchors: [...]}
        v = data["golden_anchors"]
        return v if isinstance(v, list) else [row for rows in v.values() for row in rows]
    return [row for rows in data.values() if isinstance(rows, list) for row in rows]


def load_curated_calendar() -> dict:
    return _load_yaml("curated_calendar.yaml")


def load_params_block_c() -> dict:
    return _load_yaml("params_block_c.yaml")


def load_params_signals() -> dict:
    return _load_yaml("params_signals.yaml")


def load_cot_contracts() -> list[dict]:
    data = _load_yaml("cot_contracts.yaml")
    return data if isinstance(data, list) else data.get("contracts", [])
