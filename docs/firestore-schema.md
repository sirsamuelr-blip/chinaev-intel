# Firestore Schema

Firestore is a document database. Denormalize aggressively so common queries do not require multiple lookups. All collections use auto-generated document IDs unless noted.

## `brands` collection

| Field | Type | Description |
|---|---|---|
| nameEn | string | English brand name |
| nameZh | string | Chinese brand name |
| parentGroup | string | Parent company (e.g., "BYD", "Geely") |
| founded | number | Year founded |
| hqCity | string | Headquarters city |
| evFocus | boolean | True if brand is EV-only |
| website | string | Brand website URL |
| logoUrl | string | URL to logo in Firebase Storage |
| lastUpdated | timestamp | Last modification time |

## `vehicles` collection

| Field | Type | Description |
|---|---|---|
| brandId | string | Reference to brands doc ID |
| brandNameEn | string | Denormalized English brand name |
| modelNameEn | string | English model name |
| modelNameZh | string | Chinese model name |
| year | number | Model year |
| segment | string | Vehicle segment (sedan, SUV, MPV, etc.) |
| powertrain | string | One of: `BEV`, `PHEV`, `ICE` |
| priceRangeCny | string | Price range in CNY (e.g., "150,000-200,000") |
| priceRangeUsd | string | Price range in USD |
| platformName | string | Vehicle platform name |
| eeArchitecture | string | One of: `domain`, `zonal`, `centralized` |
| lastUpdated | timestamp | Last modification time |

## `features` collection

| Field | Type | Description |
|---|---|---|
| vehicleId | string | Reference to vehicles doc ID |
| vehicleModelName | string | Denormalized model name |
| brandId | string | Reference to brands doc ID |
| brandNameEn | string | Denormalized English brand name |
| category | string | One of: `adas`, `ai_assistant`, `infotainment`, `connectivity`, `ota`, `battery_software`, `cockpit_ux` |
| subcategory | string | Finer classification within category |
| featureNameEn | string | English feature name |
| featureNameZh | string | Chinese feature name |
| description | string | What the feature does |
| supplier | string | Supplier or provider if known |
| firstSeenDate | timestamp | When we first saw this feature |
| isStandard | boolean | True if included in base trim |
| isOtaUpdatable | boolean | True if deliverable via OTA |
| competitiveNote | string | Why this matters vs. Western OEMs |
| launchDate | timestamp | Official launch date |
| launchType | string | One of: `new`, `update`, `rollout` |

Composite indexes: `brandId + category`, `category + firstSeenDate`, `brandId + firstSeenDate`

**Feature categories:**
- `adas` — L2/L2+/L3 capabilities, compute chip, sensor suite
- `ai_assistant` — Voice AI, LLM provider, multimodal capabilities
- `infotainment` — Screen specs, OS, app ecosystem
- `connectivity` — V2X, 5G, phone integration
- `ota` — Update frequency, scope, delivery method
- `battery_software` — BMS features, charging optimization
- `cockpit_ux` — Interaction patterns, UI paradigms

## `articles` collection

| Field | Type | Description |
|---|---|---|
| sourceName | string | Source identifier (e.g., "gasgoo", "autohome") |
| sourceUrl | string | Original article URL |
| titleZh | string | Original Chinese title (empty string if English source) |
| titleEn | string | English title (original or translated) |
| bodyZh | string | Original Chinese body text |
| bodyEn | string | English body text (original or translated) |
| publishDate | timestamp | When the source published the article |
| scrapeDate | timestamp | When we scraped it |
| contentType | string | One of: `news`, `review`, `teardown`, `forum`, `video_transcript` |
| relevanceScore | number | 1-10, 10 = directly about software/AI/UX features |
| processed | boolean | True after LLM extraction is complete |
| processingError | string | Error message if LLM extraction failed. Null on success. |
| brandsMentioned | array\<string\> | Brand names found in the article |
| vehiclesMentioned | array\<string\> | Specific model names found |
| featuresExtracted | array\<map\> | Embedded array of extracted feature objects (see llm-pipeline.md for shape) |

Composite indexes: `sourceName + scrapeDate`, `relevanceScore`, `processed`

## `signals` collection

| Field | Type | Description |
|---|---|---|
| signalType | string | One of: `new_feature_launch`, `feature_trickle_down`, `ai_integration`, `ota_deployment`, `partnership_change`, `chip_hardware_announcement` |
| title | string | One-line signal headline |
| summary | string | 2-3 sentence summary |
| brandsMentioned | array\<string\> | Brands involved |
| featuresMentioned | array\<string\> | Features involved |
| sourceArticleIds | array\<string\> | References to articles doc IDs |
| implicationsForWesternOems | string | Why Western automakers should care |
| competitiveImpactScore | number | 1-10 |
| createdDate | timestamp | When signal was generated |
| status | string | One of: `pending`, `approved`, `killed` |
| includedInDigestId | string | Reference to digests doc if included |

Composite indexes: `createdDate`, `status + createdDate`, `competitiveImpactScore`

## `digests` collection

| Field | Type | Description |
|---|---|---|
| weekNumber | number | ISO week number |
| year | number | Year |
| generatedDate | timestamp | When the digest was generated |
| subjectLine | string | Email subject line |
| introParagraph | string | Opening paragraph of the brief |
| signalIds | array\<string\> | References to included signal doc IDs |
| fullMarkdown | string | Complete brief in Markdown |
| fullHtml | string | Complete brief in HTML (for email) |
| sent | boolean | True after email is sent |
| sentDate | timestamp | When email was sent |
| editedBy | string | Admin user ID who last edited |
| editNotes | string | Notes about edits made |

## `subscribers` collection

Document ID = Firebase Auth UID (not auto-generated).

| Field | Type | Description |
|---|---|---|
| email | string | Subscriber email |
| plan | string | One of: `free`, `analyst`, `team`, `enterprise` |
| stripeCustomerId | string | Stripe customer ID |
| stripeSubscriptionId | string | Stripe subscription ID |
| signupDate | timestamp | When they signed up |
| lastLogin | timestamp | Last dashboard login |
| teamId | string | Team identifier (for team plan) |
| savedSearches | array\<map\> | Saved search configurations |
| alertPreferences | map | `{ brands: string[], categories: string[] }` |

## `scraper_health` collection

| Field | Type | Description |
|---|---|---|
| sourceName | string | Source identifier |
| runTimestamp | timestamp | When the scrape ran |
| status | string | One of: `success`, `partial`, `failure` |
| articlesIngested | number | Count of articles scraped this run |
| requestsMade | number | Total HTTP requests made |
| errorCount | number | Number of errors |
| errors | array\<map\> | `[{ url: string, statusCode: number, message: string }]` |
| durationSeconds | number | Total run time in seconds |

Composite index: `sourceName + runTimestamp`
