"""Load the editable issue taxonomy and fix-action patterns from YAML."""
import os
import re
from dataclasses import dataclass

import yaml

from .config import ConfigError

DEFAULT_RULES_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "rules.yaml")


@dataclass(frozen=True)
class Rules:
    categories: dict  # name -> [compiled keyword patterns]
    actions: tuple  # ((label, compiled pattern), ...)
    uncategorised: str

    @property
    def names(self):
        return list(self.categories)


def load_rules(path=""):
    path = path or DEFAULT_RULES_FILE
    try:
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read rules file {path}: {exc}") from exc
    problems = []
    categories = raw.get("categories") if isinstance(raw, dict) else None
    if not isinstance(categories, dict) or not categories:
        problems.append("'categories' must be a non-empty mapping of name -> list of keywords")
        categories = {}
    compiled = {}
    for name, words in categories.items():
        if not isinstance(words, list) or not words:
            problems.append(f"category '{name}' needs a non-empty list of keywords")
            continue
        compiled[str(name)] = [re.compile(r"\b" + re.escape(str(w).lower())) for w in words]
    actions = []
    for item in (raw.get("actions") or []) if isinstance(raw, dict) else []:
        try:
            actions.append((str(item["label"]), re.compile(str(item["pattern"]), re.IGNORECASE)))
        except (KeyError, TypeError, re.error) as exc:
            problems.append(f"bad action {item!r}: {exc}")
    if problems:
        raise ConfigError(f"Invalid rules file {path}:\n  - " + "\n  - ".join(problems))
    return Rules(compiled, tuple(actions), str(raw.get("uncategorised") or "Uncategorised"))
