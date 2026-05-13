---
name: oxidized
description: Check and operate the local Oxidized backup service. Use when listing Oxidized node status, checking last config fetch status, reloading the node list, fetching stored configs, or summarizing failed/stale backups from the Oxidized web API.
user-invocable: true
metadata:
  openclaw:
    requires:
      bins: ["python3"]
      env: ["MCP_CALL", "OXIDIZED_MCP_SCRIPT", "OXIDIZED_URL"]
---

# Oxidized Operations

Use this skill for the local Oxidized config-backup service.

The Oxidized API endpoint is configured by `OXIDIZED_URL`. Do not hard-code the service address in skills or scripts.

## Token-Efficient Rules

1. Use the Oxidized MCP server first; do not dump full configs unless explicitly requested.
2. Prefer compact summaries: counts by status, stale/failed nodes, and per-node last fetch metadata.
3. For config retrieval, return metadata by default: byte count, line count, SHA256 prefix, first hostname line if present.
4. Only include full config text when the user explicitly asks for the full backup.

## Operations

### Node status

```bash
python3 $MCP_CALL "python3 -u $OXIDIZED_MCP_SCRIPT" oxidized_status '{"include_nodes":true}'
```

Uses `/nodes.json` and reports:
- node count
- status counts
- failed/non-success nodes
- stale nodes when `--stale-hours` is set
- node fields: `full_name`, `ip`, `group`, `model`, `last.status`, `last.start`, `last.end`, `last.time`

### Reload node list

```bash
python3 $MCP_CALL "python3 -u $OXIDIZED_MCP_SCRIPT" oxidized_reload '{}'
```

Uses `/reload.json` first and falls back to `/reload`.

### Config fetch status / stored config metadata

```bash
python3 $MCP_CALL "python3 -u $OXIDIZED_MCP_SCRIPT" oxidized_fetch '{"node":"rs-lab-eve1/VY1"}'
```

Uses `/node/fetch/<group>/<node>`. This fetches the stored backup from Oxidized, not a live device poll. Report metadata unless `full_config=true` is explicitly used.

## Safety

- Oxidized reads are safe.
- `reload` refreshes the Oxidized node list; use it when requested or when node inventory looks stale.
- Do not modify the Oxidized git repository from this skill.
- If failed backups are found, report them. Create a task only when the user asks or another workflow explicitly requires task creation.
