#!/usr/bin/env python3
"""Shared secure webhook listener primitives for NetClaw integrations."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Dict, Optional, Tuple


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def env_bool(name: str, default: bool) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = env(name)
    return int(value) if value else default


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def constant_time_hmac_sha256(secret: str, raw: bytes, signature: Optional[str]) -> bool:
    if not secret or not signature:
        return False
    supplied = signature.strip()
    for prefix in ("sha256=", "hmac-sha256=", "v1="):
        if supplied.lower().startswith(prefix):
            supplied = supplied[len(prefix) :]
            break
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied)


def timestamp_is_fresh(value: Optional[str], replay_window_seconds: int) -> bool:
    if not value:
        return True
    try:
        ts = int(float(value))
    except ValueError:
        return False
    return abs(int(time.time()) - ts) <= replay_window_seconds


class StateStore:
    """Small persistent JSON state store for webhook dedupe and in-flight claims."""

    def __init__(self, path: str, max_events: int = 5000) -> None:
        from pathlib import Path

        self.path = Path(path)
        self.max_events = max_events
        self.lock = threading.Lock()
        self.state = {"events": {}, "claims": {}}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.state = json.loads(self.path.read_text())
            except Exception:
                self.state = {"events": {}, "claims": {}}
        self.state.setdefault("events", {})
        self.state.setdefault("claims", {})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=2, sort_keys=True))

    def seen_event(self, event_id: str) -> bool:
        with self.lock:
            return event_id in self.state["events"]

    def mark_event(self, event_id: str, now: Optional[float] = None) -> None:
        with self.lock:
            self.state["events"][event_id] = now or time.time()
            if len(self.state["events"]) > self.max_events:
                items = sorted(self.state["events"].items(), key=lambda kv: kv[1])
                self.state["events"] = dict(items[-self.max_events :])
            self._save()

    def claim_action(self, claim_key: str, now: Optional[float] = None) -> bool:
        with self.lock:
            if claim_key in self.state["claims"]:
                return False
            self.state["claims"][claim_key] = now or time.time()
            if len(self.state["claims"]) > self.max_events:
                items = sorted(self.state["claims"].items(), key=lambda kv: kv[1])
                self.state["claims"] = dict(items[-self.max_events :])
            self._save()
            return True

    def release_action(self, claim_key: str) -> None:
        with self.lock:
            if claim_key in self.state["claims"]:
                self.state["claims"].pop(claim_key, None)
                self._save()


@dataclass
class WebhookRequest:
    provider: str
    path: str
    raw: bytes
    headers: Dict[str, str]

    def json(self) -> Dict[str, Any]:
        return json.loads(self.raw.decode("utf-8"))


class WebhookAdapter:
    name = "base"

    def handle(self, request: WebhookRequest) -> Tuple[int, Dict[str, Any]]:
        raise NotImplementedError


class JsonError:
    @staticmethod
    def response(status: int, reason: str, **extra: Any) -> Tuple[int, Dict[str, Any]]:
        body = {"status": "rejected" if status >= 400 else "ignored", "reason": reason}
        body.update(extra)
        return status, body


def bad_json() -> Tuple[int, Dict[str, Any]]:
    return JsonError.response(HTTPStatus.BAD_REQUEST, "invalid_json")
