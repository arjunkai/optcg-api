-- Pokémon TCG cards. Multi-language: composite PK on (card_id, lang).
-- card_id is TCGdex's globally-shared id (e.g. sv01-001) — same id across
-- all languages, so a JP collector and an EN collector see the same id
-- with localized name + image.
--
-- Schema chosen to mirror what TCGdex returns + a small set of denormalized
-- fields (image_high, image_low, types_csv, dominant_color) so the slim
-- endpoint can SELECT without parsing JSON on every row. Heavy text
-- (effect/abilities/attacks) lives in `raw` for /pokemon/cards/:id full-detail
-- responses.
create table if not exists ptcg_cards (
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
  updated_at   integer not null default (strftime('%s','now')),
  primary key (card_id, lang)
);
create index if not exists ptcg_cards_set_lang on ptcg_cards (set_id, lang);
create index if not exists ptcg_cards_lang on ptcg_cards (lang);
create index if not exists ptcg_cards_name_lang on ptcg_cards (lang, name);

create table if not exists ptcg_sets (
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

create table if not exists ptcg_price_history (
  card_id      text not null,
  source       text not null,   -- 'cardmarket' | 'tcgplayer'
  variant      text not null,   -- 'normal' | 'reverseHolofoil' | 'holofoil' | etc.
  recorded_at  integer not null,
  price_usd    real,
  price_eur    real,
  primary key (card_id, source, variant, recorded_at)
);
create index if not exists ptcg_price_history_card on ptcg_price_history (card_id, recorded_at desc);
