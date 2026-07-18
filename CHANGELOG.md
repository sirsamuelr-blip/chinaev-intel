# Changelog

All notable changes to this project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Project scaffold: backend structure, reference docs, CI pipeline
- CLAUDE.md progressive disclosure architecture (root + backend + docs/)
- Playbook enforcement: pre-commit hooks, scoped rules, skills, CI security gates
- PR template, issue templates, CODEOWNERS
- ADRs: progressive disclosure (#001), component library selection (#002)
- Security: CSRF rules, audit logging rules, dependency license checking
- Docs: phase 3 frontend spec, revenue-critical paths, MCP registry, rollback plan, tech debt register

### Fixed
- Update ruff (v0.15.21) and mypy (v1.14.0) pre-commit hook versions; record Context7 3.2.4 in MCP registry and defer GitHub MCP to gh CLI
