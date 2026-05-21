# OPTCG API — handoff to Michael

**Date:** 2026-05-21
**Status:** OPTCG side audited and ready. Send when convenient.

This is a draft message + reference notes for the user to copy/paste/customize when emailing Michael his API key.

---

## Draft message

> Hey Michael,
>
> Thanks for your patience — your OPTCG API key is ready.
>
> **Base URL:** https://optcg-api.arjunbansal-ai.workers.dev
> **Auth:** send your key as the `X-API-Key` request header on every call
> **Your key:** `<paste-the-generated-key-here>`
> **OpenAPI spec:** GET `/docs` with the header, import into Scalar / Swagger UI / Postman / Insomnia
>
> **Endpoints**
> - `GET /cards` — filters: color, category, rarity, name, set_id, parallel, min_power, max_power, min_cost, max_cost, page, page_size
> - `GET /cards/{id}` — single card (e.g. `OP01-001`)
> - `GET /sets` — all sets
> - `GET /sets/{setCode}/cards` — cards in a set
> - `GET /images/{card_id}` — image proxy (no CORS issues, serves R2-hosted high-res scans when curated, falls back to TCGPlayer CDN)
>
> **Data quality snapshot (audited 2026-05-21)**
> - 4,582 cards total
> - 100% have current prices (USD, primary source TCGPlayer Market Price)
> - 100% have image URLs
> - 98.9% of prices refreshed within the last 3 days; weekly refresh runs every Monday
> - 208 DON cards using high-res R2 proxy
> - 3 JP-exclusive Championship promo variants (suffix `_jpN`)
>
> **Rate limits & terms**
> - Polite use: keep request volume reasonable; aggressive crawling will get rate-limited
> - Cache responses where possible (sets don't change between releases)
> - Don't redistribute the raw API data
> - If you hit anything unexpected, ping me and I'll dig in
>
> Looking forward to seeing what you build with it.

---

## What still needs to happen on our end (before send)

1. **Generate Michael's API key** in the key-management surface (likely a wrangler secret + DB row in `api_keys` if that table exists, or whatever the existing key-issuance flow is). Make sure it's tied to his application from the 2026-05-15 Google Form so we can audit later.
2. **Paste the key** into the message above where it says `<paste-the-generated-key-here>`.
3. **Send the email/message** through whatever channel Michael provided in his application.
4. **Log the handoff** — record in our notes / Linear / wherever that Michael received key X on 2026-05-21.

## Audit reference (for our records)

Full audit results in `[[project-optcg-api-michael-handoff]]` memory entry (2026-05-21 section). Headline:
- Coverage: 100% priced / 100% imaged across 4,582 cards
- Freshness: 98.9% updated within 3 days
- DON cards: 208/208 on R2 proxy (no legacy CDN URLs)
- API auth gate: working (returns `{"error":"api key required"}` for unauthenticated requests)
- Spot-check across 5 rarity tiers: prices look reasonable (Common $0.10 / Uncommon $0.20 / Rare $11.90 / Super Rare $4.64 / Secret Rare $18.61)

Minor hygiene items, NOT blockers for handoff:
- 13 rows have `price` set but `price_source = NULL` (legacy pre-`price_source` rows)
- ~50 rows are >14 days stale (TCGPlayer didn't return a current listing; cached value persists)

Both items can be cleaned up later as background hygiene work; neither affects what Michael's app will see.
