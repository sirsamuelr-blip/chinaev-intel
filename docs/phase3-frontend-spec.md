# Phase 3 Frontend Specification

Reference document for all frontend tooling, standards, and quality requirements. Read this before starting any Phase 3 work.

## Tooling

- TypeScript strict mode (`"strict": true` in tsconfig.json)
- typescript-eslint with type-aware linting rules: `no-floating-promises`, `await-thenable`, `no-misused-promises`
- Vitest for unit/component tests + React Testing Library for component interaction tests
- Playwright for E2E tests
- Husky + lint-staged for frontend pre-commit hooks (eslint, prettier, tsc)
- ESLint security plugins for frontend security scanning

## Design System

- **Design tokens**: single source of truth in `tailwind.config.js` theme. All colors, spacing, typography, border-radius, shadows defined as tokens. No inline one-off values anywhere.
- **shadcn/ui + Radix**: generate components, then customize to project brand. Never ship default shadcn styles. Modify colors, spacing, border-radius, and typography to match the design system.
- **Tremor**: use for data-dense analytics views (pipeline health charts, comparison tables, trend lines). Customize Tremor's theme to match project tokens.
- **No CSS modules, no styled-components, no inline style objects.** Tailwind utility classes only, driven by design tokens.

## Accessibility (WCAG 2.1 AA)

- High-contrast focus rings on all interactive elements (WCAG 2.4.7 / 2.4.13)
- ARIA labels on all interactive elements that lack visible text
- Keyboard navigation for all components (Radix provides this by default, verify it works)
- aria-live regions for dynamic content updates (loading states, notifications, real-time data)
- Skip navigation link on dashboard pages
- Color contrast ratio minimum 4.5:1 for text, 3:1 for large text and UI components

## Polish Standards

- Skeleton loaders for all async data fetches (never show a blank space while loading)
- backdrop-blur on floating UI (modals, dropdowns, command palettes)
- CSS Grid for dashboard layouts (not flexbox hacks)
- Memoize expensive chart/table renders (React.memo, useMemo for data transforms)
- Loading, empty, and error states for every data-fetching component
- Smooth transitions on route changes and panel toggles (150-300ms, ease-out)

## Testing Strategy

- Unit tests: utility functions, hooks, data transforms
- Component tests: interactive components with React Testing Library (user events, not implementation details)
- E2E tests: critical user flows (login, search, filter, export, payment)
- No snapshot tests (brittle, low signal)
