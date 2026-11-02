"""Категории инцидентов и признаки для классификации по правилам (режим без LLM)."""

from __future__ import annotations

import re

CATEGORIES: dict[str, dict] = {
    "usb_device": {
        "name": "USB-устройство недоступно",
        "patterns": [r"libusb", r"LIBUSB_ERROR", r"/dev/bus/usb", r"device .* (lost|not accessible)"],
    },
    "queue_overflow": {
        "name": "Переполнение очереди RabbitMQ",
        "patterns": [r"PRECONDITION_FAILED", r"exceeds threshold", r"unacked", r"acknowledgement .* timed out"],
    },
    "db_pool": {
        "name": "Исчерпан пул соединений к БД",
        "patterns": [r"too many clients", r"PoolTimeout", r"pool exhausted"],
    },
    "network": {
        "name": "Сетевые ошибки между сервисами",
        "patterns": [r"Connection reset", r"ECONNREFUSED", r"ECONNRESET"],
    },
    "auth_expired": {
        "name": "Истёк токен авторизации",
        "patterns": [r"token expired", r"invalid_token", r"refresh_token", r"rejected token"],
    },
    "oom": {
        "name": "Нехватка памяти (OOMKilled)",
        "patterns": [r"OOMKilled", r"MemoryError"],
    },
    "config_error": {
        "name": "Ошибка конфигурации",
        "patterns": [r"missing required env", r"CrashLoopBackOff", r"KeyError"],
    },
    "disk_full": {
        "name": "Нет места на диске",
        "patterns": [r"No space left", r"Errno 28", r"disk usage \S+ 9\d%"],
    },
    "other": {"name": "Другое", "patterns": []},
}

_COMPILED = {code: [re.compile(p, re.IGNORECASE) for p in c["patterns"]] for code, c in CATEGORIES.items()}


def category_name(code: str) -> str:
    return CATEGORIES.get(code, CATEGORIES["other"])["name"]


def score_text(text: str) -> dict[str, int]:
    """Сколько признаков каждой категории встречается в тексте."""
    return {code: sum(len(p.findall(text)) for p in patterns) for code, patterns in _COMPILED.items() if patterns}
