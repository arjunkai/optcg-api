-- schema.sql: the full production schema as a fresh database.
--
-- Generated from prod sqlite_master (2026-09-29), so column order matches prod
-- (columns added by ALTER TABLE sit at the end of their table). Keep it in
-- sync with each migration. Only for creating a NEW database (local/dev):
-- CREATE TABLE fails on an existing table, which is deliberate — this file
-- used to start with DROP TABLEs, so running it against optcg-cards --remote
-- would have wiped the catalog.


-- ===== OPTCG catalog =====

CREATE TABLE sets (
  id TEXT PRIMARY KEY,
  pack_id TEXT NOT NULL,
  label TEXT NOT NULL,
  card_count INTEGER NOT NULL
, type TEXT, logo_url TEXT, box_url TEXT);


CREATE TABLE cards (
  id TEXT PRIMARY KEY,
  base_id TEXT,
  parallel INTEGER NOT NULL DEFAULT 0,
  variant_type TEXT,
  name TEXT NOT NULL,
  rarity TEXT,
  category TEXT,
  image_url TEXT,
  colors TEXT,
  cost INTEGER,
  power INTEGER,
  counter INTEGER,
  attributes TEXT,
  types TEXT,
  effect TEXT,
  trigger_text TEXT
, finish TEXT, price REAL, foil_price REAL, delta_price REAL, delta_7d_price REAL, tcg_ids TEXT, price_updated_at INTEGER, price_source TEXT, price_ja REAL, price_source_ja TEXT, price_updated_at_ja INTEGER);

CREATE INDEX idx_cards_category ON cards(category);
CREATE INDEX idx_cards_parallel ON cards(parallel);
CREATE INDEX idx_cards_price ON cards(price);
CREATE INDEX idx_cards_price_source ON cards(price_source);
CREATE INDEX idx_cards_price_updated_at ON cards(price_updated_at);
CREATE INDEX idx_cards_rarity ON cards(rarity);
CREATE INDEX idx_cards_variant_type ON cards(variant_type);

CREATE TABLE card_translations (
  card_id      TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  language     TEXT NOT NULL CHECK (language IN ('en', 'ja')),
  name         TEXT NOT NULL,
  name_en      TEXT,           -- canonical English alias for cross-script search; NULL on EN rows
  image_url    TEXT,
  effect       TEXT,
  trigger_text TEXT,
  PRIMARY KEY (card_id, language)
);

CREATE INDEX idx_card_translations_language ON card_translations (language);
CREATE INDEX idx_card_translations_name     ON card_translations (language, name);

CREATE TABLE card_sets (
  card_id TEXT NOT NULL REFERENCES cards(id),
  set_id TEXT NOT NULL REFERENCES sets(id),
  pack_id TEXT,
  PRIMARY KEY (card_id, set_id)
);

CREATE INDEX idx_card_sets_card_id ON card_sets(card_id);
CREATE INDEX idx_card_sets_set_id ON card_sets(set_id);

CREATE TABLE card_price_history (
  card_id TEXT NOT NULL,
  price REAL NOT NULL,
  captured_at INTEGER NOT NULL,
  PRIMARY KEY (card_id, captured_at)
);

CREATE INDEX idx_price_history_card_time
  ON card_price_history(card_id, captured_at DESC);


-- ===== OPCanvs metadata (017, 018, 020) =====

CREATE TABLE illustrators (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  slug        TEXT UNIQUE NOT NULL,
  name        TEXT NOT NULL,
  name_ja     TEXT,
  name_kana   TEXT,
  twitter TEXT, instagram TEXT, pixiv TEXT, tumblr TEXT, website TEXT,
  bio         TEXT,
  card_count  INTEGER DEFAULT 0,
  source      TEXT
);


CREATE TABLE card_illustrators (
  card_id        TEXT NOT NULL,
  illustrator_id INTEGER NOT NULL,
  PRIMARY KEY (card_id, illustrator_id)
);

CREATE INDEX idx_card_illustrators_ill ON card_illustrators(illustrator_id);

CREATE TABLE crews (
  id INTEGER PRIMARY KEY, source_id INTEGER, name TEXT, roman_name TEXT,
  is_yonko INTEGER, total_prime TEXT, number TEXT, status TEXT
);


CREATE TABLE characters (
  id INTEGER PRIMARY KEY, source_id INTEGER,
  name TEXT, name_normalized TEXT,
  crew_id INTEGER, fruit_name TEXT, fruit_type TEXT,
  bounty TEXT, job TEXT, status TEXT
, name_ja TEXT, source TEXT, wikidata_qid TEXT, fandom_title TEXT, epithet TEXT, affiliation TEXT);

CREATE INDEX idx_characters_crew       ON characters(crew_id);

CREATE TABLE locations (
  id INTEGER PRIMARY KEY, source_id INTEGER,
  name TEXT, region_name TEXT, roman_name TEXT, sea_name TEXT, affiliation_name TEXT
);


CREATE TABLE card_characters (
  card_id TEXT NOT NULL, character_id INTEGER NOT NULL,
  role TEXT, match_method TEXT, confidence REAL,
  PRIMARY KEY (card_id, character_id)
);

