#!/usr/bin/env python3
import argparse
import json
import os
import sys
from typing import Any, Dict, Optional

import requests


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def require_env(name: str) -> str:
    value = env(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


class VikunjaClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        if not self.base_url.endswith("/api/v1"):
            self.base_url = f"{self.base_url}/api/v1"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, f"{self.base_url}{path}", timeout=30, **kwargs)
        if response.status_code >= 400:
            detail = response.text.strip()
            raise SystemExit(f"Vikunja API error {response.status_code} for {method} {path}: {detail}")
        if not response.text:
            return None
        return response.json()

    def list_projects(self) -> Any:
        return self.request("GET", "/projects")

    def get_project(self, project_id: int) -> Any:
        return self.request("GET", f"/projects/{project_id}")

    def list_project_users(self, project_id: int) -> Any:
        return self.request("GET", f"/projects/{project_id}/users")

    def create_task(self, project_id: int, payload: Dict[str, Any]) -> Any:
        return self.request("PUT", f"/projects/{project_id}/tasks", data=json.dumps(payload))

    def create_comment(self, task_id: int, comment: str) -> Any:
        return self.request("PUT", f"/tasks/{task_id}/comments", data=json.dumps({"comment": comment}))

    def list_comments(self, task_id: int) -> Any:
        return self.request("GET", f"/tasks/{task_id}/comments")

    def get_task(self, task_id: int) -> Any:
        return self.request("GET", f"/tasks/{task_id}")

    def update_task(self, task_id: int, payload: Dict[str, Any]) -> Any:
        return self.request("POST", f"/tasks/{task_id}", data=json.dumps(payload))

    def list_tasks(self, project_id: int) -> Any:
        return self.request("GET", f"/projects/{project_id}/tasks")

    def list_task_assignees(self, task_id: int) -> Any:
        return self.request("GET", f"/tasks/{task_id}/assignees")

    def set_task_assignees(self, task_id: int, assignees: list[dict[str, Any]]) -> Any:
        return self.request("POST", f"/tasks/{task_id}/assignees/bulk", data=json.dumps({"assignees": assignees}))


PRIORITY_MAP = {
    "unset": 0,
    "low": 1,
    "medium": 3,
    "high": 4,
    "urgent": 5,
}


