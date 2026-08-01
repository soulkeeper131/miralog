"""
Mechanical Bulgarian text corrections applied to generated readings.

Only rules that are unambiguous enough to apply without judgement live here.
Anything that needs to know the sentence's meaning (definite article, commas)
is left to the model's own prompt instructions.
"""

import re

# "във" and "със" are the long forms, used only before words starting with the
# same or a closely related consonant. Everywhere else the short form is correct.
_VAV_LONG = re.compile(r"\b(В|в)ъв(\s+)(?![вфВФ])", re.UNICODE)
_SAS_LONG = re.compile(r"\b(С|с)ъс(\s+)(?![сзшжСЗШЖ])", re.UNICODE)

# The mirror case: short form where the long one is required.
_VAV_SHORT = re.compile(r"\b(В|в)(\s+)(?=[вфВФ])", re.UNICODE)
_SAS_SHORT = re.compile(r"\b(С|с)(\s+)(?=[сзшжСЗШЖ])", re.UNICODE)

# Spacing and punctuation tidy-ups.
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?…])")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_MISSING_SPACE_AFTER_PUNCT = re.compile(r"([,;:])(?=[^\s\d])")


def _fix_prepositions(text: str) -> str:
    text = _VAV_LONG.sub(lambda m: f"{m.group(1)}{m.group(2)}", text)
    text = _SAS_LONG.sub(lambda m: f"{m.group(1)}{m.group(2)}", text)
    text = _VAV_SHORT.sub(lambda m: f"{m.group(1)}ъв{m.group(2)}", text)
    text = _SAS_SHORT.sub(lambda m: f"{m.group(1)}ъс{m.group(2)}", text)
    return text


def _fix_punctuation(text: str) -> str:
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _MISSING_SPACE_AFTER_PUNCT.sub(r"\1 ", text)
    text = _MULTI_SPACE.sub(" ", text)
    return text


def clean_bg(text: str) -> str:
    """Apply the mechanical corrections, leaving markdown structure untouched."""
    if not text:
        return text

    out = []
    for line in text.split("\n"):
        # Markdown markers and inline code are left alone; only prose is touched.
        if line.strip().startswith("```"):
            out.append(line)
            continue
        fixed = _fix_prepositions(line)
        fixed = _fix_punctuation(fixed)
        out.append(fixed)
    return "\n".join(out)


def count_issues(text: str) -> dict:
    """Report remaining в/във and с/със problems — used to verify the rules work."""
    if not text:
        return {"vav": 0, "sas": 0}
    return {
        "vav": len(_VAV_LONG.findall(text)) + len(_VAV_SHORT.findall(text)),
        "sas": len(_SAS_LONG.findall(text)) + len(_SAS_SHORT.findall(text)),
    }
