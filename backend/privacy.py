"""Mask obvious personal data before text is sent to an external LLM."""
import re

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_NUMBER = re.compile(r"(?<![\w])\+?\d[\d\s().-]{6,}\d")


def _mask_number(match):
    digits = sum(c.isdigit() for c in match.group())
    if 13 <= digits <= 19:
        return "[card]"
    if digits >= 9:
        return "[phone]"
    return match.group()  # dates, versions and short ids are left alone


def mask_pii(text):
    """Replace e-mail addresses, card-like and phone-like numbers with neutral placeholders."""
    return _NUMBER.sub(_mask_number, _EMAIL.sub("[email]", text))
