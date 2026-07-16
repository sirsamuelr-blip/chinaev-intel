---
description: Auth and payments code requires human review
paths:
  - "**/*auth*"
  - "**/*login*"
  - "**/*signup*"
  - "**/*stripe*"
  - "**/*payment*"
  - "**/*billing*"
  - "**/*subscri*"
---

## MANDATORY: Human Review Required

Code in this area (auth, payments, subscriptions) must NOT be auto-accepted or auto-committed.

1. Write the code.
2. Run all tests.
3. STOP. Output the code and wait for human review.
4. Do not commit, push, or merge without explicit human approval.

### Auth Patterns

- Use Firebase Auth. Do not build custom auth.
- Validate Firebase ID tokens server-side on every protected endpoint.
- Never expose user data to other users. Always check `request.auth.uid`.
- Implement session expiry and token refresh.
- Include CSRF protection on all auth endpoints.
- Log all auth events (login success, login failure, token refresh, logout).

### Payment Patterns

- Use Stripe's official SDK. Do not build custom payment flows.
- Validate webhook signatures on every Stripe webhook.
- Store Stripe customer/subscription IDs in the `subscribers` collection.
- Never log full card numbers or payment details.
- Handle all Stripe error states: failed payments, expired cards, disputed charges.
- Log all subscription state changes.
