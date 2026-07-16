---
name: security-review
description: Pre-merge security review checklist. Use before merging any PR, especially those touching auth, payments, database access, or API endpoints.
---

# Security Review Checklist

## Secrets
- [ ] No API keys, tokens, passwords, or credentials in the diff
- [ ] No hardcoded URLs with credentials
- [ ] All secrets loaded from environment variables via `backend/config/settings.py`

## Authentication & Authorization
- [ ] Protected endpoints validate Firebase ID tokens server-side
- [ ] No endpoint exposes data without checking `request.auth.uid`
- [ ] No user can access another user's data
- [ ] Auth tokens not stored in localStorage
- [ ] CSRF protection on state-changing endpoints

## Input Validation
- [ ] All user inputs validated server-side
- [ ] No string concatenation in database queries
- [ ] Rate limiting on public-facing endpoints

## Firestore
- [ ] No `allow read, write: if true`
- [ ] Rules validate `request.auth.uid == resource.data.userId`
- [ ] Rules use `hasOnly()` to prevent field injection

## Audit Logging
- [ ] Auth events logged (login, logout, failures)
- [ ] Payment/subscription events logged
- [ ] Admin actions logged

## Dependencies
- [ ] No new packages with known vulnerabilities (`pip-audit`)
- [ ] No packages that don't exist on PyPI/npm
- [ ] All new dependency licenses are MIT/Apache-2.0/BSD/ISC (no GPL/AGPL)
- [ ] Versions pinned

## Report
After completing, report: issues found, severity (critical/high/medium/low), recommended fixes.
