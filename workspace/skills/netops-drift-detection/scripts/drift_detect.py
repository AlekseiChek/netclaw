#!/usr/bin/env python3
"""Token-efficient NetBox vs Oxidized drift detector.

Outputs compact JSON or TOON-like text. Creates one Vikunja task when drift exists
and task-manager env is configured.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    print("missing dependency: requests", file=sys.stderr)
    sys.exit(2)

DEFAULT_REPO = ""
DEFAULT_OXIDIZED_URL = ""
DEFAULT_CACHE = Path.home() / ".cache" / "netclaw" / "oxidized-network-configs"
VIKUNJA_HELPER = Path.home() / ".openclaw" / "workspace" / "skills" / "vikunja-task-tracker" / "scripts" / "vikunja_api.py"
SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
TASK_TITLES = {
    "HOSTNAME_MISMATCH": "Drift detected on {device}",
    "MGMT_IP_MISMATCH": "Drift detected on {device}",
    "INTERFACE_DESCRIPTION_MISMATCH": "Drift detected on {device}",
    "NETBOX_ACTIVE_NOT_IN_OXIDIZED": "Drift detected on {device}",
    "OXIDIZED_NOT_ACTIVE_IN_NETBOX": "Verify device decommissioning",
    "OXIDIZED_BACKUP_FAILED": "Oxidized failed to collect config from {device}",
    "NETBOX_DEVICE_MISSING_FIELDS": "Device has no platform / mgmt IP / role",
    "CONFIG_CHANGED_OUTSIDE_PIPELINE": "Manual config change detected",
}


def norm_name(v: str | None) -> str:
    return (v or "").strip().lower()


def host_ip(v: str | None) -> str:
    if not v:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    # NetBox may return nested primary_ip object, handled before this.
    try:
        return str(ipaddress.ip_interface(s).ip)
    except ValueError:
        return s.split("/")[0]


IFACE_PREFIXES = {
    "gigabitethernet": "gi",
    "gige": "gi",
    "gi": "gi",
    "fastethernet": "fa",
    "fa": "fa",
    "tengigabitethernet": "te",
    "tengige": "te",
    "te": "te",
    "twentyfivegige": "twe",
    "twentyfivegigabitethernet": "twe",
    "hundredgige": "hu",
    "hundredgigabitethernet": "hu",
    "ethernet": "eth",
    "eth": "eth",
    "loopback": "lo",
    "lo": "lo",
    "vlan": "vlan",
    "port-channel": "po",
    "portchannel": "po",
    "po": "po",
}


def norm_iface_name(name: str | None) -> str:
    """Normalize common NetBox/vendor interface spellings for description checks."""
    s = (name or "").strip().lower().replace(" ", "")
    if not s:
        return ""
    s = s.replace("_", "-")
    m = re.match(r"^([a-z-]+)([0-9].*)$", s)
    if not m:
        return s
    prefix, suffix = m.groups()
    return IFACE_PREFIXES.get(prefix, prefix) + suffix


def norm_desc(desc: str | None) -> str:
    """Normalize description text enough to avoid whitespace-only false positives."""
    return re.sub(r"\s+", " ", (desc or "").strip())


def nb_get_all(base: str, token: str, endpoint: str, params: dict | None = None) -> list[dict]:
    sess = requests.Session()
    sess.headers.update({"Authorization": f"Token {token}", "Accept": "application/json"})
    url = base.rstrip("/") + "/api/" + endpoint.lstrip("/")
    out: list[dict] = []
    params = dict(params or {})
    params.setdefault("limit", 1000)
    while url:
        r = sess.get(url, params=params, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"NetBox GET {endpoint} failed: HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        if isinstance(data, list):
            return data
        out.extend(data.get("results", []))
        url = data.get("next")
        params = None
    return out


def active_status(value) -> bool:
    if isinstance(value, dict):
        value = value.get("value") or value.get("label")
    return str(value).lower() == "active"


def clone_or_pull(repo_url: str, cache: Path) -> Path:
    cache.parent.mkdir(parents=True, exist_ok=True)
    if (cache / ".git").exists():
        subprocess.run(["git", "-C", str(cache), "pull", "--ff-only", "--quiet"], check=True)
    else:
        if cache.exists():
            shutil.rmtree(cache)
        subprocess.run(["git", "clone", "--quiet", repo_url, str(cache)], check=True)
    return cache


def parse_config(path: Path) -> dict:
    text = path.read_text(errors="ignore")
    hostname = ""
    mgmt_ips: set[str] = set()
    ifaces: dict[str, dict] = defaultdict(dict)

    # IOS/NX-OS/Junos-ish flat hostname
    m = re.search(r"(?m)^\s*hostname\s+['\"]?([^\s'\"]+)", text)
    if m:
        hostname = m.group(1)
    # VyOS command format
    m = re.search(r"(?m)^set\s+system\s+host-name\s+'?([^'\s]+)'?", text)
    if m:
        hostname = m.group(1)

    # IOS-style interface blocks
    for im in re.finditer(r"(?ms)^interface\s+(\S+)\n(.*?)(?=^!\n|^interface\s+|\Z)", text):
        name, body = im.group(1), im.group(2)
        dm = re.search(r"(?m)^\s*description\s+(.+?)\s*$", body)
        if dm:
            ifaces[name]["description"] = norm_desc(dm.group(1))
            ifaces[name]["name"] = name
        for ipm in re.finditer(r"(?m)^\s*ip address\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)", body):
            mgmt_ips.add(ipm.group(1))

    # VyOS set commands
    for vm in re.finditer(r"(?m)^set\s+interfaces\s+\S+\s+(\S+)\s+description\s+'([^']*)'", text):
        ifaces[vm.group(1)]["description"] = norm_desc(vm.group(2))
        ifaces[vm.group(1)]["name"] = vm.group(1)
    for vm in re.finditer(r"(?m)^set\s+interfaces\s+\S+\s+(\S+)\s+address\s+'?([^'\s]+)'?", text):
        ip = host_ip(vm.group(2))
        if ip:
            mgmt_ips.add(ip)

    # Common management hints: mgmt interface, loopback, or source-interface IPs are not reliably knowable
    # from vendor-neutral text, so keep all parsed interface IPs and compare primary IP membership.
    return {
        "file": str(path),
        "backup_id": path.name,
        "hostname": hostname or path.name,
        "mgmt_ips": sorted(mgmt_ips),
        "interfaces": {k: v for k, v in ifaces.items() if v},
    }


def load_oxidized(cache: Path) -> list[dict]:
    ignored = {".git"}
    files = [p for p in cache.rglob("*") if p.is_file() and not any(part in ignored for part in p.parts)]
    # Oxidized repos usually store one config per file without extension. Keep all text-like small files.
    configs = []
    for p in files:
        if p.name.startswith(".") or p.suffix in {".idx", ".pack", ".sample"}:
            continue
        try:
            if p.stat().st_size == 0 or p.stat().st_size > 2_000_000:
                continue
            configs.append(parse_config(p))
        except UnicodeDecodeError:
            continue
    return configs


def nb_primary_ip(device: dict) -> str:
    for key in ("primary_ip4", "primary_ip6", "primary_ip"):
        val = device.get(key)
        if isinstance(val, dict):
            return host_ip(val.get("address") or val.get("display"))
        if val:
            return host_ip(val)
    return ""


def nb_device_slug(device: dict) -> str:
    return device.get("name") or device.get("display") or str(device.get("id"))


def compact_nb_devices(raw: list[dict]) -> list[dict]:
    out = []
    for d in raw:
        if not active_status(d.get("status")):
            continue
        out.append({
            "id": d.get("id"),
            "name": nb_device_slug(d),
            "primary_ip": nb_primary_ip(d),
            "site": (d.get("site") or {}).get("name") or (d.get("site") or {}).get("display") or "",
            "platform": (d.get("platform") or {}).get("name") or (d.get("platform") or {}).get("display") or "",
            "role": (d.get("role") or d.get("device_role") or {}).get("name") or (d.get("role") or d.get("device_role") or {}).get("display") or "",
            "url": d.get("url") or "",
        })
    return out


def compact_nb_interfaces(raw: list[dict]) -> dict[str, dict[str, str]]:
    by_dev: dict[str, dict[str, str]] = defaultdict(dict)
    for i in raw:
        dev = i.get("device") or {}
        dev_name = dev.get("name") or dev.get("display")
        if not dev_name:
            continue
        ifname = i.get("name") or i.get("display")
        if not ifname:
            continue
        # Keep empty descriptions too. A backup-side description with no NetBox
        # description is still drift when NetBox is the source of truth.
        by_dev[dev_name][ifname] = norm_desc(i.get("description"))
    return by_dev


def backup_iface_descriptions(backup: dict) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for raw_name, attrs in (backup.get("interfaces") or {}).items():
        desc = norm_desc((attrs or {}).get("description"))
        if desc:
            out[norm_iface_name(raw_name)] = {"name": (attrs or {}).get("name") or raw_name, "description": desc}
    return out


def find_match(nb: dict, backups: list[dict], used: set[int]) -> int | None:
    nb_name = norm_name(nb["name"])
    nb_ip = nb.get("primary_ip") or ""
    for idx, b in enumerate(backups):
        if idx in used:
            continue
        if nb_name and nb_name in {norm_name(b.get("backup_id")), norm_name(b.get("hostname"))}:
            return idx
    if nb_ip:
        for idx, b in enumerate(backups):
            if idx not in used and nb_ip in b.get("mgmt_ips", []):
                return idx
    return None


def finding(code, severity, device, detail, **kw):
    d = {"severity": severity, "code": code, "device": device, "detail": detail}
    d.update({k: v for k, v in kw.items() if v not in (None, "", [], {})})
    return d


def detect(nb_devices: list[dict], nb_ifaces: dict[str, dict[str, str]], backups: list[dict], ox_nodes: list[dict] | None = None, cache: Path | None = None, pipeline_author_pattern: str | None = None) -> list[dict]:
    findings = []
    ox_by_name = {norm_name((n.get("full_name") or n.get("name"))): n for n in (ox_nodes or [])}
    ox_by_short = {norm_name(n.get("name")): n for n in (ox_nodes or [])}
    for nb in nb_devices:
        missing = [field for field in ("platform", "primary_ip", "role") if not nb.get(field)]
        if missing:
            findings.append(finding("NETBOX_DEVICE_MISSING_FIELDS", "MEDIUM", nb["name"], "active NetBox device is missing required fields", missing=missing, site=nb.get("site"), netbox_device=nb.get("url")))
    used: set[int] = set()
    matches: dict[str, dict] = {}
    for nb in nb_devices:
        idx = find_match(nb, backups, used)
        if idx is None:
            findings.append(finding("NETBOX_ACTIVE_NOT_IN_OXIDIZED", "HIGH", nb["name"], "active NetBox device has no matching Oxidized backup", netbox_ip=nb.get("primary_ip"), site=nb.get("site"), netbox_device=nb.get("url")))
            continue
        used.add(idx)
        b = backups[idx]
        matches[nb["name"]] = b
        if norm_name(nb["name"]) != norm_name(b.get("hostname")):
            findings.append(finding("HOSTNAME_MISMATCH", "MEDIUM", nb["name"], "NetBox name differs from backup hostname", netbox=nb["name"], oxidized=b.get("hostname"), file=b.get("backup_id"), site=nb.get("site"), netbox_device=nb.get("url")))
        nb_ip = nb.get("primary_ip") or ""
        if nb_ip and b.get("mgmt_ips") and nb_ip not in b["mgmt_ips"]:
            findings.append(finding("MGMT_IP_MISMATCH", "MEDIUM", nb["name"], "NetBox primary IP not found in backup config IPs", netbox_ip=nb_ip, oxidized_ips=b["mgmt_ips"][:8], file=b.get("backup_id"), site=nb.get("site"), netbox_device=nb.get("url")))
        nb_descs = {norm_iface_name(k): {"name": k, "description": norm_desc(v)} for k, v in nb_ifaces.get(nb["name"], {}).items()}
        ox_descs = backup_iface_descriptions(b)
        for key in sorted(set(nb_descs) | set(ox_descs)):
            nb_item = nb_descs.get(key, {})
            ox_item = ox_descs.get(key, {})
            nb_desc = nb_item.get("description", "")
            ox_desc = ox_item.get("description", "")
            if nb_desc != ox_desc:
                ifname = nb_item.get("name") or ox_item.get("name") or key
                detail = f"{ifname} description differs between NetBox and backup"
                findings.append(finding(
                    "INTERFACE_DESCRIPTION_MISMATCH",
                    "LOW",
                    nb["name"],
                    detail,
                    interface=ifname,
                    normalized_interface=key,
                    netbox=nb_desc or "<empty>",
                    oxidized=ox_desc or "<empty>",
                    file=b.get("backup_id"),
                    site=nb.get("site"),
                    netbox_device=nb.get("url"),
                    expected="NetBox interface description matches backed-up running configuration",
                    actual=f"NetBox={nb_desc or '<empty>'}; backup={ox_desc or '<empty>'}",
                    recommended_action="choose source of truth, then update NetBox interface description or device config through change control",
                ))
    active_names = {norm_name(d["name"]) for d in nb_devices}
    active_ips = {d.get("primary_ip") for d in nb_devices if d.get("primary_ip")}
    for idx, b in enumerate(backups):
        if idx in used:
            continue
        ids = {norm_name(b.get("backup_id")), norm_name(b.get("hostname"))}
        if ids.isdisjoint(active_names) and not (set(b.get("mgmt_ips", [])) & active_ips):
            findings.append(finding("OXIDIZED_NOT_ACTIVE_IN_NETBOX", "HIGH", b.get("hostname") or b.get("backup_id"), "backup exists but no active NetBox device matches", file=b.get("backup_id"), oxidized_ips=b.get("mgmt_ips", [])[:8], oxidized_history=f"/node/version?node_full={b.get('file', b.get('backup_id'))}"))
    for n in (ox_nodes or []):
        status = (n.get("last") or {}).get("status") or n.get("status")
        if status and status != "success":
            findings.append(finding("OXIDIZED_BACKUP_FAILED", "HIGH", n.get("full_name") or n.get("name"), "Oxidized last collection did not succeed", actual=status, expected="success", oxidized_history=f"/node/version?node_full={n.get('full_name')}", recommended_action="check credentials/reachability/model and run Oxidized reload/fetch"))
    if cache and pipeline_author_pattern:
        findings.extend(detect_manual_git_changes(cache, pipeline_author_pattern))
    return sorted(findings, key=lambda f: (SEVERITY_RANK.get(f["severity"], 9), f["code"], f["device"]))



def load_oxidized_nodes(base_url: str | None) -> list[dict]:
    if not base_url:
        return []
    r = requests.get(base_url.rstrip("/") + "/nodes.json", timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Oxidized GET /nodes.json failed: HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def detect_manual_git_changes(cache: Path, pipeline_author_pattern: str) -> list[dict]:
    try:
        out = subprocess.check_output(["git", "-C", str(cache), "log", "--name-only", "--pretty=format:%H%x09%an%x09%ae%x09%s", "-n", "20"], text=True, timeout=15)
    except Exception:
        return []
    rx = re.compile(pipeline_author_pattern, re.I)
    findings = []
    cur = None
    for line in out.splitlines():
        if "	" in line and len(line.split("	", 3)) == 4:
            h, an, ae, subj = line.split("	", 3)
            cur = {"hash": h[:12], "author": an, "email": ae, "subject": subj}
            continue
        if cur and line.strip() and not rx.search(cur["author"] + " " + cur["email"] + " " + cur["subject"]):
            device = Path(line.strip()).name
            findings.append(finding("CONFIG_CHANGED_OUTSIDE_PIPELINE", "MEDIUM", device, "latest config git history includes non-pipeline commit", commit=cur["hash"], author=cur["author"], subject=cur["subject"], gitea_diff="compare/history in network-configs repo"))
            cur = None
    return findings

def summarize(findings: list[dict], nb_count: int, ox_count: int, cache: Path) -> dict:
    return {
        "summary": {
            "netbox_active_devices": nb_count,
            "oxidized_backups": ox_count,
            "drift_findings": len(findings),
            "by_severity": dict(Counter(f["severity"] for f in findings)),
            "by_code": dict(Counter(f["code"] for f in findings)),
            "repo_cache": str(cache),
        },
        "findings": findings,
    }


def to_toon(report: dict) -> str:
    s = report["summary"]
    lines = ["summary:"]
    for k, v in s.items():
        lines.append(f"  {k}: {json.dumps(v, separators=(',', ':')) if isinstance(v, (dict, list)) else v}")
    lines.append("findings[severity,code,device,detail,evidence]:")
    for f in report["findings"]:
        evidence = {k: v for k, v in f.items() if k not in {"severity", "code", "device", "detail"}}
        lines.append(f"  - {f['severity']} | {f['code']} | {f['device']} | {f['detail']} | {json.dumps(evidence, separators=(',', ':'))}")
    return "\n".join(lines)


def task_description(report: dict) -> str:
    first = report["findings"][0]
    evidence = {k: v for k, v in first.items() if k not in {"severity", "code", "device", "detail"}}
    lines = [
        f"Device: {first.get('device','')}",
        f"Site: {first.get('site','')}",
        f"Severity: {first.get('severity','')}",
        "Detected by: netops-drift-detection",
        f"Expected state: {evidence.get('expected', 'NetBox and Oxidized agree; backups successful; changes via pipeline')}",
        f"Actual state: {first.get('detail','')}",
        f"Recommended action: {evidence.get('recommended_action', 'Review finding, choose source of truth, then approve NetBox/Oxidized/device remediation')}",
        "Links:",
        f"- NetBox device: {evidence.get('netbox_device','')}",
        f"- Gitea config diff: {evidence.get('gitea_diff', os.getenv('OXIDIZED_CONFIG_REPO', 'network-configs repo'))}",
        f"- Oxidized config history: {evidence.get('oxidized_history', os.getenv('OXIDIZED_URL', 'Oxidized'))}",
        "",
        "All findings (compact):",
        to_toon(report),
    ]
    return "\n".join(lines)


def create_task(report: dict, project_id: str | None) -> dict:
    if not report["findings"]:
        return {"status": "skipped", "reason": "no drift"}
    missing = [k for k in ("VIKUNJA_URL", "VIKUNJA_TOKEN") if not os.getenv(k)]
    if not (project_id or os.getenv("VIKUNJA_DEFAULT_PROJECT_ID")):
        missing.append("VIKUNJA_DEFAULT_PROJECT_ID or --task-project-id")
    if missing:
        return {"status": "blocked", "reason": "missing " + ", ".join(missing)}
    first = report["findings"][0]
    title = TASK_TITLES.get(first["code"], "Drift detected on {device}").format(device=first.get("device", "network"))
    desc = task_description(report)
    cmd = [sys.executable, str(VIKUNJA_HELPER), "create-task", title, "--description", desc, "--priority", "high"]
    if project_id:
        cmd += ["--project-id", str(project_id)]
    try:
        p = subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=30)
        return {"status": "created" if p.returncode == 0 else "failed", "rc": p.returncode, "stdout": p.stdout[-1000:], "stderr": p.stderr[-1000:]}
    except Exception as e:
        return {"status": "failed", "reason": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-url", default=os.getenv("OXIDIZED_CONFIG_REPO", DEFAULT_REPO))
    ap.add_argument("--cache-dir", default=os.getenv("OXIDIZED_CONFIG_CACHE", str(DEFAULT_CACHE)))
    ap.add_argument("--oxidized-url", default=os.getenv("OXIDIZED_URL", DEFAULT_OXIDIZED_URL), help="Oxidized web/API URL for /nodes.json verification; use empty string to skip")
    ap.add_argument("--pipeline-author-pattern", default=os.getenv("CONFIG_PIPELINE_AUTHOR_PATTERN", ""), help="Regex for approved config pipeline git author/subject; when set, non-matching commits become CONFIG_CHANGED_OUTSIDE_PIPELINE")
    ap.add_argument("--output", choices=["json", "toon"], default="toon")
    ap.add_argument("--create-task", action="store_true")
    ap.add_argument("--task-project-id")
    args = ap.parse_args()

    nb_url = os.getenv("NETBOX_URL")
    nb_token = os.getenv("NETBOX_TOKEN")
    if not nb_url or not nb_token:
        print("NETBOX_URL and NETBOX_TOKEN are required", file=sys.stderr)
        return 2
    if not args.repo_url:
        print("OXIDIZED_CONFIG_REPO or --repo-url is required", file=sys.stderr)
        return 2

    cache = clone_or_pull(args.repo_url, Path(args.cache_dir))
    devices = compact_nb_devices(nb_get_all(nb_url, nb_token, "dcim/devices/", {"status": "active"}))
    # Pull interface descriptions once; for very large NetBox deployments use server-side filters in a future revision.
    interfaces = compact_nb_interfaces(nb_get_all(nb_url, nb_token, "dcim/interfaces/"))
    backups = load_oxidized(cache)
    ox_nodes = load_oxidized_nodes(args.oxidized_url) if args.oxidized_url else []
    findings = detect(devices, interfaces, backups, ox_nodes=ox_nodes, cache=cache, pipeline_author_pattern=args.pipeline_author_pattern or None)
    report = summarize(findings, len(devices), len(backups), cache)
    report["summary"]["oxidized_api_nodes"] = len(ox_nodes)
    if args.create_task:
        report["task"] = create_task(report, args.task_project_id)
    print(json.dumps(report, indent=2) if args.output == "json" else to_toon(report) + ("\ntask: " + json.dumps(report.get("task", {}), separators=(",", ":")) if args.create_task else ""))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
