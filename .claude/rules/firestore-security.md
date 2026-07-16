---
description: Firestore security rules — loaded when working on database or security files
paths:
  - "backend/db/**"
  - "**/firestore*"
  - "**/firebase*"
  - "**/*.rules"
---

## Firestore Security Rules

The #1 mistake in AI-generated Firebase apps is shipping `allow read, write: if true`.

### Required patterns

- Always verify ownership: `request.auth.uid == resource.data.userId`
- Always validate data shape: use `request.resource.data.keys().hasOnly([...])` to prevent field injection
- Never allow `isAdmin` to be set by the client. Admin status is set server-side only.
- Always validate data types in rules (e.g., `request.resource.data.email is string`)
- Separate read and write rules. Read access may be broader than write.

### Testing

- Test rules with `@firebase/rules-unit-testing` and the Firebase Emulator Suite
- Write tests for: authenticated access, unauthenticated denial, cross-user denial, field injection denial
- Run emulator tests in CI

### Deployment

- Firestore rules and Storage rules are separate deploys
- Review rules diff before every deploy
- Never deploy rules that are less restrictive than current production rules
