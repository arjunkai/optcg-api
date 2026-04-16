"""
import.py — loads data/cards.json + data/sets.json into Supabase PostgreSQL
Run: python import.py
"""

import json
import psycopg2
from psycopg2.extras import execute_batch
from pathlib import Path

import os
DATABASE_URL = os.environ["DATABASE_URL"]

DATA_DIR = Path("data")

def main():
    print("Connecting to Supabase...")
    conn = psycopg2.connect(DATABASE_URL)
    cur  = conn.cursor()

    # ── Load sets ─────────────────────────────────────────────────────────────
    sets_file = DATA_DIR / "sets.json"
    with sets_file.open(encoding="utf-8") as f:
        sets = json.load(f)

    print(f"Importing {len(sets)} sets...")
    execute_batch(cur, """
        INSERT INTO sets (id, pack_id, label, type, card_count)
        VALUES (%(set_id)s, %(pack_id)s, %(label)s, %(type)s, %(count)s)
        ON CONFLICT (id) DO UPDATE SET
            pack_id    = EXCLUDED.pack_id,
            label      = EXCLUDED.label,
            type       = EXCLUDED.type,
            card_count = EXCLUDED.card_count
    """, sets)
    print(f"  ✅ {len(sets)} sets inserted")

    # ── Load cards ────────────────────────────────────────────────────────────
    cards_file = DATA_DIR / "cards.json"
    with cards_file.open(encoding="utf-8") as f:
        cards = json.load(f)

    # Apply variant_type overrides from classifier output
    mapping_file = DATA_DIR / "variant_types.json"
    overrides = {}
    if mapping_file.exists():
        with mapping_file.open(encoding="utf-8") as f:
            overrides = json.load(f)
        print(f"  Loaded {len(overrides)} variant_type overrides")

    for card in cards:
        if card["id"] in overrides:
            card["variant_type"] = overrides[card["id"]]

    print(f"Importing {len(cards)} cards...")
    execute_batch(cur, """
        INSERT INTO cards (
            id, base_id, parallel, variant_type, name,
            rarity, category, image_url,
            colors, cost, power, counter,
            attributes, types, effect, trigger
        ) VALUES (
            %(id)s, %(base_id)s, %(parallel)s, %(variant_type)s, %(name)s,
            %(rarity)s, %(category)s, %(image_url)s,
            %(colors)s, %(cost)s, %(power)s, %(counter)s,
            %(attributes)s, %(types)s, %(effect)s, %(trigger)s
        )
        ON CONFLICT (id) DO UPDATE SET
            name       = EXCLUDED.name,
            variant_type = EXCLUDED.variant_type,
            rarity     = EXCLUDED.rarity,
            category   = EXCLUDED.category,
            image_url  = EXCLUDED.image_url,
            colors     = EXCLUDED.colors,
            cost       = EXCLUDED.cost,
            power      = EXCLUDED.power,
            counter    = EXCLUDED.counter,
            attributes = EXCLUDED.attributes,
            types      = EXCLUDED.types,
            effect     = EXCLUDED.effect,
            trigger    = EXCLUDED.trigger
    """, cards, page_size=500)
    print(f"  ✅ {len(cards)} cards inserted")

    # ── Load card_sets (many-to-many) ──────────────────────────────────────
    print("Importing card-set relationships...")
    execute_batch(cur, """
        INSERT INTO card_sets (card_id, set_id, pack_id)
        VALUES (%(id)s, %(set_id)s, %(pack_id)s)
        ON CONFLICT (card_id, set_id) DO NOTHING
    """, cards, page_size=500)
    print(f"  ✅ {len(cards)} card-set relationships inserted")

    conn.commit()
    cur.close()
    conn.close()
    print("\n🎉 Done! Database is ready.")

if __name__ == "__main__":
    main()