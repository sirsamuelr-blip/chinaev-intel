# Revenue-Critical Test Paths

These paths must have test coverage before the corresponding phase ships. Investors check for tests on revenue-critical code.

## Phase 3 (Admin Dashboard)
- Firebase Auth: admin login, session validation, unauthorized access denial
- Firestore security rules: emulator tests for all collections

## Phase 4 (Report Generation)
- Digest generation: Opus API call, markdown output validation
- Email delivery: Resend API integration, template rendering, subscriber list filtering
- Digest scheduling: send at correct time, handle send failures

## Phase 5 (Subscriber Dashboard)
- Stripe webhook signature validation
- Subscription state machine: free → analyst → team, upgrade, downgrade, cancel, expire
- Paywall enforcement: free user cannot access paid features, expired subscription blocks access
- Stripe checkout session creation and completion
- Saved search + alert creation and triggering
- Feature search: correct filtering by brand, category, date, segment
- Export to PDF/PPTX: output matches expected format

## Cross-cutting
- API rate limiting: verify limits are enforced
- CSRF: verify state-changing endpoints reject requests without valid tokens
- Auth token expiry: verify expired tokens are rejected
