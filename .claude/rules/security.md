---
description: Security rules that apply to all code in this project
---

## Security Baseline

AI-generated code has 2.74x more vulnerabilities than human code. Apply extra scrutiny to all output.

- Never commit secrets, API keys, or credentials. Use `.env` and `backend/config/settings.py`.
- Never use `eval()`, `exec()`, or `pickle.loads()` with untrusted input.
- Never construct SQL or NoSQL queries by string concatenation.
- Never trust client-side input without server-side validation.
- Never store auth tokens in localStorage. Use httpOnly cookies.
- Never disable CORS protections or set `Access-Control-Allow-Origin: *` in production.
- All user-facing endpoints must have rate limiting.
- All file uploads must validate type and size server-side.
- Log security-relevant events (login attempts, permission failures, data access).

## CSRF Protection

All state-changing endpoints (POST, PUT, DELETE) must include CSRF protection. Use established CSRF token middleware, not custom implementation.

## Audit Logging

Log all security-relevant events with structured logging: timestamp, user ID, action, and result. Events to log:
- Login attempts (success and failure)
- Permission changes
- Subscription state changes (upgrade, downgrade, cancel)
- Admin actions (approve signal, send digest, modify subscriber)
- Data exports
- Failed authorization attempts

## Dependency Safety

- Verify package names exist on PyPI/npm before adding. ~20% of AI-suggested packages do not exist.
- Pin dependency versions in requirements.txt.
- Never install packages with `--no-verify` or equivalent flags.
- Before adding any dependency, verify its license: MIT, Apache-2.0, BSD, ISC are acceptable. GPL, AGPL, SSPL are not — copyleft contaminates proprietary SaaS. Check transitive dependencies too.
