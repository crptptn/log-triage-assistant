"""Маскирование секретов и персональных данных до индексации и перед выдачей наружу.

Порядок правил важен: JWT маскируется раньше общего Bearer-токена, а пароль в строке
подключения — раньше e-mail (иначе «pass@host.ru» могла бы принять за адрес почты).
"""

from __future__ import annotations

import re
from collections import Counter

RULES: list[tuple[str, re.Pattern, str]] = [
    ("jwt", re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), "<JWT>"),
    ("bearer", re.compile(r"(?i)\b(bearer\s+)(?!<)[\w.~+/=-]{8,}"), r"\1<TOKEN>"),
    ("dsn_password", re.compile(r"(\b[a-z][\w+.-]*://[^:/\s@]+:)[^@\s]+(@)"), r"\1<PASSWORD>\2"),
    (
        "secret",
        re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token)(\s*[=:]\s*)(?!<)[^\s,;\"']+"),
        r"\1\2<SECRET>",
    ),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "<EMAIL>"),
    ("phone", re.compile(r"(?<![\w+])(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"), "<PHONE>"),
    ("card", re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?!\d)"), "<CARD>"),
]


def mask_with_stats(text: str) -> tuple[str, Counter]:
    stats: Counter = Counter()
    for name, pattern, replacement in RULES:
        text, n = pattern.subn(replacement, text)
        if n:
            stats[name] += n
    return text, stats


def mask(text: str) -> str:
    return mask_with_stats(text)[0]
