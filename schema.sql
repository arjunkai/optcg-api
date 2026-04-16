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
  trigger_text TEXT
);

CREATE TABLE card_sets (
  card_id TEXT NOT NULL REFERENCES cards(id),
  set_id TEXT NOT NULL REFERENCES sets(id),
  pack_id TEXT,
  PRIMARY KEY (card_id, set_id)
);

CREATE INDEX idx_cards_category ON cards(category);
CREATE INDEX idx_cards_rarity ON cards(rarity);
CREATE INDEX idx_cards_parallel ON cards(parallel);
CREATE INDEX idx_cards_variant_type ON cards(variant_type);
CREATE INDEX idx_card_sets_set_id ON card_sets(set_id);
CREATE INDEX idx_card_sets_card_id ON card_sets(card_id);
