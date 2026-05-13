# Universal secure webhook listener for NetClaw

NetClaw uses a single universal webhook listener:

| Script | Purpose |
|--------|---------|
| `scripts/universal_webhook_listener.py` | Secure webhook gateway. Routes `/webhooks/<provider>` to provider adapters. |

The universal listener currently ships with:

| Provider | Endpoint | Behavior |
|----------|----------|----------|
| `vikunja` | `/webhooks/vikunja` | Handles Vikunja task assignment and `@netclaw` mention workflows. |
| `generic` | `/webhooks/generic` | Validates an HMAC-signed JSON webhook and dispatches a dedicated OpenClaw session. No outbound provider reply is attempted. |
| `atlassian` | `/webhooks/atlassian` | Handles Jira issue/comment webhooks and dispatches issue-scoped OpenClaw sessions that use Atlassian MCP tools. |

## Vikunja adapter

The Vikunja adapter acknowledges two trigger types:
- task assigned to `netclaw`
- comment created with `@netclaw`

It only responds when the event author is a **project admin**.

## Security model

Checks happen in this order:
1. Verify `X-Vikunja-Signature` when `VIKUNJA_WEBHOOK_SECRET` is set.
2. Ignore events created by `netclaw` itself.
3. Optionally enforce allowed project IDs via `VIKUNJA_ALLOWED_PROJECT_IDS`.
4. Require the event author to be a project admin:
   - project owner always counts as admin
   - otherwise the listener calls `GET /projects/{id}/users` and requires `permission == 2`
5. Enforce cooldown per task with `VIKUNJA_COOLDOWN_SECONDS`.
6. Record event fingerprints and ignore duplicates.

## Required token permissions

You can run the listener in two modes:

### Single-token mode
One token does everything. It must be able to:
- read projects
- read project users
- read tasks
- create task comments

```json
{
  "other": ["user", "users", "routes"],
  "projects": ["read_all", "read_one"],
  "projects_users": ["read_all"],
  "tasks": ["read_one"],
  "tasks_comments": ["create"]
}
```

### Split-token mode
Recommended when the bot token can comment but cannot inspect project permissions.

- `VIKUNJA_TOKEN` = bot token used to post comments as `netclaw`
- `VIKUNJA_ADMIN_TOKEN` = read-capable token used only to verify whether the triggering user is a project admin

If `projects_users.read_all` is missing from the token used for admin checks, strict admin verification will reject triggers.

## Dispatch modes

`VIKUNJA_DISPATCH_MODE` controls how the listener responds to a trigger:

| Mode | Behavior | When to use |
|------|----------|-------------|
| `agent` (default) | Posts an immediate ack comment, then launches a background `openclaw agent --local` run keyed to the Vikunja task id. The spawned task session can use real tools and post results back into the same task, then the listener reassigns the task back to the initiating user. | Primary mode for live NetBox / pyATS / skill-backed work. |
| `gemini` | Calls Gemini to generate a text reply from task context. **Cannot query live systems.** | Lightweight text-only replies when no live tooling is required. |

### Agent dispatch requirements

- `openclaw` CLI must be installed and on `PATH`.
- The listener environment must already carry the tool auth/env needed by the spawned local agent.
- Optional overrides:
  - `VIKUNJA_OPENCLAW_AGENT_ID` (default `main`)
  - `VIKUNJA_OPENCLAW_MODEL`

### Gemini mode limitation

In `gemini` mode the listener can only reply based on task text already in Vikunja. It **cannot** call any APIs or run commands.

## Environment

Universal listener settings:

```env
WEBHOOK_LISTEN_HOST=0.0.0.0
WEBHOOK_LISTEN_PORT=8787
WEBHOOK_ENABLED_PROVIDERS=vikunja,generic,atlassian
WEBHOOK_OPENCLAW_AGENT_ID=main
WEBHOOK_OPENCLAW_MODEL=
WEBHOOK_REPLAY_WINDOW_SECONDS=300
```

