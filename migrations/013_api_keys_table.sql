-- 013_api_keys_table.sql
--
-- Promotes the API key system from a comma-separated env var to a proper
-- database table with per-key metadata, lifecycle status, and audit
-- timestamps. Mirrors the pattern used by Stripe/OpenAI/etc.
--
-- Storage rule: only the SHA-256 hash of the raw key is stored. The raw
-- key is shown ONCE at issuance (in scripts/issue-key.mjs output) and
-- then never recoverable. Lookups happen by hash, not raw value.
--
-- `key_prefix` is the first 12 chars of the raw key (e.g. `opt_aBcDeFgH`),
-- stored separately so the owner can recognize a key in list output and
-- in logs without exposing the rest. The prefix on its own is not enough
-- to authenticate.
--
-- `status` is `active` or `revoked`. Revoked rows are kept (not deleted)
-- for audit history. Lookups filter on status='active'.
--
-- `tier` defaults to 'standard' (the 300/min + 100k/day caps in auth.js).
-- Reserved for a future `partner` tier with higher caps that select
-- partners can be promoted to.

CREATE TABLE IF NOT EXISTS api_keys (
  key_hash      TEXT PRIMARY KEY,
  key_prefix    TEXT NOT NULL,
  owner_name    TEXT NOT NULL,
  owner_contact TEXT,
  notes         TEXT,
  tier          TEXT NOT NULL DEFAULT 'standard',
  status        TEXT NOT NULL DEFAULT 'active',
  created_at    INTEGER NOT NULL,
  last_used_at  INTEGER,
  revoked_at    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_api_keys_status ON api_keys (status);
CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys (key_prefix);
