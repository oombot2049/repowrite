# MCP and Skills guide

[Documentation](../README.md) · [中文](../zh-CN/guides/mcp-and-skills.md)

MCP and Skills extend different parts of NexaPilot:

- **MCP Server** contributes executable tools over a process or network transport.
- **Skill** contributes instructions and supporting resources that teach an Agent how to perform a class of work.

A Skill may tell the model when to call a tool, but it does not bypass Tool Policy or Permission.

## Inspect MCP

```bash
nexa mcp status
nexa mcp tools
nexa mcp connect <server-name>
nexa mcp disconnect <server-name>
```

Add a local stdio server:

```bash
nexa mcp add \
  --name local-fs \
  --command npx \
  --args "@modelcontextprotocol/server-filesystem,/safe/root"
```

Adding configuration does not mean every server is always connected. At startup or explicit connect, NexaPilot starts or connects the configured transport, discovers tool schemas, and registers them. The actual external action occurs only when the model later calls a discovered tool.

## Trust boundary

A local stdio MCP Server is a separate process running on the same machine. Installing or starting it executes third-party code. Review its package, command, arguments, environment, allowed roots, and network behavior.

MCP tools enter the same Policy Engine as built-in tools. Configure `allow`, `ask`, or `deny` based on side effects; discovery alone is not user approval for every invocation.

## Inspect Skills

```bash
nexa skills list --worktree /path/to/repository
nexa skills get <skill-name>
```

Skill loading is worktree-aware. A Skill should define a narrow trigger, complete procedure, required resources, safety constraints, and fallback behavior. Do not put credentials or repository-private data in reusable Skills.

## Troubleshooting

- **Server not connected**: inspect `mcp status`, command availability, process stderr, URL, and authentication.
- **Tool missing**: confirm connection and schema discovery, then inspect the effective Agent tool allowlist.
- **Tool denied**: inspect Policy rules; MCP does not bypass them.
- **Duplicate Skill**: remove duplicate definitions from overlapping discovery roots.
