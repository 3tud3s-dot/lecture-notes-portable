"""Save non-sensitive spike evidence; never serialize a requests request object."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


REDACTED = "[REDACTED]"
SENSITIVE_FIELDS = {
    "authorization", "proxyauthorization", "apikey", "siliconflowapikey",
    "token", "accesstoken", "refreshtoken", "password", "secret", "credentials",
    "cookie", "setcookie", "headers", "requestheaders", "request", "requestmetadata",
}


def redact(value: object, api_key: str) -> object:
    """Remove credential fields/subtrees and suspicious strings, not just mask ends.

    Also drop strings echoing identifiable key fragments (six or more chars).
    Shorter fragments under credential fields are removed with the entire field.
    """
    if isinstance(value, dict):
        return {
            str(redact(name, api_key)): (
                REDACTED if re.sub(r"[^a-z0-9]", "", str(name).lower()) in SENSITIVE_FIELDS
                else redact(item, api_key)
            )
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, api_key) for item in value]
    if isinstance(value, str):
        fragments = [api_key[i:i + 6] for i in range(max(0, len(api_key) - 5))]
        if ((api_key and api_key in value) or any(part in value for part in fragments)
                or re.search(r"(?i)\bbearer\s+|\bsk-[\w.-]+|(?:api[_ -]?key|authorization|cookie|token|password)\s*[:=]", value)):
            return REDACTED
    return value


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")


def start_run(output_root: Path, metadata: dict, api_key: str) -> Path:
    """Create a unique directory and persist request intent BEFORE sending."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = output_root / f"{stamp}-{uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    write_json(path / "request.json", redact(metadata, api_key))
    write_json(path / "result.json", {"state": "prepared", "http_status": None})
    return path


def finish_run(path: Path, result) -> None:
    """Result contains sanitized values only; no key or HTTP objects are accepted."""
    if result.response_body is not None:
        (path / "response.txt").write_text(result.response_body, encoding="utf-8", newline="")
    if result.response_is_json:
        write_json(path / "response.json", result.response_json)
    if result.transcript is not None:
        (path / "transcript.txt").write_text(result.transcript, encoding="utf-8", newline="")
    write_json(path / "result.json", {
        "state": "response_received" if result.http_status is not None else "transport_failed",
        "http_status": result.http_status,
        "latency_seconds": result.latency_seconds,
        "response_is_json": result.response_is_json,
        "response_redacted": result.response_redacted,
        "response_content_type": result.content_type,
        "trace_id": result.trace_id,
        "error": result.error,
        "nonempty_transcript": bool(result.transcript and result.transcript.strip()),
        "human_content_review": "pending",
        "stage_pass": False,
    })
