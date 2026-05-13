#!/usr/bin/env python3
"""Universal secure webhook listener for NetClaw/OpenClaw.

Provider adapters keep source-specific parsing/reply logic small while this
process owns HTTP routing, health, and common dispatch patterns.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from webhook_core import (
    JsonError,
    StateStore,
    WebhookAdapter,
    WebhookRequest,
    bad_json,
    constant_time_hmac_sha256,
    env,
    env_bool,
    env_int,
    sha256_hex,
    timestamp_is_fresh,
)


class VikunjaWebhookAdapter(WebhookAdapter):
    name = "vikunja"

    def __init__(self) -> None:
        from vikunja_adapter import ListenerConfig, VikunjaAdapter

        self.listener = VikunjaAdapter(ListenerConfig())

    def handle(self, request: WebhookRequest) -> Tuple[int, Dict[str, Any]]:
        return self.listener.handle(request.raw, request.headers)


class GenericSignedAgentAdapter(WebhookAdapter):
    """Provider-neutral signed webhook -> OpenClaw session adapter.

    This is intentionally conservative: it requires a shared secret by default,
    dedupes events persistently, then dispatches one OpenClaw session keyed by a
    supplied object id or event id. It does not attempt an outbound provider reply.
    """

    name = "generic"

    def __init__(self) -> None:
        self.secret = env("GENERIC_WEBHOOK_SECRET", env("WEBHOOK_GENERIC_SECRET"))
        self.require_secret = env_bool("GENERIC_WEBHOOK_REQUIRE_SECRET", True)
        self.replay_window = env_int("WEBHOOK_REPLAY_WINDOW_SECONDS", 300)
        self.state = StateStore(
            env(
                "GENERIC_WEBHOOK_STATE_PATH",
                "/root/.openclaw/workspace/state/generic-webhook-state.json",
            )
        )
        self.agent_id = env("WEBHOOK_OPENCLAW_AGENT_ID", env("VIKUNJA_OPENCLAW_AGENT_ID", "main"))
        self.agent_model = env("WEBHOOK_OPENCLAW_MODEL", env("VIKUNJA_OPENCLAW_MODEL"))
        self.async_dispatch = env_bool("GENERIC_WEBHOOK_ASYNC", True)
        self.max_payload_chars = env_int("GENERIC_WEBHOOK_MAX_PAYLOAD_CHARS", 12000)

    def _header(self, headers: Dict[str, str], *names: str) -> Optional[str]:
        lowered = {k.lower(): v for k, v in headers.items()}
        for name in names:
            value = lowered.get(name.lower())
            if value:
                return value
        return None

    def _verify(self, request: WebhookRequest) -> Optional[Tuple[int, Dict[str, Any]]]:
        if self.require_secret and not self.secret:
            return JsonError.response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "generic_secret_not_configured",
                hint="set GENERIC_WEBHOOK_SECRET or WEBHOOK_GENERIC_SECRET",
            )
        if self.secret:
            signature = self._header(
                request.headers,
                "X-Webhook-Signature",
                "X-Hub-Signature-256",
                "X-Signature-256",
            )
            if not constant_time_hmac_sha256(self.secret, request.raw, signature):
                return JsonError.response(HTTPStatus.FORBIDDEN, "invalid_signature")
        timestamp = self._header(request.headers, "X-Webhook-Timestamp", "X-Timestamp")
        if not timestamp_is_fresh(timestamp, self.replay_window):
            return JsonError.response(HTTPStatus.FORBIDDEN, "stale_or_invalid_timestamp")
        return None

    def handle(self, request: WebhookRequest) -> Tuple[int, Dict[str, Any]]:
        rejected = self._verify(request)
        if rejected:
            return rejected
        try:
            payload = request.json()
        except json.JSONDecodeError:
            return bad_json()

        header_event_id = self._header(request.headers, "X-Webhook-Id", "X-Request-Id", "X-Event-Id")
        event_id = str(
            header_event_id
            or payload.get("event_id")
            or payload.get("id")
            or sha256_hex(request.raw)
        )
        scoped_event_id = f"generic:{event_id}"
        if self.state.seen_event(scoped_event_id):
            return HTTPStatus.OK, {"status": "ignored", "reason": "duplicate_event", "event_id": event_id}

        object_id = str(
            payload.get("object_id")
            or payload.get("task_id")
            or payload.get("issue_id")
            or payload.get("id")
            or event_id[:12]
        )
        session_id = f"webhook-generic-{object_id}".replace("/", "-").replace(" ", "-")[:80]
        claim_key = f"generic:{session_id}:{event_id}"
        if not self.state.claim_action(claim_key):
            self.state.mark_event(scoped_event_id)
            return HTTPStatus.OK, {"status": "ignored", "reason": "duplicate_inflight", "event_id": event_id}

        prompt = self._build_prompt(payload, event_id)
        if self.async_dispatch:
            thread = threading.Thread(
                target=self._run_agent_job,
                args=(session_id, prompt, claim_key),
                daemon=True,
            )
            thread.start()
            self.state.mark_event(scoped_event_id)
            return HTTPStatus.ACCEPTED, {
                "status": "accepted",
                "provider": "generic",
                "event_id": event_id,
                "session_id": session_id,
            }

        status, body = self._run_agent_job(session_id, prompt, claim_key)
        self.state.mark_event(scoped_event_id)
        return status, body

    def _build_prompt(self, payload: Dict[str, Any], event_id: str) -> str:
        compact = json.dumps(payload, indent=2, sort_keys=True)[: self.max_payload_chars]
        return (
            "You are NetClaw handling a generic signed webhook event.\n\n"
            f"Event id: {event_id}\n"
            "Required workflow:\n"
            "1. Inspect the normalized JSON payload below.\n"
            "2. Determine whether a NetClaw skill/tool should be used.\n"
            "3. If action is unsafe, destructive, external-facing, or under-scoped, stop and report the blocker.\n"
            "4. If action is safe and sufficiently scoped, do the work and produce a concise result.\n\n"
            "Webhook payload JSON:\n"
            f"{compact}\n"
        )

    def _run_agent_job(self, session_id: str, prompt: str, claim_key: str) -> Tuple[int, Dict[str, Any]]:
        command = [
            "openclaw",
            "agent",
            "--local",
            "--agent",
            self.agent_id or "main",
            "--session-id",
            session_id,
            "--message",
            prompt,
            "--json",
            "--timeout",
            "600",
        ]
        if self.agent_model:
            command.extend(["--model", self.agent_model])
        try:
            proc = subprocess.run(
                command,
                cwd="/root/.openclaw/workspace",
                env={**os.environ},
                capture_output=True,
                text=True,
                timeout=900,
            )
        except Exception as exc:
            self.state.release_action(claim_key)
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error", "reason": "agent_launch_failed", "detail": str(exc)}
        if proc.returncode != 0:
            self.state.release_action(claim_key)
            detail = (proc.stderr or proc.stdout or "agent run failed").strip().replace("\n", " ")[:800]
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error", "reason": "agent_failed", "detail": detail}
        return HTTPStatus.OK, {"status": "completed", "session_id": session_id}


class UniversalWebhookService:
    def __init__(self) -> None:
        self.listen_host = env("WEBHOOK_LISTEN_HOST", env("VIKUNJA_LISTEN_HOST", "0.0.0.0"))
        self.listen_port = env_int("WEBHOOK_LISTEN_PORT", env_int("VIKUNJA_LISTEN_PORT", 8787))
        self.adapters: Dict[str, WebhookAdapter] = {}
        enabled = {x.strip() for x in (env("WEBHOOK_ENABLED_PROVIDERS", "vikunja,generic,atlassian") or "").split(",") if x.strip()}
        if "vikunja" in enabled:
            self.adapters["vikunja"] = VikunjaWebhookAdapter()
        if "generic" in enabled:
            self.adapters["generic"] = GenericSignedAgentAdapter()
        if "atlassian" in enabled:
            from atlassian_adapter import AtlassianWebhookAdapter

            self.adapters["atlassian"] = AtlassianWebhookAdapter()

    def handle(self, path: str, raw: bytes, headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        clean = path.split("?", 1)[0].rstrip("/")
        if clean.startswith("/webhooks/"):
            provider = clean.split("/", 3)[2]
        else:
            return HTTPStatus.NOT_FOUND, {"status": "not_found"}
        adapter = self.adapters.get(provider)
        if not adapter:
            return HTTPStatus.NOT_FOUND, {"status": "not_found", "reason": "provider_not_enabled", "provider": provider}
        return adapter.handle(WebhookRequest(provider=provider, path=clean, raw=raw, headers=headers))


class RequestHandler(BaseHTTPRequestHandler):
    service: UniversalWebhookService

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        status, body = self.service.handle(self.path, raw, dict(self.headers))
        self._json(status, body)

    def do_GET(self) -> None:
        clean = self.path.split("?", 1)[0].rstrip("/")
        if clean == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok", "providers": sorted(self.service.adapters.keys())})
            return
        self._json(HTTPStatus.NOT_FOUND, {"status": "not_found"})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, status: int, body: Dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    service = UniversalWebhookService()
    RequestHandler.service = service
    server = ThreadingHTTPServer((service.listen_host, service.listen_port), RequestHandler)
    print(
        json.dumps(
            {
                "listen": f"{service.listen_host}:{service.listen_port}",
                "paths": ["/webhooks/<provider>", "/healthz"],
                "providers": sorted(service.adapters.keys()),
            }
        )
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
