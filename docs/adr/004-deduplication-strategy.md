# ADR 004: Deduplication Strategy

## Status
Accepted

## Context
Multiple sources report the same story. Gasgoo and CnEVPost both cover a BYD announcement, with different headlines and wording. Without deduplication, the signal detection pipeline generates duplicate signals from duplicate articles. Phase 1 deduplicates by URL only (same source, same article). Phase 2 needs content-level dedup across sources.

Three approaches considered: title similarity only (misses ~40-60% of real duplicates due to different headlines), multi-signal similarity using extracted fields (no extra API cost, catches ~80%+), or embedding similarity (most robust but requires embedding model and vector storage, overkill at V1 scale).

## Decision
Multi-signal similarity using fields already extracted by the Phase 1 LLM pipeline:

**Similarity dimensions (weighted):**
- Title similarity (fuzzy string matching on titleEn): weight 0.3
- Brand overlap (Jaccard similarity on brandsMentioned): weight 0.25
- Vehicle overlap (Jaccard similarity on vehiclesMentioned): weight 0.2
- Feature category overlap (categories from featuresExtracted): weight 0.15
- Publish date proximity (within 72-hour window): weight 0.1 (binary — within window or not)

Composite score above threshold → duplicate. Start threshold conservatively high (e.g., 0.75) to avoid false merges. Loosen after reviewing real data.

**Comparison scope:** Only compare articles from different sources (same-source dedup already handled by URL in the runner). Only compare articles within a 72-hour publish date window to limit the comparison set.

**Storage — fields added to articles collection:**
- `isDuplicate` (boolean): true if this article is a duplicate of an earlier one
- `canonicalArticleId` (string): doc ID of the earliest/highest-relevance article in the group
- `duplicateGroupId` (string): shared ID across all articles in the same duplicate cluster

**Downstream effects:**
- Signal detection filters out articles where isDuplicate == true
- The canonical article retains all source URLs for "reported by multiple sources" attribution in digests
- The digest builder can show "Sources: Gasgoo, CnEVPost, 36kr" on a signal by pulling all articles in the duplicateGroupId cluster

## Consequences
- No additional API cost — uses fields already extracted
- Threshold needs tuning after seeing real data. Err conservative (let dupes through rather than merge distinct stories)
- Comparison is O(n²) within the 72-hour window, but with ~30-50 articles per window across 4 sources, this is trivial
- Three new fields on every article doc. Firestore index on duplicateGroupId for cluster queries.
- Articles about the same brand but different topics (e.g., "BYD price cut" vs "BYD earnings") should not merge — the publish date window + vehicle/feature overlap dimensions prevent this in most cases
