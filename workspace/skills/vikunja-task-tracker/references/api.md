# Vikunja API quick reference

Use API tokens unless the user explicitly wants username/password login.

## Required environment

- `VIKUNJA_URL` — base URL of the Vikunja instance, with or without `/api/v1`
- `VIKUNJA_TOKEN` — API token
- `VIKUNJA_DEFAULT_PROJECT_ID` — optional fallback project ID for task creation

## Endpoints used by this skill

- `GET /projects` — list visible projects
- `PUT /projects/{id}/tasks` — create a task
- `GET /tasks/{task_id}` — fetch one task
- `PUT /tasks/{taskID}/comments` — add a task comment

## Payload notes

### Create task
Safe default payload:

```json
{
  "title": "Replace access switch uplink optics",
  "description": "Context, scope, and next step.",
  "priority": 4,
  "due_date": "2026-05-07T12:00:00Z"
}
```

Common fields:
- `title` (required)
- `description`
- `priority` (`low=1`, `medium=3`, `high=4`, `urgent=5` in this skill helper)
- `due_date`, `start_date`, `end_date` — ISO-8601 timestamps
- `done`
- `hex_color`
- `percent_done`

### Create comment
```json
{
  "comment": "Triggered by @alex after router maintenance review."
}
```

Mentions are plain text `@username` inside the comment body.

## Operational guidance

- Resolve ambiguity before writing. If multiple projects match the same name, stop and ask.
- Prefer a single well-formed comment over several short comments.
- When acting because the agent was explicitly asked to create/update a task, say that plainly in the comment.
- When mentioning people, use the exact Vikunja username if known. If not known, ask once instead of guessing.
