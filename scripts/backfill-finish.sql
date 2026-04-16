UPDATE cards SET finish = 'standard' WHERE rarity IN ('Common', 'Uncommon') AND variant_type IS NULL;
UPDATE cards SET finish = 'foil' WHERE rarity = 'Rare' AND variant_type IS NULL;
UPDATE cards SET finish = 'holo' WHERE rarity IN ('Super Rare', 'Leader', 'Special', 'Promo') AND variant_type IS NULL;
UPDATE cards SET finish = 'textured' WHERE rarity IN ('Secret Rare', 'Treasure Rare') AND variant_type IS NULL;
UPDATE cards SET finish = 'holo' WHERE variant_type = 'Reprint';
UPDATE cards SET finish = 'textured' WHERE variant_type IN ('Alternate Art', 'Manga Art', 'Serial');
