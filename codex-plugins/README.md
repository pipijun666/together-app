# MCP Selfhost Codex Marketplace

This directory contains a Codex plugin marketplace for the `mcp-selfhost` plugin.

The plugin connects Codex on the same Windows machine to a local MCP endpoint at `http://127.0.0.1:18080/mcp`.

## Marketplace import

Use this repository as the marketplace source, keep the Git ref on `main`, and set the sparse path to `codex-plugins`.

## Included files

- `.agents/plugins/marketplace.json`
- `plugins/mcp-selfhost/.codex-plugin/plugin.json`
- `plugins/mcp-selfhost/.mcp.json`
- `plugins/mcp-selfhost/scripts/check-local.ps1`
- `plugins/mcp-selfhost/skills/mcp-selfhost/SKILL.md`

## Security

No credentials, private keys, `.env` files, or tokens are included. The MCP endpoint is loopback-only and must be provided by the local `mcp-selfhost` bridge.
