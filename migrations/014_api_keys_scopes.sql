-- 014_api_keys_scopes.sql
--
-- Per-key scope allow-list. Stored as a comma-separated string (e.g.
-- 'optcg', 'optcg,ptcg') so we can add future scopes without another
-- schema migration. Auth gate splits on commas, trims, and treats the
-- resulting set as the key's allowed games.
--
-- Default: 'optcg'. New keys without an explicit scope flag get OPTCG
-- access only — matches the conservative-by-default policy and means
-- PTCG access has to be an explicit decision.
--
-- Scope -> path mapping (enforced in src/auth.js):
--   optcg  -> /sets, /cards, /cards/:id, /cards/:id/price-history
--   ptcg   -> /pokemon/* (except /pokemon/images which is public)
-- Public paths (/, /docs, /openapi.json, /images, /pokemon/images) never
-- check scopes.

ALTER TABLE api_keys ADD COLUMN scopes TEXT NOT NULL DEFAULT 'optcg';

-- Backfill: any existing rows get the default 'optcg'.
UPDATE api_keys SET scopes = 'optcg' WHERE scopes IS NULL OR scopes = '';
