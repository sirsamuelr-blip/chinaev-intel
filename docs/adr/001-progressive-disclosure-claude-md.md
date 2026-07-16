# ADR 001: Progressive Disclosure for CLAUDE.md

## Status
Accepted

## Context
The original CLAUDE.md was 430+ lines. Research shows frontier models reliably follow only ~150 instructions, and the "lost in the middle" effect degrades attention to mid-context instructions.

## Decision
Split into a progressive disclosure hierarchy: root CLAUDE.md (~100 lines) for universal rules, backend/CLAUDE.md for Python conventions, docs/ for reference specs, .claude/rules/ for conditional rules, .claude/skills/ for on-demand workflows.

## Consequences
- Session context stays lean, improving instruction adherence
- Detailed specs available on demand via @docs/ references
- Must maintain consistency across files when conventions change