Atlassian webhook settings:

```env
ATLASSIAN_WEBHOOK_SECRET=replace-me
ATLASSIAN_WEBHOOK_REQUIRE_SECRET=true
ATLASSIAN_WEBHOOK_STATE_PATH=/root/.openclaw/workspace/state/atlassian-webhook-state.json
ATLASSIAN_WEBHOOK_ASYNC=true
ATLASSIAN_WEBHOOK_MAX_PAYLOAD_CHARS=16000
ATLASSIAN_NETCLAW_MENTION=@netclaw
ATLASSIAN_NETCLAW_IDENTITIES=netclaw,@netclaw
ATLASSIAN_TRIGGER_LABELS=netclaw-run,netclaw
ATLASSIAN_ALLOWED_PROJECT_KEYS=NET,OPS
ATLASSIAN_IGNORE_SELF_EVENTS=true
```

Generic signed webhook settings:

```env
GENERIC_WEBHOOK_SECRET=replace-me
GENERIC_WEBHOOK_REQUIRE_SECRET=true
GENERIC_WEBHOOK_STATE_PATH=/root/.openclaw/workspace/state/generic-webhook-state.json
GENERIC_WEBHOOK_ASYNC=true
GENERIC_WEBHOOK_MAX_PAYLOAD_CHARS=12000
```

Vikunja adapter settings:

```env
VIKUNJA_URL=http://vikunja.example.local:3456
VIKUNJA_TOKEN=...
VIKUNJA_ADMIN_TOKEN=...
VIKUNJA_NETCLAW_USERNAME=netclaw
VIKUNJA_WEBHOOK_SECRET=replace-me
VIKUNJA_ALLOWED_PROJECT_IDS=2,5
VIKUNJA_REQUIRE_PROJECT_ADMIN=true
VIKUNJA_STRICT_PROJECT_ADMIN=true
VIKUNJA_COOLDOWN_SECONDS=300
VIKUNJA_WEBHOOK_STATE_PATH=/root/.openclaw/workspace/state/vikunja-webhook-state.json
VIKUNJA_LISTEN_HOST=0.0.0.0
VIKUNJA_LISTEN_PORT=8787
VIKUNJA_AUTOREPLY_ENABLED=true
VIKUNJA_AUTOREPLY_MODEL=gemini-2.5-flash
VIKUNJA_AUTOREPLY_MAX_COMMENTS=12
VIKUNJA_AUTOREPLY_MAX_HISTORY=10
VIKUNJA_SESSION_STATE_DIR=/root/.openclaw/workspace/state/vikunja-task-sessions
VIKUNJA_DISPATCH_MODE=agent
VIKUNJA_OPENCLAW_AGENT_ID=main
VIKUNJA_OPENCLAW_MODEL=
GEMINI_API_KEY=...
```

## Run

From the skill directory, use the universal listener by default:

```bash
python3 scripts/universal_webhook_listener.py
```

Health endpoint:

```text
GET /healthz
```

Webhook endpoints:

```text
POST /webhooks/vikunja
POST /webhooks/generic
POST /webhooks/atlassian
```

## Persistent systemd service

This host runs the universal listener as:

```text
netclaw-universal-webhook.service
```

Installed unit path:

```text
/etc/systemd/system/netclaw-universal-webhook.service
```

Manage it with:

```bash
systemctl status netclaw-universal-webhook.service
systemctl restart netclaw-universal-webhook.service
systemctl stop netclaw-universal-webhook.service
systemctl start netclaw-universal-webhook.service
```

It is enabled at boot with:

```bash
systemctl enable netclaw-universal-webhook.service
```

The service loads environment from:

```text
/root/.openclaw/.env
```

Verify health with:

```bash
curl http://127.0.0.1:8787/healthz
```

## Vikunja webhook setup

Create a **project webhook** in each allowed project pointing to:

