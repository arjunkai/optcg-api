-- OPCanvs Secondary Cards seed: characters DEPICTED IN THE ART of another
-- character's card (role='secondary'). Vision-identified, owner-confirmed.
-- match_method='vision', confidence reflects identification certainty.
-- Idempotent: INSERT OR IGNORE on the (card_id, character_id) PK.
-- This file is the format the full vision-scrape tool appends to / regenerates.

-- OP14-013 Monkey.D.Luffy (SR): Law + Kid drawn behind Luffy (Sabaody Supernovas)
INSERT OR IGNORE INTO card_characters (card_id, character_id, role, match_method, confidence) VALUES
  ('OP14-013', 610, 'secondary', 'vision', 0.99),  -- Trafalgar Law
  ('OP14-013', 186, 'secondary', 'vision', 0.99);  -- Eustass Kid
