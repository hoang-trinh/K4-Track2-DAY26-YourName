"""Deterministic answer checks. These checks do not claim semantic entailment.

Only exchange evidence can ground citations or numeric precision. Injection
scanning is a conservative heuristic; retrieved content never grants authority.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Iterable, Mapping
from kit.world.anchor import Anchor, AnchorSyntaxError


@dataclass(frozen=True, slots=True)
class GroundingResult:
    grounded: bool
    cited: tuple[str, ...]
    ungrounded: tuple[str, ...]
    malformed: tuple[str, ...]


def check_grounding(answer: Mapping[str, Any], retrieved_anchors: Iterable[str], *, require_citation: bool = True) -> GroundingResult:
    raw = answer.get("cited_anchors", ())
    if not isinstance(raw, (list, tuple)):
        return GroundingResult(False, (), (), (str(raw),))
    cited = tuple(raw)
    retrieved = frozenset(retrieved_anchors)
    malformed, ungrounded = [], []
    for value in cited:
        if not isinstance(value, str):
            malformed.append(str(value))
            continue
        try:
            Anchor.parse(value)
        except AnchorSyntaxError:
            malformed.append(value)
            continue
        if value not in retrieved:
            ungrounded.append(value)
    return GroundingResult(not malformed and not ungrounded and (bool(cited) or not require_citation),
                           cited, tuple(ungrounded), tuple(malformed))


@dataclass(frozen=True, slots=True)
class InjectionScanResult:
    suspicious: bool
    matched_patterns: tuple[str, ...]


_INJECTION_PATTERNS = {
    "instruction_override": r"\bignore\s+(?:all\s+)?(?:your\s+)?(?:previous|prior|system)\s+(?:instructions?|prompts?)\b",
    "system_impersonation": r"system\s+override|\[system\]|<system>|(?:^|\n)\s*system\s*:",
    "secret_disclosure": r"\b(?:reveal|dump|exfiltrate)\b.{0,80}\b(?:private|secret|password|ctx\.act|ctx\.scopes)\b",
    "cross_learner_write": r"\b(?:also\s+)?record\b.{0,80}\b(?:for|to)\s+learner:",
    "vietnamese_override": r"bỏ\s+qua.{0,40}(?:chỉ\s+dẫn|hướng\s+dẫn|chỉ\s+thị|lệnh).{0,30}(?:trước|hệ\s+thống)",
    "vietnamese_disclosure": r"(?:tiết\s+lộ|xuất\s+toàn\s+bộ).{0,60}(?:bí\s+mật|riêng\s+tư|mật\s+khẩu)",
}


def scan_for_injected_instructions(text: str) -> InjectionScanResult:
    hits = tuple(name for name, pattern in _INJECTION_PATTERNS.items() if re.search(pattern, text, re.I))
    return InjectionScanResult(bool(hits), hits)


@dataclass(frozen=True, slots=True)
class RedactionResult:
    redacted_text: str
    hits: tuple[str, ...]


def redact(text: str, *, private_values: Iterable[str] = ()) -> RedactionResult:
    """Remove known private fragments as well as labelled secrets and PII.

    A regex cannot identify arbitrary private prose: supply private_values
    from the trusted evidence's private fields to remove those verbatim too.
    """
    hits = []
    for value in sorted({v for v in private_values if isinstance(v, str) and v}, key=lambda v: (-len(v), v)):
        if value in text:
            text = text.replace(value, "[REDACTED]")
            hits.append("private_value")
    patterns = {
        "labelled_private": r"(?im)\b(?:private(?:\s+note)?|password|secret|mật\s+khẩu|ghi\s+chú\s+riêng\s+tư)\s*[:=]\s*[^\n]+",
        "email": r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "phone": r"(?<![\w])(?:\+?\d[ .()-]*){9,15}(?![\w])",
        "learner_id": r"(?i)\blearner:sv-[a-z0-9-]+\b",
    }
    for name, pattern in patterns.items():
        text, count = re.subn(pattern, "[REDACTED]", text)
        if count:
            hits.append(name)
    return RedactionResult(text, tuple(hits))


@dataclass(frozen=True, slots=True)
class ArithmeticCheckResult:
    checked: bool
    ok: bool | None
    detail: str


_NUM = r"-?\d{1,18}(?:\.\d{1,12})?"
_NUMBER_RE = re.compile(r"(?<![\w.])" + _NUM + r"(?!\w|\.\d)")
_EQUATION_RE = re.compile(r"(" + _NUM + r")\s*([+*/×÷−-])\s*(" + _NUM + r")\s*=\s*(" + _NUM + r")")


def verify_arithmetic(text: str, *, source_texts: Iterable[str] | None = None) -> ArithmeticCheckResult:
    """Check bounded binary equations exactly; compare other numbers to evidence.

    No eval, floating-point rounding or invented evidence. A plain number
    without source_texts remains unchecked rather than being called correct.
    """
    # Unsupported numeric syntax/length must not silently bypass validation.
    if re.search(r"(?<!\w)\d+(?:\.\d+)?[eE][+-]?\d+", text):
        return ArithmeticCheckResult(True, False, "scientific notation needs explicit verification")
    if re.search(r"(?<!\w)\d{19,}|\.\d{13,}", text):
        return ArithmeticCheckResult(True, False, "numeric literal exceeds verification bounds")
    equations = list(_EQUATION_RE.finditer(text))
    for match in equations:
        a, op, b, expected = match.groups()
        x, y = Fraction(a), Fraction(b)
        if op in ("/", "÷") and not y:
            return ArithmeticCheckResult(True, False, "division by zero")
        value = {"+": lambda: x + y, "-": lambda: x - y, "−": lambda: x - y,
                 "*": lambda: x * y, "×": lambda: x * y, "/": lambda: x / y, "÷": lambda: x / y}[op]()
        if value != Fraction(expected):
            return ArithmeticCheckResult(True, False, "incorrect arithmetic equation")
    numbers = _NUMBER_RE.findall(text)
    if source_texts is not None:
        sources = [n for source in source_texts for n in _NUMBER_RE.findall(source)]
        # Computed results may be derived, but operands must still be sourced.
        derived_spans = [match.span(4) for match in equations]
        for match in _NUMBER_RE.finditer(text):
            if match.span() in derived_spans:
                continue
            number = match.group()
            decimals = max(0, -Decimal(number).as_tuple().exponent)
            if not any(Fraction(number) == Fraction(source) and decimals <= max(0, -Decimal(source).as_tuple().exponent) for source in sources):
                return ArithmeticCheckResult(True, False, "numeric claim or precision is not supported by cited evidence")
        if numbers:
            return ArithmeticCheckResult(True, True, "numbers and precision supported by evidence")
    if equations:
        return ArithmeticCheckResult(True, True, "binary equations verified exactly")
    return ArithmeticCheckResult(False, None, "no checkable equation" if numbers else "no numeric claims")


def abstention_policy(grounding: GroundingResult, *, missing_required: bool = False,
                      unresolved_conflict: bool = False, unsafe: bool = False) -> bool:
    return not grounding.grounded or missing_required or unresolved_conflict or unsafe
