# ADR 005: Entity Resolution Strategy

## Status
Accepted

## Context
The Phase 1 LLM pipeline extracts brandsMentioned, vehiclesMentioned, and featuresExtracted as string arrays on article docs. Phase 2 promotes these to standalone docs in the brands, vehicles, and features collections. The same entity appears under multiple names across articles: "BYD", "BYD Auto", "比亚迪" are all one brand. Without resolution, the features collection fragments and the competitive comparison engine returns incomplete results.

Three approaches considered: LLM-only resolution (handles novel entities but non-deterministic and adds API cost on every entity), static alias dictionary (fast and testable but requires manual curation), or hybrid.

## Decision
Hybrid: curated alias dictionary with LLM fallback.

**Alias dictionary** (`backend/config/brand_aliases.json`):
- Maps variant names → canonical English name
- Encodes parentGroup relationships
- Covers all major Chinese EV brands, key subsidiaries, and major suppliers
- Checked on every entity promotion call. Fast, deterministic, testable.

Initial dictionary scope (~40 brand entries):
- OEMs: BYD, NIO, XPENG, Li Auto, Huawei/AITO, Xiaomi, Zeekr, Geely, Volvo, Polestar, Lynk & Co, Great Wall/Haval/Tank/ORA, Changan, SAIC/IM Motors/MG/Roewe, GAC Aion, Dongfeng/Voyah, Chery, FAW, BAIC, Leapmotor, Neta/Hozon, Avatr, JiYue, Rising Auto, iCar, Deepal, Smart (Geely/Mercedes JV)
- Suppliers: CATL, Huawei Qiankun, Horizon Robotics, Black Sesame, RoboSense, Hesai, Desay SV, BYD Semiconductor
- Each entry includes: canonical nameEn, nameZh, known aliases (English + Chinese), parentGroup

**LLM fallback:** When a brand name is not in the dictionary, call Sonnet: "Is [name] a known Chinese EV brand or automotive supplier? If so, return the canonical English name, Chinese name, and parent group. If not, return null." On success, the resolved entity is added to the dictionary file for next time (flagged for human review via a log entry, not auto-committed).

**Brand modeling:** Separate brand doc per brand, linked by parentGroup field. Zeekr and Geely are separate docs with parentGroup: "Geely". This preserves feature-level granularity for the comparison engine while allowing group-level queries.

**Vehicle resolution:** Same alias pattern. Vehicle model name variants mapped to canonical form. Less critical than brand resolution because vehicles are always associated with a brand, providing disambiguation.

**Feature resolution:** Features are NOT resolved to canonical names. Different articles may describe the same feature differently ("City NOA", "Urban Navigate on Autopilot", "城市领航辅助驾驶"). Instead, features are grouped by category (adas, ai_assistant, etc.) and brand. The comparison engine operates at the category level, not individual feature name level. This avoids a hard NLP problem that doesn't need solving at V1.

## Consequences
- Initial curation effort for the alias dictionary (~2 hours). One-time cost.
- Dictionary grows over time as the LLM fallback resolves new entities.
- LLM fallback calls are rare — estimated <5 per week after the first month.
- Separate brand docs mean the comparison engine can query by brand OR by parentGroup.
- Feature-level dedup is deferred. Acceptable at V1 scale. Revisit if the features collection grows large enough that duplicate features cause noise in the comparison engine.
- The alias dictionary is a config file, not code. Can be updated without a code deploy.