CREATE INDEX idx_card_characters_char  ON card_characters(character_id);

CREATE TABLE card_locations (
  card_id TEXT NOT NULL, location_id INTEGER NOT NULL,
  match_method TEXT, confidence REAL,
  PRIMARY KEY (card_id, location_id)
);

CREATE INDEX idx_card_locations_loc    ON card_locations(location_id);

CREATE TABLE artwork (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL, card_id TEXT, illustrator_id INTEGER,
  image_key TEXT, title TEXT
, source_url TEXT, kind TEXT, artist TEXT, collection TEXT);

CREATE INDEX idx_artwork_card          ON artwork(card_id);
CREATE INDEX idx_artwork_ill           ON artwork(illustrator_id);

CREATE TABLE artwork_characters (
  artwork_id INTEGER NOT NULL, character_id INTEGER NOT NULL,
  PRIMARY KEY (artwork_id, character_id)
);

CREATE INDEX idx_artwork_chars_char    ON artwork_characters(character_id);

CREATE TABLE set_characters (
  set_id TEXT NOT NULL,
  character_id INTEGER NOT NULL,
  PRIMARY KEY (set_id, character_id)
);


CREATE TABLE products (
  slug TEXT PRIMARY KEY,
  type TEXT NOT NULL,          -- sleeve | playmat | collection | set
  title TEXT NOT NULL,
  price TEXT,
  release TEXT,
  image_url TEXT,
  product_url TEXT
);



-- ===== Pokemon TCG (006-011, 015) =====

CREATE TABLE ptcg_sets (
  set_id            text not null,
  lang              text not null,
  name              text not null,
  series            text,
  release_date      text,
  card_count_total    integer,
  card_count_official integer,
  logo_url          text,
  symbol_url        text,
  raw               text,
  updated_at        integer not null default (strftime('%s','now')),
  primary key (set_id, lang)
);


CREATE TABLE ptcg_cards (
  card_id      text not null,
  lang         text not null,
  set_id       text not null,
  local_id     text not null,
  name         text not null,
  category     text,         -- 'Pokemon' | 'Trainer' | 'Energy'
  rarity       text,
  hp           integer,
  types_csv    text,         -- comma-separated, e.g. "Fire,Colorless"
  stage        text,         -- 'Basic' | 'Stage1' | 'Stage2' | 'VMAX' | 'VSTAR' | etc.
  variants_json text,        -- JSON: {"normal":true,"holo":false,"reverse":true,"firstEdition":false,"wPromo":false}
  image_low    text,         -- url e.g. https://assets.tcgdex.net/en/sv/sv01/001/low.png
  image_high   text,         -- url e.g. https://assets.tcgdex.net/en/sv/sv01/001/high.webp
  pricing_json text,         -- JSON: {"cardmarket":{...},"tcgplayer":{...}} — null until pricing import lands
  dominant_color text,       -- '#RRGGBB' — null until placeholder color backfill (future)
  raw          text,         -- full TCGdex card JSON for /pokemon/cards/:id full-detail
  updated_at   integer not null default (strftime('%s','now')), price_source TEXT, retreat INTEGER, name_en TEXT, campaign TEXT, distribution_method TEXT,
  primary key (card_id, lang)
);

CREATE INDEX idx_ptcg_cards_price_source
    ON ptcg_cards(price_source);
CREATE INDEX ptcg_cards_campaign ON ptcg_cards (campaign) WHERE campaign IS NOT NULL;
CREATE INDEX ptcg_cards_distribution_method ON ptcg_cards (distribution_method) WHERE distribution_method IS NOT NULL;
CREATE INDEX ptcg_cards_lang on ptcg_cards (lang);
CREATE INDEX ptcg_cards_name_en ON ptcg_cards (name_en) WHERE name_en IS NOT NULL;
CREATE INDEX ptcg_cards_name_lang on ptcg_cards (lang, name);
CREATE INDEX ptcg_cards_set_lang on ptcg_cards (set_id, lang);

CREATE TABLE ptcg_price_history (
  card_id      text not null,
  source       text not null,   -- 'cardmarket' | 'tcgplayer'
  variant      text not null,   -- 'normal' | 'reverseHolofoil' | 'holofoil' | etc.
  recorded_at  integer not null,
  price_usd    real,
  price_eur    real,
  primary key (card_id, source, variant, recorded_at)
);

CREATE INDEX ptcg_price_history_card on ptcg_price_history (card_id, recorded_at desc);

CREATE TABLE ptcg_backfill_cursor (
  source TEXT PRIMARY KEY,
  last_card_id TEXT,
  updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);



-- ===== API keys (012-014) =====

CREATE TABLE api_keys (
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
, scopes TEXT NOT NULL DEFAULT 'optcg');

CREATE INDEX idx_api_keys_prefix ON api_keys (key_prefix);
CREATE INDEX idx_api_keys_status ON api_keys (status);

CREATE TABLE api_key_usage (
  api_key TEXT NOT NULL,
  day TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (api_key, day)
);

CREATE INDEX idx_api_key_usage_day ON api_key_usage (day);

