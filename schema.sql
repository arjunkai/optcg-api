DROP TABLE IF EXISTS card_translations;
DROP TABLE IF EXISTS card_price_history;
DROP TABLE IF EXISTS card_sets;
DROP TABLE IF EXISTS cards;
DROP TABLE IF EXISTS sets;

CREATE TABLE sets (
  id TEXT PRIMARY KEY,
  pack_id TEXT NOT NULL,
  label TEXT NOT NULL,
  type TEXT,
  card_count INTEGER NOT NULL
);

CREATE TABLE cards (
  id TEXT PRIMARY KEY,
  base_id TEXT,
  parallel INTEGER NOT NULL DEFAULT 0,
  variant_type TEXT,
  name TEXT NOT NULL,
  rarity TEXT,
  category TEXT,
  finish TEXT,
  image_url TEXT,
  colors TEXT,
  cost INTEGER,
  power INTEGER,
  counter INTEGER,
  attributes TEXT,
  types TEXT,
  effect TEXT,
  trigger_text TEXT,
  price REAL,
  foil_price REAL,
  delta_price REAL,
  delta_7d_price REAL,
  tcg_ids TEXT,
  price_updated_at INTEGER,
  price_source TEXT,
  -- Per-language Japanese price (real JA market value; never the EN price on a
  -- JA card). EN price stays in the columns above. See migration 016.
  price_ja REAL,
  price_source_ja TEXT,
  price_updated_at_ja INTEGER
);

-- Per-language display fields for OPTCG. Only name/image/effect/trigger vary
-- by language; everything game-neutral stays on `cards`. name_en is the
-- canonical English alias for cross-script search (NULL on EN rows). One
-- Piece is one catalog with translated display, so this is a side-table —
-- NOT a denormalized row-per-(card,lang) like ptcg_cards. See migration 016.
CREATE TABLE card_translations (
  card_id      TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  language     TEXT NOT NULL CHECK (language IN ('en', 'ja')),
  name         TEXT NOT NULL,
  name_en      TEXT,
  image_url    TEXT,
  effect       TEXT,
  trigger_text TEXT,
  PRIMARY KEY (card_id, language)
);

CREATE TABLE card_sets (
  card_id TEXT NOT NULL REFERENCES cards(id),
  set_id TEXT NOT NULL REFERENCES sets(id),
  pack_id TEXT,
  PRIMARY KEY (card_id, set_id)
);

CREATE TABLE card_price_history (
  card_id TEXT NOT NULL,
  price REAL NOT NULL,
  captured_at INTEGER NOT NULL,
  PRIMARY KEY (card_id, captured_at)
);

CREATE INDEX idx_cards_category ON cards(category);
CREATE INDEX idx_cards_rarity ON cards(rarity);
CREATE INDEX idx_cards_parallel ON cards(parallel);
CREATE INDEX idx_cards_variant_type ON cards(variant_type);
CREATE INDEX idx_cards_price ON cards(price);
CREATE INDEX idx_cards_price_updated_at ON cards(price_updated_at);
CREATE INDEX idx_cards_price_source ON cards(price_source);
CREATE INDEX idx_card_sets_set_id ON card_sets(set_id);
CREATE INDEX idx_card_sets_card_id ON card_sets(card_id);
CREATE INDEX idx_card_translations_language ON card_translations(language);
CREATE INDEX idx_card_translations_name ON card_translations(language, name);

-- OPCanvs metadata (migration 017)
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
CREATE TABLE crews (
  id INTEGER PRIMARY KEY, source_id INTEGER, name TEXT, roman_name TEXT,
  is_yonko INTEGER, total_prime TEXT, number TEXT, status TEXT
);
CREATE TABLE characters (
  id INTEGER PRIMARY KEY, source_id INTEGER,
  name TEXT, name_normalized TEXT,
  crew_id INTEGER, fruit_name TEXT, fruit_type TEXT,
  bounty TEXT, job TEXT, status TEXT
);
CREATE TABLE locations (
  id INTEGER PRIMARY KEY, source_id INTEGER,
  name TEXT, region_name TEXT, roman_name TEXT, sea_name TEXT, affiliation_name TEXT
);
CREATE TABLE card_characters (
  card_id TEXT NOT NULL, character_id INTEGER NOT NULL,
  role TEXT, match_method TEXT, confidence REAL,
  PRIMARY KEY (card_id, character_id)
);
CREATE TABLE card_locations (
  card_id TEXT NOT NULL, location_id INTEGER NOT NULL,
  match_method TEXT, confidence REAL,
  PRIMARY KEY (card_id, location_id)
);
CREATE TABLE artwork (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL, card_id TEXT, illustrator_id INTEGER,
  image_key TEXT, title TEXT
);
CREATE TABLE artwork_characters (
  artwork_id INTEGER NOT NULL, character_id INTEGER NOT NULL,
  PRIMARY KEY (artwork_id, character_id)
);
CREATE INDEX idx_card_illustrators_ill ON card_illustrators(illustrator_id);
CREATE INDEX idx_card_characters_char  ON card_characters(character_id);
CREATE INDEX idx_card_locations_loc    ON card_locations(location_id);
CREATE INDEX idx_characters_crew       ON characters(crew_id);
CREATE INDEX idx_artwork_card          ON artwork(card_id);
CREATE INDEX idx_artwork_ill           ON artwork(illustrator_id);
CREATE INDEX idx_artwork_chars_char    ON artwork_characters(character_id);
