from datetime import timedelta

import pytest

from triage.logparse import normalize, parse_lines, render, trace_chunks, window_chunks
from triage.masking import mask, mask_with_stats


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc123DEF.sig-_x", "Authorization: Bearer <JWT>"),
        ("token Bearer abcdefgh12345678", "token Bearer <TOKEN>"),
        (
            "connecting to postgresql://report:S3cretPass@db-main:5432/reports",
            "connecting to postgresql://report:<PASSWORD>@db-main:5432/reports",
        ),
        ("password=qwerty123 api_key: 9f8e7d", "password=<SECRET> api_key: <SECRET>"),
        ("login ok user=ivan.petrov@example.ru", "login ok user=<EMAIL>"),
        ("phone=+7 912 345-67-89", "phone=<PHONE>"),
        ("card 4111 1111 1111 1111 declined", "card <CARD> declined"),
    ],
)
def test_masking(raw, expected):
    assert mask(raw) == expected


def test_masking_keeps_ordinary_log_text():
    line = "09:50:31 ERROR udev: cannot open /dev/bus/usb/001/016: Permission denied, job=4521, took 812ms"
    assert mask(line) == line


def test_mask_stats():
    _, stats = mask_with_stats("a@b.ru c@d.ru Bearer abcdefgh1234")
    assert stats["email"] == 2 and stats["bearer"] == 1


RAW = [
    "2026-05-14T09:50:30.100Z ERROR device-agent [trace=aa11] libusb: error LIBUSB_ERROR_NO_DEVICE",
    "Traceback (most recent call last):",
    "2026-05-14T09:50:31.200Z ERROR device-agent [trace=aa11] udev: cannot open /dev/bus/usb/001/016",
    "2026-05-14T09:50:40.000Z INFO  device-agent [trace=-] print job 1001 sent to label-printer-1",
    "2026-05-14T09:50:41.000Z INFO  device-agent [trace=-] print job 1002 sent to label-printer-1",
    "2026-05-14T09:57:00.000Z INFO  device-agent [trace=-] print job 1003 sent to label-printer-2",
]


def test_parse_lines_attaches_continuation_and_keeps_line_numbers():
    lines = parse_lines(RAW)
    assert len(lines) == 5
    assert "Traceback" in lines[0].message
    assert [line.line_no for line in lines] == [1, 3, 4, 5, 6]
    assert lines[0].trace == "aa11" and lines[2].trace == ""
    assert lines[1].ts - lines[0].ts == timedelta(seconds=1.1)


def test_normalize_and_compaction():
    assert normalize("job 1001 to 10.0.0.1:8080 id 4f2a9c1e") == "job <N> to <IP> id <HEX>"
    text = render(parse_lines(RAW)[2:4])
    assert "(×2)" in text and text.count("\n") == 0


def test_window_and_trace_chunks():
    lines = parse_lines(RAW)
    windows = window_chunks(lines, window_seconds=300)
    assert [c.chunk_id for c in windows] == ["device-agent:1-5", "device-agent:6-6"]
    assert windows[0].max_severity == 40 and "ERROR×2" in windows[0].text
    traces = trace_chunks(lines)
    assert len(traces) == 1 and traces[0].chunk_id == "trace:aa11"
    payload = traces[0].payload()
    assert payload["line_refs"] == [["device-agent", 1], ["device-agent", 3]]
