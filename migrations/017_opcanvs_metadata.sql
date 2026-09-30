-- 017_opcanvs_metadata.sql — additive; touches no existing table.
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