```text
http://<listener-host>:8787/webhooks/vikunja
```

Use the same secret in Vikunja and `VIKUNJA_WEBHOOK_SECRET`.

Subscribe to events that include:
- assignee created
- comment created

Depending on your Vikunja version, event names may look like `task.assignee.created` and `task.comment.created`. The listener matches by substring, so minor naming differences are tolerated.

For mentions, the listener accepts both plain-text `@netclaw` style mentions and Vikunja's HTML mention markup such as `data-id="netclaw"` / `data-label="netclaw"`.

## Atlassian webhook setup

Use this adapter when Jira should wake NetClaw automatically.

Endpoint:

```text
POST /webhooks/atlassian
```

Supported trigger patterns:

- Jira comment body contains `ATLASSIAN_NETCLAW_MENTION` — default `@netclaw`
- issue is assigned to one of `ATLASSIAN_NETCLAW_IDENTITIES`
- issue has one of `ATLASSIAN_TRIGGER_LABELS`

The adapter does not call Jira directly. It starts an OpenClaw session named:

```text
atlassian-issue-<ISSUE-KEY>
```

The spawned agent is instructed to use the `atlassian-itsm` skill and Atlassian MCP tools, for example `jira_get_issue`, `jira_get_issue_comments`, and `jira_add_comment`.

Recommended webhook security options:

```text
X-Webhook-Signature: sha256=<hex-hmac-sha256-of-raw-body>
X-Webhook-Timestamp: <unix-time>        # optional, checked when present
X-Webhook-Id: <stable-event-id>         # optional, recommended
```

If a Jira automation rule cannot generate HMAC, it may send a shared secret header instead:

```text
X-Atlassian-Webhook-Secret: <ATLASSIAN_WEBHOOK_SECRET>
```

HMAC is preferred. Keep `ATLASSIAN_WEBHOOK_REQUIRE_SECRET=true` for production.

Minimal Jira payload shape expected by the adapter:

```json
{
  "webhookEvent": "comment_created",
  "issue": {
    "key": "NET-123",
    "fields": {
      "summary": "BGP peer down",
      "labels": ["netclaw-run"]
    }
  },
  "comment": {
    "id": "10001",
    "body": "@netclaw check current status"
  },
  "user": {
    "displayName": "Alex"
  }
}
```

## Generic signed webhook setup

Use this adapter for systems that can send a JSON POST and HMAC-SHA256 signature.

Endpoint:

```text
POST /webhooks/generic
```

Headers:

```text
X-Webhook-Signature: sha256=<hex-hmac-sha256-of-raw-body>
X-Webhook-Timestamp: <unix-time>        # optional, checked when present
X-Webhook-Id: <stable-event-id>         # optional, recommended
```

Body should be JSON. Useful optional fields:

```json
{
  "event_id": "evt-123",
  "object_id": "change-456",
  "event_type": "change.requested",
  "actor": "alex",
  "summary": "Check this change request",
  "details": {}
}
```

The generic adapter creates/reuses an OpenClaw session named:

```text
webhook-generic-<object_id>
```

It is intentionally provider-neutral: it dispatches work into OpenClaw, but it does not post back to the originating system. Add a provider-specific adapter when two-way interaction is required.

## Cooldown and dedupe behavior

- The listener stores event fingerprints in a JSON state file.
- Re-delivery of the same payload is ignored.
- Repeated delivery of the same event is ignored.
- Each distinct mention comment is answered independently.
- In-flight replies are claimed atomically per event/comment to prevent duplicate replies during concurrent webhook delivery.

## Example comments

Assignment:
- `@alex, acknowledged. I’m on it and will post the results in this task shortly.`

Mention:
- `@alex, acknowledged. I’m on it and will post the results in this task shortly.`
- Follow-up result comment is then posted by the background OpenClaw task session with real findings or a concrete blocker.
- After the worker finishes, the listener reassigns the task back to `@alex`.
