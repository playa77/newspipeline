"""Recipient inheritance and effective, isolated interest configurations."""

# version: 0.6.0

from __future__ import annotations

from copy import deepcopy
from typing import Any

INTEREST_DEFAULTS: dict[str, Any] = {
    "start_time": "04:00",
    "interval_hours": 24,
    "max_themes": 5,
    "enable_summary": True,
    "enable_script": False,
    "enable_script_de": False,
    "enable_brief": True,
    # Per-theme result emails. False = the daily brief is this interest's
    # only mail; its themes are still generated and archived either way.
    "email_themes": True,
    "target_summary_words": 600,
    "target_script_words": 1250,
    "target_script_de_words": 1250,
    "target_brief_words": 690,
    "delivery": ["file"],
    "email_recipient": "",
    "input_data_length_mode": "full_article",
    "input_word_count": 256,
    "feeds": [],
    "enabled": True,
    "delivery_schedule": {},
    "summary_detail": "standard",
    "script_detail": "standard",
    "script_de_detail": "standard",
    "brief_detail": "standard",
}

INT_RANGES: dict[str, tuple[int, int]] = {
    "interval_hours": (1, 168),
    "max_themes": (1, 100),
    "target_summary_words": (1, 100_000),
    "target_script_words": (1, 100_000),
    "target_script_de_words": (1, 100_000),
    "target_brief_words": (1, 100_000),
    "input_word_count": (1, 100_000),
}


def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge mappings; lists/scalars (including false/empty) replace."""
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            # Only an empty schedule means immediate delivery. Empty defaults
            # are sparse overrides and continue to inherit their parent.
            result[key] = {} if key == "delivery_schedule" and not value else merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _merge_interests(base: list[dict], override: list[dict]) -> list[dict]:
    result = {i["name"]: deepcopy(i) for i in base}
    for interest in override:
        name = interest["name"]
        result[name] = merge(result.get(name, {}), interest)
    return list(result.values())


def resolve_recipients(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve shared defaults -> parent chain -> recipient, without mutation.

    Recipient ids are immutable scope identifiers, independent of email.
    Named interests merge; feeds replace as a list; enabled=false excludes
    an inherited interest. Missing parents, cycles and duplicate ids/names
    fail before any pipeline is launched or configuration is saved.
    """
    defaults = settings.get("recipient_defaults", {})
    raw = settings.get("recipients", [])
    by_id = {}
    for recipient in raw:
        rid = recipient.get("id")
        if not isinstance(rid, str) or not rid or rid in by_id:
            raise ValueError("recipient ids must be non-empty and unique")
        by_id[rid] = recipient
    resolved = {}
    visiting = set()

    def resolve(rid):
        if rid in resolved:
            return resolved[rid]
        if rid in visiting:
            raise ValueError("recipient inheritance contains a cycle")
        if rid not in by_id:
            raise ValueError(f"unknown parent recipient {rid!r}")
        visiting.add(rid)
        recipient = by_id[rid]
        parent = recipient.get("parent", "")
        base = resolve(parent) if parent else defaults
        result = merge(base, {k: v for k, v in recipient.items() if k != "interests"})
        result["interests"] = _merge_interests(base.get("interests", []), recipient.get("interests", []))
        result["id"] = rid
        visiting.remove(rid)
        resolved[rid] = result
        return result

    for cfg in [defaults, *raw]:
        names = [i.get("name") for i in cfg.get("interests", [])]
        if any(not isinstance(n, str) or not n for n in names) or len(names) != len(set(names)):
            raise ValueError("interest names must be non-empty and unique within a configuration")
    return [deepcopy(resolve(rid)) for rid in by_id]


def effective_interests(settings: dict[str, Any], recipient_id: str | None = None) -> list[dict[str, Any]]:
    """Flat runtime view. Legacy interests retain the empty recipient id."""
    result = []
    if recipient_id in (None, ""):
        result.extend(deepcopy(settings.get("interests", [])))
    for recipient in resolve_recipients(settings):
        rid = recipient["id"]
        if recipient_id is not None and recipient_id != rid:
            continue
        for raw in recipient.get("interests", []):
            interest = merge(INTEREST_DEFAULTS, recipient.get("interest_defaults", {}))
            interest = merge(interest, raw)
            interest["recipient_id"] = rid
            interest["recipient_email"] = recipient.get("email", "")
            if interest.get("enabled", True):
                result.append(interest)
    return result


def interest_key(interest: dict[str, Any]) -> str | tuple[str, str]:
    """Legacy keys stay strings; recipient keys cannot collide with names."""
    rid = interest.get("recipient_id", "")
    return (rid, interest["name"]) if rid else interest["name"]
