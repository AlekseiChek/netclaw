---
name: vikunja-task-tracker
description: Create and update Vikunja tasks through the Vikunja HTTP API. Use when the user wants to create a task, add a task comment, log that the agent was triggered, or mention a person in a Vikunja task comment.
---

# Vikunja Task Tracker

Use this skill to write tasks and comments into Vikunja with a small local helper script.

## Quick start

1. Confirm the write target.
   - For task creation: project name or project ID.
   - For comments: numeric task ID.
2. Check required environment.
   - `VIKUNJA_URL`
   - `VIKUNJA_TOKEN`
   - optional `VIKUNJA_DEFAULT_PROJECT_ID`
3. If you need endpoint or payload details, read `references/api.md`.
4. For automatic reactions to assignments or mentions, read `references/webhooks.md`.
5. Run `scripts/vikunja_api.py` or, for automatic inbound triggers, `scripts/universal_webhook_listener.py`.
6. Return the created task/comment ID or listener result and a short summary.

## Supported operations

### Automatic webhook reactions

Use when Vikunja should wake NetClaw automatically after task assignment or a `@netclaw` mention.

The bundled universal listener:
- accepts Vikunja webhooks on `/webhooks/vikunja`
- validates the shared secret when configured
- accepts both plain-text and HTML-rendered Vikunja mentions
- keeps separate per-task session history under local state
- posts an immediate acknowledgement comment
- launches a background OpenClaw task session per Vikunja task for live tool-backed work
- hands the task back to the initiating user after the worker finishes
- can fall back to text-only Gemini mode when configured
- ignores self-generated events
- only reacts when the event author is a **project admin**
- applies persistent event dedupe
- replies to each distinct mention while suppressing duplicate concurrent deliveries

Run:

```bash
python3 scripts/universal_webhook_listener.py
```

Read `references/webhooks.md` before deployment because the listener requires specific token permissions and webhook settings.


### Create a task

Use when the user asks to open, add, track, capture, or remember work in Vikunja.

Example:

```bash
python3 scripts/vikunja_api.py create-task \
  "Audit OSPF neighbors in lab" \
  --project-name "Network Ops" \
  --description "Triggered from Telegram after topology review." \
  --priority high \
  --due-date 2026-05-07T12:00:00Z
```

Rules:
- Require an explicit project target unless `VIKUNJA_DEFAULT_PROJECT_ID` is set.
- Do not guess project names when multiple matches exist.
- Keep titles short; put operational detail in the description.

### Add a comment to a task

Use when the user asks to update a task, log progress, note that the agent was triggered, or mention someone.

Example:

```bash
python3 scripts/vikunja_api.py comment-task \
  123 \
  "Triggered by @alex in Telegram. Captured the current finding and waiting for next action."
```

Rules:
- Mention people as `@username` only when the exact Vikunja username is known.
- Say why the comment exists: triggered by user request, follow-up update, completion note, blocker, etc.
- Prefer one concise comment that contains status, evidence, and next step.

## Comment patterns

Use one of these patterns and adapt it to the request:

- Triggered log:
  - `Triggered by @username in <channel>. Working on: <summary>.`
- Progress update:
  - `Update: <what changed>. Evidence: <short evidence>. Next: <next action>.`
- Completion:
  - `Completed. Result: <outcome>. Verification: <how verified>.`
- Blocker:
  - `Blocked. Need: <missing access/decision/input>.`

## Validation

Before any write:
- Make sure the user asked for the write.
- Make sure the project or task is unambiguous.
- Make sure mentions use known Vikunja usernames.

After any write:
- Report the returned task/comment ID.
- Quote the project or task the write landed on.

## Script reference

The bundled helper supports:

```bash
python3 scripts/vikunja_api.py list-projects [--query TEXT]
python3 scripts/vikunja_api.py create-task TITLE [--project-id ID | --project-name NAME] [--description TEXT] [--priority unset|low|medium|high|urgent] [--due-date ISO8601] [--start-date ISO8601] [--end-date ISO8601] [--hex-color #RRGGBB] [--percent-done N] [--done] [--extra-json JSON]
python3 scripts/vikunja_api.py get-task TASK_ID
python3 scripts/vikunja_api.py comment-task TASK_ID COMMENT
python3 scripts/universal_webhook_listener.py
```

If the user wants fields the helper does not expose directly, pass them with `--extra-json` after confirming the payload shape in `references/api.md`.

### Universal webhook providers

The universal listener supports multiple inbound providers:

| Provider | Endpoint | Use |
|----------|----------|-----|
| `vikunja` | `/webhooks/vikunja` | Vikunja task assignment and `@netclaw` mentions |
| `atlassian` | `/webhooks/atlassian` | Jira issue/comment events that mention, assign, or label work for NetClaw |
| `generic` | `/webhooks/generic` | HMAC-signed JSON events from systems without a dedicated adapter |

For Atlassian webhooks, configure Jira to send issue/comment events to `/webhooks/atlassian`. The adapter creates an issue-scoped OpenClaw session named `atlassian-issue-<ISSUE-KEY>` and the spawned worker should use the `atlassian-itsm` skill plus Atlassian MCP tools for Jira/Confluence reads or writes.
