"""Генератор синтетических логов тестового стенда с инцидентами, причина которых известна.

Стенд: шлюз API, агент устройств (Windows-ПО под Wine с USB-принтерами и сканерами),
сервис отчётов на PostgreSQL, обработчик очереди RabbitMQ, сервис авторизации и события Kubernetes.
Фоновый шум включает «ложные следы» — медленные запросы, fixme от Wine, повторные доставки —
и персональные данные с секретами, чтобы было что маскировать.

Для каждого инцидента сохраняется описание, каким его написал бы тестировщик, истинная причина
и номера строк логов, где она видна. По ним считаются метрики качества поиска и классификации.
"""

from __future__ import annotations

import json
import random
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVICES = ["api-gateway", "auth-service", "device-agent", "k8s-events", "queue-worker", "report-service"]
START = datetime(2026, 5, 14, 9, 0, tzinfo=timezone.utc)
HOURS = 6

USERS = ["ivan.petrov", "anna.smirnova", "o.kuznetsova", "d.volkov", "test.operator"]
CLIENTS = ["web-ui", "device-agent", "report-cli", "mobile-app"]


@dataclass
class Event:
    ts: datetime
    level: str
    service: str
    trace: str
    message: str
    incident: str | None = None

    def format(self) -> str:
        ms = self.ts.microsecond // 1000
        return f"{self.ts:%Y-%m-%dT%H:%M:%S}.{ms:03d}Z {self.level:<5} {self.service} [trace={self.trace or '-'}] {self.message}"


class Ctx:
    """Случайные значения для шаблонов строк."""

    def __init__(self, rng: random.Random):
        self.rng = rng

    def trace(self) -> str:
        return "".join(self.rng.choices("0123456789abcdef", k=16))

    def hexid(self, n: int = 5) -> str:
        return "".join(self.rng.choices("0123456789abcdef", k=n))

    def uuid(self) -> str:
        h = "".join(self.rng.choices("0123456789abcdef", k=32))
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

    def email(self) -> str:
        return f"{self.rng.choice(USERS)}@example.ru"

    def phone(self) -> str:
        d = [self.rng.randint(0, 9) for _ in range(7)]
        return f"+7 9{self.rng.randint(10, 99)} {d[0]}{d[1]}{d[2]}-{d[3]}{d[4]}-{d[5]}{d[6]}"

    def jwt(self) -> str:
        body = "".join(self.rng.choices(string.ascii_letters + string.digits, k=24))
        sig = "".join(self.rng.choices(string.ascii_letters + string.digits + "-_", k=20))
        return f"eyJhbGciOiJIUzI1NiJ9.{body}.{sig}"

    def password(self) -> str:
        return "".join(self.rng.choices(string.ascii_letters + string.digits, k=10))


# --- фоновый шум ------------------------------------------------------------

