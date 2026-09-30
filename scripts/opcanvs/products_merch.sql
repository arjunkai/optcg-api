-- OPCanvs Products: non-card merch (accessories / premium collections).
-- Card products (boosters/decks/premium boosters) live in the `sets` table;
-- this table holds the accessory catalog the sets table doesn't cover.
-- Source: en.onepiece-cardgame.com/products (current lineup; the official site
-- 404s historical product pages, so this is the currently-listed merch, and is
-- refreshable by re-running the products scrape). image_url + product_url are
-- absolute (rendered through wsrv on the frontend). Idempotent on slug PK.
CREATE TABLE IF NOT EXISTS products (
  slug TEXT PRIMARY KEY,
  type TEXT NOT NULL,          -- sleeve | playmat | collection | set
  title TEXT NOT NULL,
  price TEXT,
  release TEXT,
  image_url TEXT,
  product_url TEXT
);

INSERT OR IGNORE INTO products (slug, type, title, price, release, image_url, product_url) VALUES
 ('sleeve041','sleeve','LIMITED CARD SLEEVE PREMIUM MATTE vol.6','$12.00',NULL,'https://en.onepiece-cardgame.com/onepiececg/bccard/jp/products/2026/06/17/3bvtf9OBglsmqaxB/img_item01.webp','https://en.onepiece-cardgame.com/products/sleeve041.html'),
 ('sleeve040','sleeve','OFFICIAL CARD SLEEVE LIMITED EDITION vol.6','$9.00',NULL,'https://en.onepiece-cardgame.com/onepiececg/bccard/jp/product/2026/06/18/1FayaUeq4BHgbAmi/img_item01.webp','https://en.onepiece-cardgame.com/products/sleeve040.html'),
 ('playmat_limited006','playmat','OFFICIAL PLAYMAT LIMITED EDITION vol.6','$30.00',NULL,'https://en.onepiece-cardgame.com/onepiececg/bccard/en/product/2026/06/18/eFrWoO7UjFLwfHmo/img_set_PBあり_card-en.webp','https://en.onepiece-cardgame.com/products/playmat_limited006.html'),
 ('heroines-special','set','ONE PIECE Heroines Special Set','$70.00','September 2026','https://en.onepiece-cardgame.com/onepiececg/bccard/en/products/2026/03/27/g6g9k0iwjLG0icmQ/img_item01.webp','https://en.onepiece-cardgame.com/products/heroines-special.html'),
 ('collection-drama003','collection','Premium Card Collection -Live Action Edition vol.2 Baroque Works-','$20.00','November 2026','https://en.onepiece-cardgame.com/onepiececg/bccard/en/products/2026/04/17/VLw2s5angR3lvzLp/img_item01.webp','https://en.onepiece-cardgame.com/products/collection-drama003.html'),
 ('collection-drama002','collection','Premium Card Collection -Live Action Edition vol.2 Straw Hat Crew-','$20.00','November 2026','https://en.onepiece-cardgame.com/onepiececg/bccard/en/products/2026/04/17/MP7cRkVFMjQzyMqj/img_item01.webp','https://en.onepiece-cardgame.com/products/collection-drama002.html'),
 ('cardcollection_bestselection_vol6','collection','Premium Card Collection -Best Selection Vol.6-','$25.00','August 2026','https://en.onepiece-cardgame.com/renewal/images/products/other/cardcollection_bestselection_vol6/img_item01.webp','https://en.onepiece-cardgame.com/products/other/cardcollection_bestselection_vol6.php'),
 ('3rd_anniversary_set','set','ONE PIECE CARD GAME English Version 3rd Anniversary Set','$120.00','August 2026','https://en.onepiece-cardgame.com/renewal/images/products/other/3rd_anniversary_set/img_item01.webp','https://en.onepiece-cardgame.com/products/other/3rd_anniversary_set.php'),
 ('cardcollection29th','collection','Premium Card Collection -29th Anniversary Edition-','$10.00',NULL,'https://en.onepiece-cardgame.com/onepiececg/bccard/en/product/2026/07/02/P8ICy7wt507KRvOw/img_products_en.webp','https://en.onepiece-cardgame.com/products/cardcollection29th.html');
