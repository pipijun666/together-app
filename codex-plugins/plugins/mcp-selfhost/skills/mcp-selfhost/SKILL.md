---
name: mcp-selfhost
description: Use the user's private local Desktop Commander MCP on this Windows machine for file, process, PowerShell, WSL, Git, and development tasks without the hosted Remote Desktop Commander relay.
---

# MCP Selfhost

Use the `desktop-commander-local` MCP server for work that needs access to this Windows development machine.

## Preferred workflow

1. Confirm the requested action is local-machine work.
2. Prefer read-only inspection before edits or process changes.
3. Use file tools for file reads/writes and process tools for commands.
4. Keep commands scoped to the user's requested project or directory.
5. Report concrete command output and changed paths.

## Local transport

The plugin connects to:

`http://127.0.0.1:18080/mcp`

That endpoint is provided by the local `mcp-selfhost` bridge and does not require the hosted Remote Desktop Commander service.

## Safety and secrets

- Do not read SSH private keys, browser profiles, credential stores, password databases, `.env` secrets, or tokens unless the user explicitly requests the specific secret-bearing file and it is necessary.
- Do not change Desktop Commander security configuration such as `allowedDirectories` or `blockedCommands` as part of an unrelated file/process workflow.
- Treat destructive filesystem, service, firewall, account, disk, or boot changes as high-impact operations and require explicit user intent.
- Do not expose the local MCP port to the public network.

## Bridge recovery

If `desktop-commander-local` is unavailable, inspect the local bridge first. The deployment project is at:

`C:\Users\pipijun\mcp-selfhost`

The normal bridge endpoint should listen on `127.0.0.1:18080`. Use the project's existing startup scripts or scheduled task rather than creating a second unmanaged relay.