NOISE = {
    "api-gateway": [
        (20, "INFO", True, lambda c: f"GET /api/v1/devices 200 {c.rng.randint(8, 60)}ms user={c.email()}"),
        (
            20,
            "INFO",
            True,
            lambda c: f"POST /api/v1/print 200 {c.rng.randint(40, 300)}ms job={c.rng.randint(1000, 9999)}",
        ),
        (20, "INFO", True, lambda c: f"GET /api/v1/reports/{c.rng.randint(100, 999)} 200 {c.rng.randint(30, 400)}ms"),
        (
            4,
            "WARN",
            True,
            lambda c: f"slow upstream report-service {c.rng.randint(1200, 2500)}ms on GET /api/v1/reports",
        ),
        (3, "DEBUG", True, lambda c: f"forwarding request, Authorization: Bearer {c.jwt()}"),
    ],
    "auth-service": [
        (20, "INFO", False, lambda c: f"token issued for client {c.rng.choice(CLIENTS)} ttl=3600s"),
        (12, "INFO", False, lambda c: f"login ok user={c.email()} phone={c.phone()}"),
        (3, "WARN", False, lambda c: f"rate limit approaching for client {c.rng.choice(CLIENTS)}"),
    ],
    "device-agent": [
        (15, "INFO", False, lambda c: f"wine: device session opened for scanner-{c.rng.randint(1, 3)}"),
        (
            20,
            "INFO",
            True,
            lambda c: f"print job {c.rng.randint(1000, 9999)} sent to label-printer-{c.rng.randint(1, 3)}",
        ),
        (10, "DEBUG", False, lambda c: f"libusb: bulk transfer ok, {c.rng.randint(64, 4096)} bytes"),
        (5, "WARN", False, lambda c: f"wine: fixme:ntoskrnl:IoGetDeviceProperty unhandled property 0x{c.hexid(4)}"),
    ],
    "k8s-events": [
        (10, "INFO", False, lambda c: f"pod {c.rng.choice(SERVICES[:3])}-{c.hexid()} liveness probe ok"),
        (2, "INFO", False, lambda c: f"scaled deployment queue-worker to {c.rng.randint(2, 4)} replicas"),
    ],
    "queue-worker": [
        (
            20,
            "INFO",
            False,
            lambda c: f"consumed message {c.uuid()} from reports.generate, ack in {c.rng.randint(5, 80)}ms",
        ),
        (8, "INFO", False, lambda c: f"queue reports.generate depth={c.rng.randint(0, 300)}"),
        (3, "WARN", False, lambda c: f"redelivered message {c.uuid()} (attempt 2)"),
    ],
    "report-service": [
        (20, "INFO", True, lambda c: f"report {c.rng.randint(100, 999)} generated in {c.rng.randint(200, 3000)}ms"),
        (10, "INFO", False, lambda c: f"db query reports.by_period took {c.rng.randint(20, 400)}ms"),
        (4, "WARN", False, lambda c: f"slow query reports.by_period {c.rng.randint(1500, 4000)}ms"),
        (2, "DEBUG", False, lambda c: f"connecting to postgresql://report:{c.password()}@db-main:5432/reports"),
    ],
}
RATE_SECONDS = {
    "api-gateway": 25,
    "auth-service": 60,
    "device-agent": 45,
    "k8s-events": 120,
    "queue-worker": 40,
    "report-service": 40,
}


# --- инциденты --------------------------------------------------------------
# каждый шаг: (смещение в секундах, уровень, сервис, привязан к trace, шаблон сообщения)

