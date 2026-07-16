---
description: Commit message format — always loaded
---

## Commit Message Format

Every commit uses Conventional Commits. Enforced by pre-commit hook.

```
<type>: <description>
```

Types: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`

Rules:
- Imperative mood: "add scraper" not "added scraper"
- Lowercase description, no period, under 72 characters

Examples:
- `feat: add gasgoo scraper with RSS discovery`
- `fix: handle malformed JSON in pipeline extraction`
- `test: add fixtures for cnevpost article parsing`
- `docs: add ADR for Firestore denormalization strategy`
