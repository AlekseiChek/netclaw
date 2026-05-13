---
name: netops-drift-detection
description: Detect drift between NetBox active device inventory, Oxidized API status, and Oxidized git backups. Use when checking NetBox vs Oxidized coverage, backup failures, missing NetBox fields, hostname/management IP/interface description mismatch, manual config changes outside pipeline, or when drift findings should create a task manager task.
user-invocable: true
metadata:
  openclaw:
    requires:
      bins: ["python3", "git"]
      env: ["NETBOX_URL", "NETBOX_TOKEN", "OXIDIZED_URL"]
---

# NetOps Drift Detection

Compare **NetBox source-of-truth**, **Oxidized API status**, and **Oxidized backup configs stored in git**.

The detector validates inventory coverage, backup health, hostname / management-IP drift, and **port/interface descriptions** between NetBox and the backed-up running configuration.

Configuration:

```text
Oxidized API:  OXIDIZED_URL
Gitea repo:    OXIDIZED_CONFIG_REPO or --repo-url
```

Do not hard-code the Oxidized service address; keep it in `OXIDIZED_URL`.

## Token-Efficient Workflow

1. Use the bundled script first; do not dump full configs into chat.
2. Pull compact NetBox active device data: name, site, platform, role, primary mgmt IP, URL.
3. Pull NetBox interface/port names and descriptions for active devices.
4. Verify Oxidized API with `/nodes.json`: node presence, last collection status, last collection time.
5. Pull/update the Oxidized git repo and parse only needed config facts: hostname, interface descriptions, IPs.
6. Normalize common interface names before description comparison, e.g. `GigabitEthernet0/1` vs `Gi0/1`, `Ethernet0/0` vs `eth0`.
7. Treat either side having an empty description while the other has a description as drift.
8. Return only counts and severity-sorted findings unless the user asks for a report.
9. If the user asks for a **report**, include the compact summary and finding table.
10. If the user does **not** ask for a report and everything is healthy, no detailed report is needed; a short all-clear is enough.
11. If any problem exists, create/trigger one task-manager task and offer fix options. Do **not** fix anything until the user approves the exact action.

Run:

```bash
python3 /root/.openclaw/workspace/skills/netops-drift-detection/scripts/drift_detect.py \
  --repo-url "$OXIDIZED_CONFIG_REPO" \
  --output toon \
  --create-task
```

Use `--output json` when another tool will consume the output.

Optional manual-change detection:

```bash
python3 scripts/drift_detect.py --pipeline-author-pattern 'jenkins|netclaw-pipeline|oxidized'
```

## Drift Checks

| Code | Severity | Task title pattern | Meaning |
|---|---:|---|---|
| `NETBOX_ACTIVE_NOT_IN_OXIDIZED` | HIGH | `Drift detected on PE01` | Active NetBox device has no Oxidized backup/API match. |
| `OXIDIZED_NOT_ACTIVE_IN_NETBOX` | HIGH | `Verify device decommissioning` | Oxidized backup exists but no active NetBox device matches. |
| `OXIDIZED_BACKUP_FAILED` | HIGH | `Oxidized failed to collect config from MX router` | Oxidized `/nodes.json` last status is not `success`. |
| `NETBOX_DEVICE_MISSING_FIELDS` | MEDIUM | `Device has no platform / mgmt IP / role` | Active NetBox device is missing platform, management IP, or role. |
| `CONFIG_CHANGED_OUTSIDE_PIPELINE` | MEDIUM | `Manual config change detected` | Git history indicates config changed by a non-approved pipeline author/pattern. |
| `HOSTNAME_MISMATCH` | MEDIUM | `Drift detected on PE01` | NetBox device name differs from hostname parsed from backup config. |
| `MGMT_IP_MISMATCH` | MEDIUM | `Drift detected on PE01` | NetBox primary management IP not found in parsed backup config IPs. |
| `INTERFACE_DESCRIPTION_MISMATCH` | LOW | `Drift detected on PE01` | NetBox port/interface description differs from backup config, including missing description on either side. |

## Task Manager Rule

If any drift/problem is found, create one task-manager task unless the user explicitly asked for report-only mode.

Preferred task path:

```bash
python3 /root/.openclaw/workspace/skills/vikunja-task-tracker/scripts/vikunja_api.py create-task ...
```

Requirements:
- `VIKUNJA_URL` and `VIKUNJA_TOKEN`
- `VIKUNJA_DEFAULT_PROJECT_ID`, or explicit `--task-project-id`

If task-manager environment is missing, report `[blocked] task creation` with the missing variable, but still return drift findings.

### Task Body Template

Every task must include these fields:

```text
Device:
Site:
Severity:
Detected by: netops-drift-detection
Expected state:
Actual state:
Recommended action:
Links:
- NetBox device
- Gitea config diff
- Oxidized config history
```

## Fix Handling

When problems exist, offer 2-3 safe options, for example:

- update NetBox fields after approval
- reload Oxidized node list / investigate failed collection after approval
- create a ServiceNow change for device-side correction when config remediation is needed

Never auto-fix NetBox, Oxidized, git, or device config from this skill. Wait for explicit approval.

## Normalization Rules

- Device identity match order: NetBox name, backup hostname, primary management IP, backup filename, Oxidized full_name/name.
- Compare names case-insensitively, but preserve original values in findings.
- Strip CIDR prefix when comparing management IP host address.
- Normalize common interface aliases when comparing port descriptions: `GigabitEthernet`/`Gi`, `FastEthernet`/`Fa`, `TenGigabitEthernet`/`Te`, `Ethernet`/`eth`, `Loopback`/`Lo`, `Port-channel`/`Po`, `Vlan`/`Vlan`.
- Ignore interface descriptions only when both NetBox and backup are empty.
- Flag description drift when NetBox has a description but backup is empty, backup has a description but NetBox is empty, or both are non-empty but differ after whitespace normalization.
- Do not modify NetBox or Oxidized from this skill; findings are reported and ticketed only.

## Evidence to Return

For findings or requested reports include:
- NetBox active device count
- Oxidized API node count
- Oxidized git backup count
- drift count by severity and code
- task creation status or `[blocked]` reason
- path of cloned/pulled repo cache
