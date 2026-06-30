# OPCanvs data-layer — production apply runbook (optcg-cards D1)

Apply each file with:
`npx wrangler d1 execute optcg-cards --remote --file=<path>`

**Apply IN THIS ORDER** (017 creates the tables, 018 ALTERs them, data comes after):

1. `migrations/017_opcanvs_metadata.sql`     — additive tables (illustrators, characters, joins, artwork…)
2. `migrations/018_characters_enrich.sql`    — adds characters.name_ja/source/wikidata_qid/fandom_title/epithet/affiliation
3. `scripts/opcanvs/opcanvs_batches/illustrators.sql`
4. `scripts/opcanvs/opcanvs_batches/card_illustrators.sql`
5. `scripts/opcanvs/opcanvs_batches/characters.sql`
6. `scripts/opcanvs/opcanvs_batches/card_characters.sql`

That is the COMPLETE v1 apply set. Nothing else in this directory is applied.

## Do NOT apply
- There is no api-onepiece `lore.sql` anymore (removed — it was superseded and would PK-collide with `characters.sql`).
- **Locations / world-map** (the `locations`/`card_locations` data) are a LATER phase — regenerate them from a clean source then; the api-onepiece versions were removed.

## Notes
- 017/018 are applied here via raw `execute --file` (the tables are new / columns additive, so they won't disturb the live `cards`/prices/images). This does NOT update wrangler's `d1_migrations` tracking table — so do NOT later run `wrangler d1 migrations apply optcg-cards` expecting it to skip these (it would try to re-run them). If you want tracking, insert rows into `d1_migrations` for 017/018 after applying.
- Roll back (if ever needed): `DROP TABLE card_characters; DROP TABLE card_illustrators; DROP TABLE characters; DROP TABLE illustrators; DROP TABLE crews; DROP TABLE locations; DROP TABLE card_locations; DROP TABLE artwork; DROP TABLE artwork_characters;` — all new/isolated; nothing else references them.
- Regenerate the character data anytime with `node scripts/opcanvs/resolve-characters.mjs` (needs `scripts/opcanvs/match_cards.json`, a `wrangler d1 execute optcg-cards --remote --json "SELECT id,name,category FROM cards"` dump).
