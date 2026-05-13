#!/usr/bin/env python3
"""Atlassian provider adapter for the universal webhook listener.

This adapter turns Jira issue/comment webhooks into dedicated OpenClaw sessions.
It does not call Jira directly from the listener; the spawned agent should use the
Atlassian MCP tools from the atlassian-itsm skill for read/write follow-up.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http import HTTPStatus
from typing import Any, Dict, List, Optional, Tuple

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


class AtlassianWebhookAdapter(WebhookAdapter):
    """Jira/Atlassian webhook -> OpenClaw session adapter.

    Trigger policy is deliberately narrow by default:
    - comment body contains the configured mention, default `@netclaw`
    - assignee changes/current assignee match configured NetClaw identity
    - configured trigger label is present, default `netclaw-run,netclaw`
    """

    name = "atlassian"

    def __init__(self) -> None:
        self.secret = env("ATLASSIAN_WEBHOOK_SECRET", env("WEBHOOK_ATLASSIAN_SECRET"))
        self.require_secret = env_bool("ATLASSIAN_WEBHOOK_REQUIRE_SECRET", True)
        self.replay_window = env_int("WEBHOOK_REPLAY_WINDOW_SECONDS", 300)
        self.state = StateStore(
            env(
                "ATLASSIAN_WEBHOOK_STATE_PATH",
                "/root/.openclaw/workspace/state/atlassian-webhook-state.json",
            )
        )
        self.agent_id = env("WEBHOOK_OPENCLAW_AGENT_ID", env("VIKUNJA_OPENCLAW_AGENT_ID", "main"))
        self.agent_model = env("WEBHOOK_OPENCLAW_MODEL", env("VIKUNJA_OPENCLAW_MODEL"))
        self.async_dispatch = env_bool("ATLASSIAN_WEBHOOK_ASYNC", True)
        self.max_payload_chars = env_int("ATLASSIAN_WEBHOOK_MAX_PAYLOAD_CHARS", 16000)
        self.netclaw_mention = env("ATLASSIAN_NETCLAW_MENTION", "@netclaw") or "@netclaw"
        identities = env("ATLASSIAN_NETCLAW_IDENTITIES", "netclaw,@netclaw") or "netclaw,@netclaw"
        self.netclaw_identities = {x.strip().lower() for x in identities.split(",") if x.strip()}
        labels = env("ATLASSIAN_TRIGGER_LABELS", "netclaw-run,netclaw") or "netclaw-run,netclaw"
        self.trigger_labels = {x.strip().lower() for x in labels.split(",") if x.strip()}
        projects = env("ATLASSIAN_ALLOWED_PROJECT_KEYS", "") or ""
        self.allowed_projects = {x.strip().upper() for x in projects.split(",") if x.strip()}
        self.ignore_self = env_bool("ATLASSIAN_IGNORE_SELF_EVENTS", True)

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
                "atlassian_secret_not_configured",
                hint="set ATLASSIAN_WEBHOOK_SECRET or WEBHOOK_ATLASSIAN_SECRET",
            )
        if self.secret:
            signature = self._header(
                request.headers,
                "X-Webhook-Signature",
                "X-Hub-Signature-256",
                "X-Signature-256",
                "X-Atlassian-Webhook-Signature",
            )
            shared_secret = self._header(
                request.headers,
                "X-Atlassian-Webhook-Secret",
                "X-Webhook-Secret",
            )
            if signature:
                if not constant_time_hmac_sha256(self.secret, request.raw, signature):
                    return JsonError.response(HTTPStatus.FORBIDDEN, "invalid_signature")
            elif shared_secret:
                if shared_secret != self.secret:
                    return JsonError.response(HTTPStatus.FORBIDDEN, "invalid_shared_secret")
            elif self.require_secret:
                return JsonError.response(HTTPStatus.FORBIDDEN, "missing_signature_or_secret_header")
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

        issue = payload.get("issue") or {}
        issue_key = str(issue.get("key") or payload.get("issue_key") or payload.get("key") or "").strip()
        if not issue_key:
            return HTTPStatus.OK, {"status": "ignored", "reason": "missing_issue_key"}
        project_key = issue_key.split("-", 1)[0].upper()
        if self.allowed_projects and project_key not in self.allowed_projects:
            return HTTPStatus.OK, {"status": "ignored", "reason": "project_not_allowed", "issue_key": issue_key}

        actor = self._actor(payload)
        if self.ignore_self and self._matches_netclaw(actor):
            return HTTPStatus.OK, {"status": "ignored", "reason": "self_event", "issue_key": issue_key}

        trigger = self._extract_trigger(payload)
        if trigger is None:
            return HTTPStatus.OK, {"status": "ignored", "reason": "no_trigger", "issue_key": issue_key}

        event_id = self._event_id(request, payload, issue_key, trigger)
        scoped_event_id = f"atlassian:{event_id}"
        if self.state.seen_event(scoped_event_id):
            return HTTPStatus.OK, {"status": "ignored", "reason": "duplicate_event", "issue_key": issue_key}

        session_id = f"atlassian-issue-{issue_key}".replace("/", "-").replace(" ", "-")[:80]
        claim_key = f"atlassian:{session_id}:{event_id}"
        if not self.state.claim_action(claim_key):
            self.state.mark_event(scoped_event_id)
            return HTTPStatus.OK, {"status": "ignored", "reason": "duplicate_inflight", "issue_key": issue_key}

        prompt = self._build_prompt(payload, issue_key, actor, trigger, event_id)
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
                "provider": "atlassian",
                "event_id": event_id,
                "issue_key": issue_key,
                "session_id": session_id,
                "trigger": trigger["type"],
            }

        status, body = self._run_agent_job(session_id, prompt, claim_key)
        self.state.mark_event(scoped_event_id)
        return status, body

    def _actor(self, payload: Dict[str, Any]) -> Dict[str, str]:
        user = payload.get("user") or payload.get("actor") or payload.get("comment", {}).get("author") or {}
        return {
            "name": str(user.get("name") or ""),
            "displayName": str(user.get("displayName") or ""),
            "emailAddress": str(user.get("emailAddress") or ""),
            "accountId": str(user.get("accountId") or ""),
        }

    def _matches_netclaw(self, user: Dict[str, str]) -> bool:
        values = {v.lower() for v in user.values() if v}
        return bool(values & self.netclaw_identities)

    def _extract_trigger(self, payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
        event = str(payload.get("webhookEvent") or payload.get("event") or payload.get("event_type") or "")
        issue = payload.get("issue") or {}
        fields = issue.get("fields") or {}

        comment = payload.get("comment") or {}
        comment_body = str(comment.get("body") or "")
        if comment_body and self.netclaw_mention.lower() in comment_body.lower():
            return {
                "type": "mention",
                "event": event,
                "text": comment_body,
                "comment_id": str(comment.get("id") or ""),
            }

        labels = {str(x).lower() for x in (fields.get("labels") or [])}
        matched_labels = sorted(labels & self.trigger_labels)
        if matched_labels:
            return {
                "type": "label",
                "event": event,
                "text": f"Trigger label present: {', '.join(matched_labels)}",
            }

        assignee = fields.get("assignee") or {}
        if self._matches_netclaw(
            {
                "name": str(assignee.get("name") or ""),
                "displayName": str(assignee.get("displayName") or ""),
                "emailAddress": str(assignee.get("emailAddress") or ""),
                "accountId": str(assignee.get("accountId") or ""),
            }
        ):
            return {"type": "assignee", "event": event, "text": "Issue is assigned to NetClaw."}

        for item in (payload.get("changelog") or {}).get("items") or []:
            if str(item.get("field") or "").lower() == "assignee":
                candidate = {
                    "name": str(item.get("to") or ""),
                    "displayName": str(item.get("toString") or ""),
                    "emailAddress": "",
                    "accountId": str(item.get("to") or ""),
                }
                if self._matches_netclaw(candidate):
                    return {"type": "assignee_change", "event": event, "text": "Issue was assigned to NetClaw."}
        return None

    def _event_id(
        self,
        request: WebhookRequest,
        payload: Dict[str, Any],
        issue_key: str,
        trigger: Dict[str, str],
    ) -> str:
        header_id = self._header(request.headers, "X-Webhook-Id", "X-Request-Id", "X-Event-Id")
        if header_id:
            return str(header_id)
        pieces = [
            str(payload.get("webhookEvent") or payload.get("event") or ""),
            issue_key,
            trigger.get("type", ""),
            trigger.get("comment_id", ""),
            sha256_hex(request.raw),
        ]
        return sha256_hex("|".join(pieces).encode("utf-8"))

    def _build_prompt(
        self,
        payload: Dict[str, Any],
        issue_key: str,
        actor: Dict[str, str],
        trigger: Dict[str, str],
        event_id: str,
    ) -> str:
        issue = payload.get("issue") or {}
        fields = issue.get("fields") or {}
        summary = str(fields.get("summary") or "")
        compact = json.dumps(payload, indent=2, sort_keys=True)[: self.max_payload_chars]
        actor_text = actor.get("displayName") or actor.get("name") or actor.get("emailAddress") or actor.get("accountId") or "unknown"
        return (
            f"You are handling Atlassian Jira issue {issue_key} as a dedicated webhook-triggered session.\n"
            f"Session key should stay scoped to this issue. Latest webhook event id: {event_id}.\n\n"
            f"Issue summary: {summary}\n"
            f"Triggered by: {actor_text}\n"
            f"Trigger type: {trigger.get('type')}\n"
            f"Trigger text: {trigger.get('text', '')}\n\n"
            "Required workflow:\n"
            "1. Use the atlassian-itsm skill and Atlassian MCP tools for Jira/Confluence reads or writes.\n"
            f"2. Read the current Jira issue using jira_get_issue for {issue_key}; read comments when useful.\n"
            "3. Decide whether the request is clear and safe.\n"
            "4. For write actions, follow the atlassian-itsm skill rules: read-before-write and human confirmation when required.\n"
            f"5. If the trigger is a @netclaw comment, add one concise Jira comment back to {issue_key} with result or blocker.\n"
            "6. If blocked, state the missing access/input/approval directly.\n\n"
            "Raw Atlassian webhook payload JSON for context:\n"
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
