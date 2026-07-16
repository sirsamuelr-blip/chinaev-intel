# MCP Server Registry

Maximum 5 servers. Each consumes context. Document every server here.

| Server | Version | Token Scope | Purpose | Installed |
|---|---|---|---|---|
| GitHub | (check with `claude mcp list`) | Fine-grained PAT, repo scope only | PR/issue participation, code review | Yes |
| Filesystem | built-in | n/a | File read/write (built into Claude Code) | Yes (built-in) |
| Context7 | (install and record) | n/a | Real-time library docs, reduces hallucination | Pending |
| Playwright | (install at Phase 3) | n/a | Browser automation for UI verification | Phase 3 |

## Security Rules

- Pin to specific versions. Do not use `latest`.
- Use fine-grained tokens scoped to minimum required permissions.
- Prefer vendor-maintained servers (Anthropic, Microsoft, GitHub) over community forks.
- Review MCP server release notes before version upgrades.
- 30+ CVEs filed against MCP servers in early 2026. Security issues found in ~66% of popular servers.