INCIDENTS = {
    "usb_device": {
        "title": "Тест печати этикеток упал около {hhmm}: принтер перестал отвечать, в интерфейсе ошибка 502",
        "root_cause": "После переподключения USB-принтер получил новый номер устройства, а udev-правило не выдало "
        "права процессу под Wine — libusb не может открыть устройство.",
        "steps": [
            (
                0,
                "ERROR",
                "device-agent",
                True,
                "libusb: error [submit_bulk_transfer] submiturb failed, errno=19 (LIBUSB_ERROR_NO_DEVICE)",
            ),
            (1, "ERROR", "device-agent", True, "udev: cannot open /dev/bus/usb/001/{dev}: Permission denied"),
            (2, "ERROR", "device-agent", True, "print job {job} failed: device label-printer-2 not accessible"),
            (2, "ERROR", "api-gateway", True, "POST /api/v1/print 502 upstream device-agent error"),
            (6, "WARN", "device-agent", False, "retrying device reconnect for label-printer-2 (attempt 1/3)"),
            (18, "ERROR", "device-agent", False, "device label-printer-2 lost after 3 reconnect attempts"),
        ],
    },
    "queue_overflow": {
        "title": "Отчёты перестали формироваться после {hhmm}, запросы на генерацию висят и падают по таймауту",
        "root_cause": "Обработчик не успевает подтверждать сообщения: брокер закрывает канал по таймауту подтверждения, "
        "очередь reports.generate растёт.",
        "steps": [
            (0, "WARN", "queue-worker", False, "queue reports.generate depth=8500 exceeds threshold 5000"),
            (
                20,
                "ERROR",
                "queue-worker",
                False,
                "channel closed by broker: PRECONDITION_FAILED - delivery acknowledgement on channel 1 timed out",
            ),
            (21, "ERROR", "queue-worker", False, "consumer reports-worker-1 cancelled, 2400 messages unacked"),
            (35, "ERROR", "api-gateway", True, "POST /api/v1/reports 504 gateway timeout after 30000ms"),
            (60, "WARN", "queue-worker", False, "queue reports.generate depth=12000 exceeds threshold 5000"),
        ],
    },
    "db_pool": {
        "title": "Около {hhmm} список отчётов в интерфейсе отдаёт ошибку 500 у всех пользователей",
        "root_cause": "Исчерпан пул соединений report-service к PostgreSQL: соединения не возвращаются в пул, "
        "новые запросы ждут и падают по таймауту.",
        "steps": [
            (0, "WARN", "report-service", False, "connection pool exhausted: 20/20 in use, 37 waiting"),
            (
                30,
                "ERROR",
                "report-service",
                True,
                "psycopg_pool.PoolTimeout: couldn't get a connection after 30.00 sec (pool size 20)",
            ),
            (30, "ERROR", "api-gateway", True, "GET /api/v1/reports 500 upstream report-service error"),
            (31, "ERROR", "report-service", False, "FATAL: sorry, too many clients already"),
        ],
    },
    "network": {
        "title": "В {hhmm} часть запросов к отчётам падает с ошибкой, повторный запрос иногда проходит",
        "root_cause": "Во время rolling update report-service закрывал соединения, а шлюз продолжал слать запросы "
        "на завершающийся под: нет readiness-проверки и плавного вывода из балансировки.",
        "steps": [
            (0, "INFO", "report-service", False, "graceful shutdown requested, closing listeners"),
            (
                1,
                "ERROR",
                "api-gateway",
                True,
                "upstream report-service: ConnectionResetError: [Errno 104] Connection reset by peer",
            ),
            (2, "WARN", "api-gateway", True, "retry 1/3 to report-service after connection reset"),
            (3, "ERROR", "api-gateway", True, "upstream report-service: connect ECONNREFUSED 10.42.3.17:8080"),
            (5, "INFO", "k8s-events", False, "pod report-service-{pod} terminated: rolling update"),
        ],
    },
    "auth_expired": {
        "title": "После {hhmm} сканер перестал отправлять документы, в журнале агента ошибки авторизации",
        "root_cause": "У device-agent истёк токен доступа, а обновление токена не настроено — все запросы агента получают 401.",
        "steps": [
            (0, "WARN", "auth-service", False, "rejected token for client device-agent: expired at {hhmmss}"),
            (0, "ERROR", "device-agent", True, "upload failed: 401 Unauthorized (invalid_token: token expired)"),
            (1, "WARN", "api-gateway", True, "POST /api/v1/documents 401 client=device-agent"),
            (2, "ERROR", "device-agent", False, "token refresh failed: refresh_token not configured"),
        ],
    },
    "oom": {
        "title": "Около {hhmm} генерация большого отчёта за квартал оборвалась, сервис отчётов на минуту пропал",
        "root_cause": "Отчёт целиком строится в памяти; на квартальном объёме под превышает лимит 2Gi и завершается OOMKilled.",
        "steps": [
            (0, "WARN", "report-service", True, "rendering report {report}: 1200000 rows, memory usage 1.8Gi"),
            (25, "ERROR", "report-service", True, "MemoryError while rendering report {report}"),
            (26, "WARN", "k8s-events", False, "pod report-service-{pod} OOMKilled (limit 2Gi), restarting"),
            (27, "ERROR", "api-gateway", True, "GET /api/v1/reports/{report} 503 upstream unavailable"),
            (70, "INFO", "k8s-events", False, "pod report-service-{pod} started"),
        ],
    },
    "config_error": {
        "title": "После деплоя в {hhmm} обработчик очереди не стартует, отчёты не генерируются",
        "root_cause": "В новой версии обработчика появилась обязательная переменная REPORTS_BUCKET, но её не добавили "
        "в конфигурацию стенда — под падает в CrashLoopBackOff.",
        "steps": [
            (0, "INFO", "k8s-events", False, "deployment queue-worker updated to version 2.4.0"),
            (10, "ERROR", "queue-worker", False, "startup failed: missing required env var REPORTS_BUCKET"),
            (10, "ERROR", "queue-worker", False, "KeyError: 'REPORTS_BUCKET'"),
            (60, "WARN", "k8s-events", False, "pod queue-worker-{pod} CrashLoopBackOff (restarts: 4)"),
            (90, "WARN", "queue-worker", False, "queue reports.generate depth=900, no active consumers"),
        ],
    },
    "disk_full": {
        "title": "В {hhmm} выгрузка отчётов в PDF начала падать с непонятной ошибкой",
        "root_cause": "Временные PDF-файлы не удаляются, раздел /var/lib/reports заполнен на 99% — запись новых файлов "
        "падает с ENOSPC.",
        "steps": [
            (0, "WARN", "report-service", False, "disk usage /var/lib/reports 99%"),
            (
                40,
                "ERROR",
                "report-service",
                True,
                "OSError: [Errno 28] No space left on device: '/var/lib/reports/tmp/{report}.pdf'",
            ),
            (40, "ERROR", "api-gateway", True, "GET /api/v1/reports/{report}/pdf 500"),
            (45, "ERROR", "report-service", False, "cleanup of temporary files skipped: previous job still running"),
        ],
    },
}


