#!/usr/bin/env python3
"""Vikunja provider adapter implementation for the universal webhook listener."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import threading
import time
from html import unescape
from http import HTTPStatus
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from vikunja_api import VikunjaClient, require_env
from webhook_core import StateStore, env, env_bool, env_int

class ListenerConfig:
    def __init__(self) -> None:
        self.listen_host = env("VIKUNJA_LISTEN_HOST", "0.0.0.0")
        self.listen_port = env_int("VIKUNJA_LISTEN_PORT", 8787)
        self.netclaw_username = env("VIKUNJA_NETCLAW_USERNAME", env("VIKUNJA_USERNAME", "netclaw"))
        self.webhook_secret = env("VIKUNJA_WEBHOOK_SECRET")
        self.cooldown_seconds = env_int("VIKUNJA_COOLDOWN_SECONDS", 300)
        self.allowed_project_ids = {
            int(x) for x in (env("VIKUNJA_ALLOWED_PROJECT_IDS", "") or "").split(",") if x.strip()
        }
        self.require_project_admin = env_bool("VIKUNJA_REQUIRE_PROJECT_ADMIN", True)
        self.strict_project_admin = env_bool("VIKUNJA_STRICT_PROJECT_ADMIN", True)
        self.state_path = env(
            "VIKUNJA_WEBHOOK_STATE_PATH",
            "/root/.openclaw/workspace/state/vikunja-webhook-state.json",
        )
        self.session_state_dir = env(
            "VIKUNJA_SESSION_STATE_DIR",
            "/root/.openclaw/workspace/state/vikunja-task-sessions",
        )
        self.autoreply_enabled = env_bool("VIKUNJA_AUTOREPLY_ENABLED", True)
        self.autoreply_model = env("VIKUNJA_AUTOREPLY_MODEL", "gemini-2.5-flash")
        self.autoreply_max_comments = env_int("VIKUNJA_AUTOREPLY_MAX_COMMENTS", 12)
        self.autoreply_max_history = env_int("VIKUNJA_AUTOREPLY_MAX_HISTORY", 10)
        self.dispatch_mode = env("VIKUNJA_DISPATCH_MODE", "agent")
        self.agent_id = env("VIKUNJA_OPENCLAW_AGENT_ID", "main")
        self.agent_model = env("VIKUNJA_OPENCLAW_MODEL")


class VikunjaAdapter:
    def __init__(self, config: ListenerConfig) -> None:
        self.config = config
        self.client = VikunjaClient(require_env("VIKUNJA_URL"), require_env("VIKUNJA_TOKEN"))
        admin_token = env("VIKUNJA_ADMIN_TOKEN")
        self.admin_client = (
            VikunjaClient(require_env("VIKUNJA_URL"), admin_token)
            if admin_token and admin_token != env("VIKUNJA_TOKEN")
            else self.client
        )
        self.state = StateStore(config.state_path)
        self.gemini_api_key = env("GEMINI_API_KEY")
        self.session_state_dir = Path(self.config.session_state_dir)

    def verify_signature(self, raw: bytes, signature: Optional[str]) -> bool:
        if not self.config.webhook_secret:
            return True
        if not signature:
            return False
        expected = hmac.new(
            self.config.webhook_secret.encode("utf-8"), raw, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def handle(self, raw: bytes, headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        signature = headers.get("X-Vikunja-Signature")
        if not self.verify_signature(raw, signature):
            return HTTPStatus.FORBIDDEN, {"status": "rejected", "reason": "invalid_signature"}

        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return HTTPStatus.BAD_REQUEST, {"status": "rejected", "reason": "invalid_json"}

        event_id = self._event_id(raw, payload)
        now = time.time()
        if self.state.seen_event(event_id):
            return HTTPStatus.OK, {"status": "ignored", "reason": "duplicate_event"}

        action = self._extract_action(payload)
        if action is None:
            self.state.mark_event(event_id, now)
            return HTTPStatus.OK, {"status": "ignored", "reason": "unsupported_event"}

        task_id, project_id, author, trigger_text, claim_key = action
        if author == self.config.netclaw_username:
            self.state.mark_event(event_id, now)
            return HTTPStatus.OK, {"status": "ignored", "reason": "self_event"}

        if self.config.allowed_project_ids and project_id not in self.config.allowed_project_ids:
            self.state.mark_event(event_id, now)
            return HTTPStatus.OK, {"status": "ignored", "reason": "project_not_allowed"}

        if self.config.require_project_admin and not self._is_project_admin(project_id, author):
            self.state.mark_event(event_id, now)
            return HTTPStatus.OK, {"status": "ignored", "reason": "author_not_project_admin"}

        if not self.state.claim_action(claim_key, now):
            self.state.mark_event(event_id, now)
            return HTTPStatus.OK, {"status": "ignored", "reason": "duplicate_inflight"}

        try:
            self._append_session_history(task_id, f"user:@{author}", self._strip_html(trigger_text))
            comment = self._build_reply(task_id, author, trigger_text)
            self.client.create_comment(task_id, comment)
            self._append_session_history(task_id, f"assistant:@{self.config.netclaw_username}", comment)
            if self.config.dispatch_mode == "agent":
                thread = threading.Thread(
                    target=self._run_agent_job,
                    args=(task_id, project_id, author, trigger_text),
                    daemon=True,
                )
                thread.start()
            self.state.mark_event(event_id, now)
            return HTTPStatus.CREATED, {"status": "commented", "task_id": task_id, "project_id": project_id}
        except Exception as exc:
            self.state.release_action(claim_key)
            self.state.mark_event(event_id, now)
            return HTTPStatus.INTERNAL_SERVER_ERROR, {
                "status": "error",
                "reason": "reply_failed",
                "detail": str(exc),
            }

    def _event_id(self, raw: bytes, payload: Dict[str, Any]) -> str:
        text = "|".join(
            [
                str(payload.get("event_name", "")),
                str(payload.get("time", "")),
                hashlib.sha256(raw).hexdigest(),
            ]
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _strip_html(self, value: str) -> str:
        text = value.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
        out: List[str] = []
        in_tag = False
        for ch in text:
            if ch == "<":
                in_tag = True
                continue
            if ch == ">":
                in_tag = False
                continue
            if not in_tag:
                out.append(ch)
        return " ".join(unescape("".join(out)).split())

    def _session_path(self, task_id: int) -> Path:
        return self.session_state_dir / f"task-{task_id}.json"

    def _load_session(self, task_id: int) -> Dict[str, Any]:
        path = self._session_path(task_id)
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                pass
        return {"task_id": task_id, "history": []}

    def _save_session(self, task_id: int, data: Dict[str, Any]) -> None:
        path = self._session_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def _append_session_history(self, task_id: int, role: str, text: str) -> None:
        session = self._load_session(task_id)
        history = session.get("history") or []
        history.append({"role": role, "text": text, "time": int(time.time())})
        session["history"] = history[-self.config.autoreply_max_history :]
        self._save_session(task_id, session)

    def _recent_task_context(self, task_id: int) -> Dict[str, Any]:
        reader = self.admin_client
        task = reader.get_task(task_id) or {}
        comments = reader.list_comments(task_id) or []
        comments = comments[-self.config.autoreply_max_comments :]
        session = self._load_session(task_id)
        return {"task": task, "comments": comments, "session": session}

    def _generate_gemini_reply(self, task_id: int, author: str, trigger_text: str) -> str:
        if not self.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        context = self._recent_task_context(task_id)
        task = context["task"]
        comments = context["comments"]
        session = context.get("session") or {"history": []}
        lines = []
        for item in comments:
            body = self._strip_html(str(item.get("comment", "")))
            user = ((item.get("author") or {}).get("username") or "unknown")
            lines.append(f"- {user}: {body}")
        session_lines = []
        for item in (session.get("history") or [])[-self.config.autoreply_max_history :]:
            session_lines.append(f"- {item.get('role')}: {item.get('text')}" )
        prompt = (
            "You are NetClaw, a precise senior network engineer replying inside Vikunja. "
            "Each Vikunja task is its own separate conversation session. "
            "Write one concise task comment reply. Be technical and helpful. "
            "Use only the task context below. Do not claim checks you did not perform. "
            "If you lack enough data, say exactly what is missing and what you can check next. "
            "Do not use markdown tables.\n\n"
            "STRICT RULES — you must follow these:\n"
            "- You are a text-only reply bot. You CANNOT query any APIs, run commands, or access live data.\n"
            "- NEVER say 'I am processing', 'I am querying', 'still processing', 'I will retrieve', 'I am working on it', or any variation that implies active execution.\n"
            "- If the user asks for live data (Netbox, pyATS, SNMP, device state, IP allocations, etc.), reply with an honest blocker that this webhook reply mode cannot perform live queries and ask for the exact scope or for a full agent run.\n"
            "- Only answer questions that can be answered from the task context provided below.\n\n"
            f"Task id: {task_id}\n"
            f"Task title: {task.get('title', '')}\n"
            f"Task description: {self._strip_html(str(task.get('description', '')))}\n"
            f"Triggered by: @{author}\n"
            f"Trigger text: {self._strip_html(trigger_text)}\n"
            "Per-task session history:\n"
            + ("\n".join(session_lines) if session_lines else "- none")
            + "\n\nRecent task comments:\n"
            + ("\n".join(lines) if lines else "- none")
            + "\n\nReply as NetClaw in 2-6 sentences. Follow strict rules above."
        )
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.config.autoreply_model}:generateContent?key={self.gemini_api_key}"
        )
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        last_error = None
        for delay in (0, 2, 5):
            if delay:
                time.sleep(delay)
            try:
                response = requests.post(url, json=payload, timeout=45)
                response.raise_for_status()
                data = response.json()
                for candidate in data.get("candidates") or []:
                    content = candidate.get("content") or {}
                    for part in content.get("parts") or []:
                        text = part.get("text")
                        if text and text.strip():
                            return text.strip()
                last_error = RuntimeError("No text returned from Gemini")
            except Exception as exc:
                last_error = exc
        raise RuntimeError(str(last_error) if last_error else "Gemini request failed")

    def _handoff_task(self, task_id: int, project_id: int, author: str) -> None:
        try:
            project = self.admin_client.get_project(project_id) or {}
        except Exception:
            project = {}
        owner = (project.get("owner") or {})
        if owner.get("username") == author:
            target = owner
        else:
            try:
                project_users = self.admin_client.list_project_users(project_id) or []
            except Exception:
                return
            target = next((u for u in project_users if u.get("username") == author), None)
        if not target:
            return
        try:
            self.admin_client.set_task_assignees(task_id, [target])
        except Exception:
            return

    def _run_agent_job(self, task_id: int, project_id: int, author: str, trigger_text: str) -> None:
        skill_dir = Path(__file__).parent
        api_script = skill_dir / "vikunja_api.py"
        request_text = self._strip_html(trigger_text)
        session_id = f"vikunja-task-{task_id}"
        before_comments = self.admin_client.list_comments(task_id) or []
        before_ids = {int(item.get('id')) for item in before_comments if item.get('id') is not None}
        prompt = (
            f"You are handling Vikunja task {task_id} as a dedicated per-task session. "
            f"The latest mention came from @{author}.\n\n"
            f"Required workflow:\n"
            f"1. Read full task context using: python3 {api_script} get-task {task_id}\n"
            f"2. Read recent task comments using the Vikunja API helper or direct API calls if needed.\n"
            f"3. Determine the right skill(s) and tool(s) for the user's request.\n"
            f"4. Gather real data. Use live tools when needed (NetBox, pyATS, web, shell, etc.).\n"
            f"5. Post exactly one concise result comment back to the same Vikunja task using: python3 {api_script} comment-task {task_id} '<comment>'\n\n"
            f"Rules:\n"
            f"- Do real work; do not post placeholder text.\n"
            f"- If blocked, post a concise blocker with the missing access/input.\n"
            f"- Keep the final task comment technical and concise.\n"
            f"- Latest trigger text: {request_text}\n"
        )
        command = [
            "openclaw", "agent", "--local", "--agent", self.config.agent_id,
            "--session-id", session_id,
            "--message", prompt,
            "--json", "--timeout", "600"
        ]
        if self.config.agent_model:
            command.extend(["--model", self.config.agent_model])
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
            self.client.create_comment(
                task_id,
                f"Blocked: failed to launch OpenClaw agent for this task. Detail: {exc}"
            )
            self._handoff_task(task_id, project_id, author)
            return
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "agent run failed").strip().replace("\n", " ")[:800]
            self.client.create_comment(
                task_id,
                f"Blocked: task agent run failed. Detail: {detail}"
            )
            self._handoff_task(task_id, project_id, author)
            return
        self._append_session_history(task_id, f"worker:@{self.config.netclaw_username}", f"openclaw-session:{session_id}")
        try:
            after_comments = self.admin_client.list_comments(task_id) or []
            new_netclaw = [
                item for item in after_comments
                if int(item.get('id', 0)) not in before_ids
                and ((item.get('author') or {}).get('username') == self.config.netclaw_username)
            ]
        except Exception:
            new_netclaw = []
        if new_netclaw:
            self._handoff_task(task_id, project_id, author)
            return
        fallback = None
        try:
            data = json.loads(proc.stdout or "{}")
            payloads = data.get('payloads') or []
            texts = [p.get('text', '').strip() for p in payloads if p.get('text', '').strip()]
            if texts:
                fallback = "\n\n".join(texts)[:4000]
        except Exception:
            fallback = None
        if fallback:
            self.client.create_comment(task_id, fallback)
        else:
            self.client.create_comment(task_id, "Update: task agent completed but did not return a usable task comment. Please mention @netclaw again if you want me to retry.")
        self._handoff_task(task_id, project_id, author)

    def _fallback_reply(self, author: str, trigger_text: str) -> str:
        text = self._strip_html(trigger_text)
        return (
            f"@{author}, I saw your mention and I am tracking this task as its own session. "
            f"I could not complete the full generated reply just now, but I captured your request: '{text}'. "
            "Please mention me again in a moment if you want an immediate retrigger."
        )

    def _build_reply(self, task_id: int, author: str, trigger_text: str) -> str:
        if self.config.dispatch_mode == "agent":
            return (
                f"@{author}, acknowledged. I’m on it and will post the results in this task shortly."
            )
        if self.config.dispatch_mode == "gemini":
            if self.config.autoreply_enabled:
                try:
                    return self._generate_gemini_reply(task_id, author, trigger_text)
                except Exception:
                    return self._fallback_reply(author, trigger_text)
        return (
            f"Triggered by @{author}. I saw the @{self.config.netclaw_username} mention and I'm on it. "
            "Automatic full replies are currently disabled in the listener."
        )

    def _extract_action(self, payload: Dict[str, Any]) -> Optional[Tuple[int, int, str, str, str]]:
        event_name = str(payload.get("event_name", ""))
        data = payload.get("data") or {}
        task = data.get("task") or {}
        task_id = task.get("id")
        project_id = task.get("project_id")
        doer = (data.get("doer") or {}).get("username")
        if not task_id or not project_id or not doer:
            return None

        lowered = event_name.lower()
        if "assignee" in lowered and "create" in lowered:
            assignee = data.get("assignee") or data.get("user") or {}
            if assignee.get("username") != self.config.netclaw_username:
                return None
            trigger_text = (
                f"Task was assigned to @{self.config.netclaw_username} by @{doer}."
            )
            assignee_id = assignee.get("id") or "unknown"
            claim_key = f"assignee:{task_id}:{assignee_id}:{payload.get('time','')}"
            return int(task_id), int(project_id), str(doer), trigger_text, claim_key

        if "comment" in lowered and "create" in lowered:
            comment_obj = data.get("comment") or {}
            text = str(comment_obj.get("comment", ""))
            lowered_text = text.lower()
            mention = f"@{self.config.netclaw_username}".lower()
            html_id = f'data-id="{self.config.netclaw_username}"'.lower()
            html_label = f'data-label="{self.config.netclaw_username}"'.lower()
            if mention not in lowered_text and html_id not in lowered_text and html_label not in lowered_text:
                return None
            comment_id = comment_obj.get("id") or hashlib.sha256(text.encode("utf-8")).hexdigest()
            claim_key = f"comment:{task_id}:{comment_id}"
            return int(task_id), int(project_id), str(doer), text, claim_key

        return None

    def _is_project_admin(self, project_id: int, username: str) -> bool:
        project = self.admin_client.get_project(project_id)
        owner = ((project or {}).get("owner") or {}).get("username")
        if owner == username:
            return True
        try:
            users = self.admin_client.list_project_users(project_id) or []
        except SystemExit:
            if self.config.strict_project_admin:
                return False
            return False
        for user in users:
            if user.get("username") == username and int(user.get("permission", 0)) == 2:
                return True
        return False