def load_json_arg(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON: {exc}") from exc


def coerce_due(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value


def resolve_project_id(client: VikunjaClient, project_id: Optional[int], project_name: Optional[str]) -> int:
    if project_id:
        return project_id
    if not project_name:
        default_id = env("VIKUNJA_DEFAULT_PROJECT_ID")
        if default_id:
            return int(default_id)
        raise SystemExit("Provide --project-id, --project-name, or VIKUNJA_DEFAULT_PROJECT_ID")

    projects = client.list_projects()
    normalized = project_name.casefold()
    exact = [p for p in projects if str(p.get("title", "")).casefold() == normalized]
    if len(exact) == 1:
        return int(exact[0]["id"])
    contains = [p for p in projects if normalized in str(p.get("title", "")).casefold()]
    matches = exact or contains
    if len(matches) == 1:
        return int(matches[0]["id"])
    if not matches:
        raise SystemExit(f"No Vikunja project matched '{project_name}'")
    raise SystemExit(
        "Multiple Vikunja projects matched '%s': %s"
        % (project_name, ", ".join(f"{p['id']}:{p.get('title','')}" for p in matches[:10]))
    )


def cmd_list_projects(client: VikunjaClient, args: argparse.Namespace) -> None:
    projects = client.list_projects()
    if args.query:
        q = args.query.casefold()
        projects = [p for p in projects if q in str(p.get("title", "")).casefold()]
    print(json.dumps(projects, indent=2))


def cmd_create_task(client: VikunjaClient, args: argparse.Namespace) -> None:
    project_id = resolve_project_id(client, args.project_id, args.project_name)
    payload: Dict[str, Any] = {
        "title": args.title,
    }
    if args.description:
        payload["description"] = args.description
    if args.priority:
        payload["priority"] = PRIORITY_MAP[args.priority]
    if args.done:
        payload["done"] = True
    due = coerce_due(args.due_date)
    if due:
        payload["due_date"] = due
    if args.start_date:
        payload["start_date"] = args.start_date
    if args.end_date:
        payload["end_date"] = args.end_date
    if args.hex_color:
        payload["hex_color"] = args.hex_color
    if args.percent_done is not None:
        payload["percent_done"] = args.percent_done
    if args.extra_json:
        extra = load_json_arg(args.extra_json)
        if not isinstance(extra, dict):
            raise SystemExit("--extra-json must decode to a JSON object")
        payload.update(extra)
    created = client.create_task(project_id, payload)
    print(json.dumps(created, indent=2))


def cmd_comment_task(client: VikunjaClient, args: argparse.Namespace) -> None:
    created = client.create_comment(args.task_id, args.comment)
    print(json.dumps(created, indent=2))


def cmd_get_task(client: VikunjaClient, args: argparse.Namespace) -> None:
    task = client.get_task(args.task_id)
    print(json.dumps(task, indent=2))


def cmd_update_task(client: VikunjaClient, args: argparse.Namespace) -> None:
    payload: Dict[str, Any] = {}
    if args.title:
        payload["title"] = args.title
    if args.description:
        payload["description"] = args.description
    if args.priority:
        payload["priority"] = PRIORITY_MAP[args.priority]
    if args.done is not None:
        payload["done"] = args.done
    if args.percent_done is not None:
        payload["percent_done"] = args.percent_done
    if args.extra_json:
        extra = load_json_arg(args.extra_json)
        if not isinstance(extra, dict):
            raise SystemExit("--extra-json must decode to a JSON object")
        payload.update(extra)
    if not payload:
        raise SystemExit("Provide at least one field to update (--title, --description, --priority, --done, --percent-done, --extra-json)")
    updated = client.update_task(args.task_id, payload)
    print(json.dumps(updated, indent=2))


def cmd_list_tasks(client: VikunjaClient, args: argparse.Namespace) -> None:
    project_id = resolve_project_id(client, args.project_id, args.project_name)
    tasks = client.list_tasks(project_id)
    if args.query:
        q = args.query.casefold()
        tasks = [t for t in tasks if q in str(t.get("title", "")).casefold()]
    print(json.dumps(tasks, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal Vikunja API helper for tasks and comments")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list-projects", help="List projects visible to the token")
    p.add_argument("--query", help="Case-insensitive substring filter")
    p.set_defaults(func=cmd_list_projects)

    p = sub.add_parser("create-task", help="Create a task in a project")
    p.add_argument("title")
    p.add_argument("--project-id", type=int)
    p.add_argument("--project-name")
    p.add_argument("--description")
    p.add_argument("--priority", choices=sorted(PRIORITY_MAP.keys()))
    p.add_argument("--due-date", help="ISO-8601 timestamp, for example 2026-05-07T12:00:00Z")
    p.add_argument("--start-date", help="ISO-8601 timestamp")
    p.add_argument("--end-date", help="ISO-8601 timestamp")
    p.add_argument("--hex-color", help="Hex color like #ff9900")
    p.add_argument("--percent-done", type=float)
    p.add_argument("--done", action="store_true")
    p.add_argument("--extra-json", help="Raw JSON object merged into the task payload")
    p.set_defaults(func=cmd_create_task)

    p = sub.add_parser("comment-task", help="Create a task comment")
    p.add_argument("task_id", type=int)
    p.add_argument("comment")
    p.set_defaults(func=cmd_comment_task)

    p = sub.add_parser("get-task", help="Fetch a task by numeric id")
    p.add_argument("task_id", type=int)
    p.set_defaults(func=cmd_get_task)

    p = sub.add_parser("update-task", help="Update fields on an existing task")
    p.add_argument("task_id", type=int)
    p.add_argument("--title")
    p.add_argument("--description")
    p.add_argument("--priority", choices=sorted(PRIORITY_MAP.keys()))
    p.add_argument("--done", action="store_true", default=None)
    p.add_argument("--percent-done", type=float)
    p.add_argument("--extra-json", help="Raw JSON object merged into the update payload")
    p.set_defaults(func=cmd_update_task)

    p = sub.add_parser("list-tasks", help="List tasks in a project")
    p.add_argument("--project-id", type=int)
    p.add_argument("--project-name")
    p.add_argument("--query", help="Case-insensitive substring filter on title")
    p.set_defaults(func=cmd_list_tasks)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    client = VikunjaClient(require_env("VIKUNJA_URL"), require_env("VIKUNJA_TOKEN"))
    args.func(client, args)


if __name__ == "__main__":
    main()
