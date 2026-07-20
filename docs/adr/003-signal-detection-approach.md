# ADR 003: Signal Detection Approach

## Status
Accepted

## Context
Phase 2 introduces signal detection: identifying actionable competitive intelligence from processed articles. The system needs to find six signal types (new_feature_launch, feature_trickle_down, ai_integration, ota_deployment, partnership_change, chip_hardware_announcement) and produce editorial-quality summaries suitable for the weekly digest.

Three approaches considered: pure rule-based (cheap, testable, but produces template-quality output), pure LLM (editorial quality, but expensive and non-deterministic), or hybrid.

## Decision
Hybrid two-stage pipeline:

**Stage 1 — Rule-based triggers (deterministic).** Pattern matching on extracted article fields (brandsMentioned, vehiclesMentioned, featuresExtracted, contentType). Each signal type has defined trigger rules. This stage identifies signal *candidates*. It is fast, free, and fully unit-testable.

**Stage 2 — LLM narrative generation (Sonnet).** Only candidates that pass Stage 1 are sent to a Sonnet API call that generates: title, summary, implicationsForWesternOems, and competitiveImpactScore (1-10). This stage produces editorial-quality output for the digest builder.

Trigger rules by signal type:
- `new_feature_launch`: featuresExtracted contains item where is_new == true
- `feature_trickle_down`: featuresExtracted contains is_new == true AND a similar feature (same category + similar name) already exists in features collection for a higher-segment vehicle
- `ai_integration`: featuresExtracted contains item where category == "ai_assistant" AND is_new == true, OR contentType == "news" AND body contains LLM/AI provider names
- `ota_deployment`: featuresExtracted contains item where category == "ota" AND is_new == true
- `partnership_change`: contentType in (news, opinion) AND brandsMentioned contains 2+ brands AND extracted competitive_signal is not null
- `chip_hardware_announcement`: featuresExtracted contains item where category == "adas" AND description mentions compute/chip/TOPS/SoC keywords

feature_trickle_down is the hardest to express as rules. Start with a loose trigger (is_new == true AND similar feature exists in a different segment) and lean on the LLM competitiveImpactScore to separate real trickle-down from noise.

## Consequences
- Stage 1 rules need tuning after seeing real data. Budget 1-2 iterations in the first week of operation.
- Estimated 10-20% of articles pass Stage 1 filters. At 200 articles/week, that's 20-40 additional Sonnet calls/week (~$1-2/week).
- Signal detection depends on entity promotion (ADR 005) populating features/brands/vehicles collections, since some rules query historical data.
- feature_trickle_down may need rule refinement or a dedicated LLM pre-check if the loose rule generates too many false positives.
