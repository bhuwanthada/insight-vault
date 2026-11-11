"""LLM prompts. Defaults live here; any of them can be replaced by a text file in PROMPT_DIR.

A file is named after the prompt (classify.txt, insights.txt, summary.txt). Files may use $variables
(for example $categories); unknown variables are left untouched.
"""
import os
from string import Template

DEFAULTS = {
    "classify": (
        "You classify customer-support tickets. Reply with JSON only: "
        '{"category": "<one of the allowed categories>"}. Allowed categories: $categories.'
    ),
    "insights": (
        "You are a research analyst. Group the customer quotes into distinct insights. "
        'Reply with JSON only: {"insights": [{"title": str, "description": str, "highlightIds": [str]}]}. '
        "Only use highlight ids you were given. Every insight needs at least one id."
    ),
    "summary": (
        "You write executive summaries for support leadership. Reply with JSON only: "
        '{"summary": "<2-3 sentences: main issue types, how agents fixed them, anything notable>"}. '
        "Use only the numbers provided."
    ),
}
FREE_TEXT = {"insights", "summary"}  # prompts whose output is prose, so the output language applies


class Prompts:
    def __init__(self, prompt_dir="", language="English"):
        self.language = language
        self.texts = dict(DEFAULTS)
        if prompt_dir:
            for name in DEFAULTS:
                path = os.path.join(prompt_dir, name + ".txt")
                if os.path.isfile(path):
                    with open(path, encoding="utf-8") as handle:
                        self.texts[name] = handle.read().strip()

    def get(self, name, **variables):
        text = Template(self.texts[name]).safe_substitute(**variables)
        if name in FREE_TEXT:
            text += f" Write all free-text fields in {self.language}."
        return text
