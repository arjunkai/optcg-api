-- OPCanvs Products: sets (starter/ultra decks) that feature a named character.
-- v1 = high-confidence starter-deck label parse only. Group/concept decks
-- (Straw Hat Crew, Worst Generation, Three Brothers, 3D2Y, GEAR5, Egghead...)
-- are intentionally excluded (no explicit personal name = not high-confidence).
-- Character ids resolved against the live `characters` roster (2026-07-07).
-- Idempotent: INSERT OR IGNORE on the composite PK.
CREATE TABLE IF NOT EXISTS set_characters (
  set_id TEXT NOT NULL,
  character_id INTEGER NOT NULL,
  PRIMARY KEY (set_id, character_id)
);

INSERT OR IGNORE INTO set_characters (set_id, character_id) VALUES
  ('ST-08', 408),  -- STARTER DECK -Monkey D. Luffy-
  ('ST-09', 656),  -- STARTER DECK -Yamato-
  ('ST-11', 624),  -- STARTER DECK -Uta-
  ('ST-12', 507),  -- STARTER DECK -Zoro and Sanji- : Roronoa Zoro
  ('ST-12', 524),  -- STARTER DECK -Zoro and Sanji- : Sanji
  ('ST-15', 176),  -- STARTER DECK -Red Edward.Newgate-
  ('ST-16', 624),  -- STARTER DECK -Green Uta-
  ('ST-17', 167),  -- STARTER DECK -Blue Donquixote Doflamingo-
  ('ST-18', 408),  -- STARTER DECK -Purple Monkey.D.Luffy-
  ('ST-19', 565),  -- STARTER DECK -Black Smoker-
  ('ST-20', 116),  -- STARTER DECK -Yellow Charlotte Katakuri-
  ('ST-22', 475),  -- STARTER DECK -Ace & Newgate- : Portgas D. Ace
  ('ST-22', 176),  -- STARTER DECK -Ace & Newgate- : Edward Newgate
  ('ST-23', 547),  -- STARTER DECK -RED Shanks-
  ('ST-24', 290),  -- STARTER DECK -GREEN Jewelry Bonney-
  ('ST-25', 79),   -- STARTER DECK -BLUE Buggy-
  ('ST-26', 408),  -- STARTER DECK -PURPLE/BLACK Monkey.D.Luffy-
  ('ST-27', 53),   -- STARTER DECK -BLACK Marshall.D.Teach- (roster canon: Blackbeard)
  ('ST-28', 656),  -- STARTER DECK -GREEN/YELLOW Yamato-
  ('ST-30', 408),  -- STARTER DECK EX -Luffy & Ace- : Monkey D. Luffy
  ('ST-30', 475);  -- STARTER DECK EX -Luffy & Ace- : Portgas D. Ace