def _noise(ctx: Ctx, service: str) -> list[Event]:
    events = []
    templates = NOISE[service]
    weights = [t[0] for t in templates]
    ts = START + timedelta(seconds=ctx.rng.uniform(0, RATE_SECONDS[service]))
    end = START + timedelta(hours=HOURS)
    while ts < end:
        _, level, traced, make = ctx.rng.choices(templates, weights=weights)[0]
        events.append(Event(ts, level, service, ctx.trace() if traced else "", make(ctx)))
        ts += timedelta(seconds=ctx.rng.expovariate(1 / RATE_SECONDS[service]))
    return events


def _incident(ctx: Ctx, incident_id: str, category: str, t0: datetime) -> tuple[list[Event], dict]:
    spec = INCIDENTS[category]
    trace = ctx.trace()
    values = {
        "dev": f"{ctx.rng.randint(5, 30):03d}",
        "job": str(ctx.rng.randint(1000, 9999)),
        "pod": ctx.hexid(),
        "report": str(ctx.rng.randint(100, 999)),
        "hhmmss": f"{t0:%H:%M:%S}",
    }
    events = []
    for offset, level, service, traced, template in spec["steps"]:
        ts = t0 + timedelta(seconds=offset, milliseconds=ctx.rng.randint(0, 900))
        events.append(Event(ts, level, service, trace if traced else "", template.format(**values), incident_id))
    # тестировщик помнит время приблизительно
    reported = t0 + timedelta(minutes=ctx.rng.randint(0, 3))
    meta = {
        "id": incident_id,
        "category": category,
        "title": spec["title"].format(hhmm=f"{reported:%H:%M}"),
        "started_at": t0.isoformat(),
        "services": sorted({e.service for e in events}),
        "trace_id": trace,
        "root_cause": spec["root_cause"],
    }
    return events, meta


def generate(out_dir: Path, seed: int = 7, per_category: int = 2) -> dict:
    """Пишет out_dir/logs/<сервис>.log и out_dir/incidents.json. Детерминировано при одном seed."""
    rng = random.Random(seed)
    ctx = Ctx(rng)
    events: list[Event] = []
    for service in SERVICES:
        events.extend(_noise(ctx, service))

    categories = [c for c in INCIDENTS for _ in range(per_category)]
    rng.shuffle(categories)
    # слоты не ближе 20 минут друг к другу, чтобы инциденты не накладывались
    slots = rng.sample(range(10, HOURS * 60 - 10, 20), len(categories))
    incidents = []
    for i, (category, minute) in enumerate(zip(categories, sorted(slots)), start=1):
        t0 = START + timedelta(minutes=minute, seconds=rng.randint(0, 59))
        inc_events, meta = _incident(ctx, f"INC-{i:03d}", category, t0)
        events.extend(inc_events)
        incidents.append(meta)

    logs_dir = out_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    signature: dict[str, list[list]] = {m["id"]: [] for m in incidents}
    total = 0
    for service in SERVICES:
        service_events = sorted((e for e in events if e.service == service), key=lambda e: e.ts)
        with (logs_dir / f"{service}.log").open("w", encoding="utf-8") as f:
            for line_no, event in enumerate(service_events, start=1):
                f.write(event.format() + "\n")
                if event.incident:
                    signature[event.incident].append([service, line_no])
        total += len(service_events)
    for meta in incidents:
        meta["signature"] = signature[meta["id"]]
    (out_dir / "incidents.json").write_text(json.dumps(incidents, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"lines": total, "incidents": len(incidents), "services": len(SERVICES), "logs_dir": str(logs_dir)}
