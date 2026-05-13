---
name: eve-ng-config-ops
description: Manage EVE-NG node startup configurations — read, push, wipe, restore from backup into lab startup-configs, and bulk-export configs stored in lab files. Use when backing up node configs before changes, restoring backup configs into lab startup-configs, pre-loading startup configs before boot, clearing configs to reset a node without full NVRAM wipe, or exporting all node configs from a lab at once.
user-invocable: true
metadata:
  openclaw:
    requires:
      bins: ["python3"]
      env: ["EVE_URL", "EVE_USER", "EVE_PASSWORD"]
---

# EVE-NG Config Operations

Manage startup configurations stored inside EVE-NG lab files. Read, push, and clear node configs without using the console. Configs are applied at next node boot.

## When to Use

- Reading the stored startup config for a node
- Exporting all node configs from a lab at once (pre-change backup)
- Pre-loading a startup config so a node boots pre-configured
- Clearing just the startup config without wiping NVRAM (lighter than `eve_wipe_node`)
- Automating lab provisioning: create lab → add nodes → push configs → start lab

## Config vs Wipe Distinction

| Operation | Effect | Node State Required |
|---|---|---|
| `eve_set_node_config` | Writes startup config | Stopped |
| `eve_wipe_node_config` | Clears startup config (writes empty) | Stopped |
| `eve_wipe_node` (node-ops) | Clears NVRAM + startup config | Stopped |

Use `eve_wipe_node_config` when you only need to reset the config file.
Use `eve_wipe_node` when you also need to clear NVRAM state.

## MCP Server

- **Command**: `python3 -u mcp-servers/eve-ng-mcp-server/eve_ng_mcp_server.py` (stdio transport)
- **Requires**: `EVE_URL`, `EVE_USER`, `EVE_PASSWORD` environment variables

## Available Tools

| Tool | Parameters | What It Does |
|------|------------|--------------|
| `eve_get_node_config` | lab_path, node | Get stored startup config for one node |
| `eve_set_node_config` | lab_path, node, config | Push startup config text to a node |
| `eve_get_all_configs` | lab_path | Bulk export — all node configs in one call |
| `eve_wipe_node_config` | lab_path, node | Clear startup config (write empty string) |

## Workflow Examples

### Backup Before Changes

```
"Export all configs from the BGP lab before I change anything"
  → eve_get_all_configs /ENSLD/BGP.unl
```

### Pre-load Config for Fresh Boot

```
"Push this IOS config to R1 so it boots pre-configured"
  → eve_stop_node R1 (node-ops)
  → eve_set_node_config /ENSLD/BGP.unl R1 "hostname R1\ninterface Ethernet0/0\n ip address 10.0.12.1 255.255.255.252\n no shutdown\n!\nrouter ospf 1\n network 0.0.0.0 255.255.255.255 area 0\n!"
  → eve_start_node R1 (node-ops)
```

### Reset Config Only

```
"Clear R1's startup config without wiping NVRAM"
  → eve_stop_node R1 (node-ops)
  → eve_wipe_node_config /ENSLD/BGP.unl R1
  → eve_start_node R1 (node-ops)
```

### Read a Single Node Config

```
"What startup config does R2 have stored?"
  → eve_get_node_config /ENSLD/BGP.unl R2
```

## Backup Restore Rule

When restoring configs from a backup repository or archive into an EVE-NG lab, treat it as **startup-config replacement**, not a live device restore:

1. Map backup file names to EVE node names/IDs.
2. Stop only affected nodes if they are running.
3. Replace stored startup config with `eve_set_node_config`.
4. Verify stored config via `eve_get_node_config`, `eve_get_all_configs`, or summaries.
5. Do **not** boot nodes, console in, commit/save, or verify running config unless the user explicitly asks for live verification.

## Config Provisioning Workflow (Full Lab)

```
1. Create lab                   → eve-ng-lab-management: eve_create_lab
2. Add nodes                    → eve-ng-node-operations: eve_create_node (repeat)
3. Wire topology                → eve-lab-topology-build: eve_create_network + eve_connect_interface
4. Push startup configs         → eve_set_node_config (repeat, nodes stopped)
5. Start lab                    → eve-ng-node-operations: eve_start_lab
6. Verify via console           → eve-ng-console-ops: eve_exec_ios / eve_exec_junos
```

## Integration with Other Skills

- **eve-ng-node-operations**: Stop nodes before pushing configs; start after
- **eve-ng-console-ops**: Use `show running-config` output to verify config was applied; or collect config text to feed back into `eve_set_node_config`
- **eve-ng-lab-management**: Export lab file after provisioning for archival

## Error Handling

| Error Code | Meaning | Resolution |
|------------|---------|------------|
| `EVE_NOT_FOUND` | Node not found in lab | Run `eve_list_nodes` to confirm name |
| `EVE_VALIDATION` | Config format rejected | Verify config text — some EVE versions require specific line endings |
| `EVE_AUTH_FAILED` | Session expired | Re-auth is automatic; retry |

## Notes

- Configs are stored inside the `.unl` file — they persist across server restarts
- A node must be **stopped** before writing or clearing its startup config
- `eve_set_node_config` does not validate config syntax — errors appear at next boot
- `eve_get_all_configs` is efficient: retrieves all node configs in two API calls (nodes + configs)
- All operations logged to GAIT audit trail
