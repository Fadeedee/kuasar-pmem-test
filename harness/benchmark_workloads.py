#!/usr/bin/env python3
from __future__ import annotations

import re
from dataclasses import dataclass


GENERIC_MARKERS = {
    "app_ready": "KUASAR_BENCH_APP_READY",
    "operation_begin": "KUASAR_BENCH_OPERATION_BEGIN",
    "operation_end": "KUASAR_BENCH_OPERATION_END",
    "ready": "KUASAR_BENCH_READY",
}

_DIGEST_PREFIX = "KUASAR_BENCH_RESULT_SHA256="
_BYTES_PREFIX = "KUASAR_BENCH_BYTES="
_CAPABILITY_PREFIX = "KUASAR_BENCH_CAPABILITY="
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Workload:
    name: str
    image_name: str
    result_kind: str
    command: str


def workload_names() -> tuple[str, ...]:
    return ("full-tree-scan", "nginx-first-request", "mysql-capability-smoke")


def _full_tree_scan(hold_seconds: int) -> Workload:
    command = "; ".join(
        [
            "set -eu",
            f"echo {GENERIC_MARKERS['app_ready']}",
            f"echo {GENERIC_MARKERS['operation_begin']}",
            "scan_output=$(/opt/sandbox-runtime/bin/read-tree)",
            'printf "%s\\n" "$scan_output"',
            'scan_bytes=${scan_output#KUASAR_REUSE_BYTES=}',
            'case "$scan_bytes" in *[!0-9]*|"") exit 1 ;; esac',
            f'echo "{_BYTES_PREFIX}$scan_bytes"',
            f"echo {GENERIC_MARKERS['operation_end']}",
            f"echo {GENERIC_MARKERS['ready']}",
            f"sleep {hold_seconds}",
        ]
    )
    return Workload(
        name="full-tree-scan",
        image_name="openeuler-24.03-lts",
        result_kind="byte-count",
        command=command,
    )


def _nginx_first_request(hold_seconds: int) -> Workload:
    command = "; ".join(
        [
            "set -eu",
            "nginx -g 'daemon off;' >/tmp/kuasar-nginx.log 2>&1 & nginx_pid=$!",
            "trap 'kill \"$nginx_pid\" 2>/dev/null || true' EXIT",
            "attempt=0",
            "until wget -q -O /tmp/kuasar-ready.html http://127.0.0.1/; do "
            "kill -0 \"$nginx_pid\" 2>/dev/null || { cat /tmp/kuasar-nginx.log; exit 1; }; "
            "attempt=$((attempt + 1)); [ \"$attempt\" -lt 300 ] || exit 1; sleep 0.1; done",
            f"echo {GENERIC_MARKERS['app_ready']}",
            "sleep 2",
            f"echo {GENERIC_MARKERS['operation_begin']}",
            "wget -q -O /tmp/kuasar-response.html http://127.0.0.1/",
            "response_sha256=$(sha256sum /tmp/kuasar-response.html)",
            "response_sha256=${response_sha256%% *}",
            "response_bytes=$(wc -c </tmp/kuasar-response.html)",
            "response_bytes=${response_bytes##* }",
            f'echo "{_DIGEST_PREFIX}$response_sha256"',
            f'echo "{_BYTES_PREFIX}$response_bytes"',
            f"echo {GENERIC_MARKERS['operation_end']}",
            f"echo {GENERIC_MARKERS['ready']}",
            f"sleep {hold_seconds}",
        ]
    )
    return Workload(
        name="nginx-first-request",
        image_name="nginx-1.27.3-alpine",
        result_kind="sha256",
        command=command,
    )


def _mysql_capability_smoke(hold_seconds: int) -> Workload:
    command = "; ".join(
        [
            "set -eu",
            "command -v mysqld",
            "command -v mysqladmin",
            "command -v mysql",
            f'echo "{_CAPABILITY_PREFIX}mysqld,mysqladmin,mysql"',
            f"echo {GENERIC_MARKERS['app_ready']}",
            f"echo {GENERIC_MARKERS['operation_begin']}",
            f"echo {GENERIC_MARKERS['operation_end']}",
            f"echo {GENERIC_MARKERS['ready']}",
            f"sleep {hold_seconds}",
        ]
    )
    return Workload(
        name="mysql-capability-smoke",
        image_name="mysql-8.4",
        result_kind="capability",
        command=command,
    )


def build_workload(name: str, *, hold_seconds: int = 600) -> Workload:
    if hold_seconds <= 0:
        raise ValueError("hold_seconds must be positive")
    builders = {
        "full-tree-scan": _full_tree_scan,
        "nginx-first-request": _nginx_first_request,
        "mysql-capability-smoke": _mysql_capability_smoke,
    }
    try:
        return builders[name](hold_seconds)
    except KeyError as error:
        raise ValueError(f"unknown workload: {name}") from error


def parse_workload_output(line: bytes) -> tuple[str | None, str | int | None]:
    if not line.startswith(b"KUASAR_BENCH_"):
        return None, None
    text = line.decode("utf-8", errors="strict").strip()
    for name, marker in GENERIC_MARKERS.items():
        if text == marker:
            return name, None
    if text.startswith(_DIGEST_PREFIX):
        digest = text.removeprefix(_DIGEST_PREFIX)
        if not _SHA256.fullmatch(digest):
            raise ValueError("invalid workload result SHA256")
        return "result_sha256", digest
    if text.startswith(_BYTES_PREFIX):
        value = text.removeprefix(_BYTES_PREFIX)
        if not value.isdigit():
            raise ValueError("invalid workload byte count")
        return "data_bytes", int(value)
    if text.startswith(_CAPABILITY_PREFIX):
        value = text.removeprefix(_CAPABILITY_PREFIX)
        if not value:
            raise ValueError("empty workload capability result")
        return "capability", value
    return None, None
