# ADR 002: Component Library Selection

## Status
Proposed (finalize before Phase 3 starts)

## Context
AI coding tools default to shadcn/ui patterns, producing millions of identical-looking sites. The product needs a professional, distinctive UI for both the admin dashboard (data-dense operations cockpit) and the subscriber dashboard (search, compare, read briefs).

## Decision
- Base components: shadcn/ui + Radix primitives (copy-paste, then customize)
- Data-dense views (pipeline health, analytics, comparison tables): Tremor
- All components customized with project design tokens — never ship defaults
- Accessibility: Radix provides keyboard nav and ARIA; we add focus rings (WCAG 2.4.7/2.4.13), aria-live regions, skip nav

## Consequences
- Two component libraries to learn, but they cover different use cases
- Must build design token system before any frontend components
- shadcn components must be explicitly customized after generation
