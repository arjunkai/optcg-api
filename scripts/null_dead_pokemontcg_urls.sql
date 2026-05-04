-- Null image URLs that pokemontcg.io's CDN doesn't actually host.
-- These 51 cards (mcd14/mcd15/mcd17/mcd18 + 3 xyp promos) were given
-- pokemontcg.io URLs by the pokemontcg-data submodule import, but the CDN
-- returns 404 for every number in those sets. Audit 2026-05-02 verified
-- via HEAD that mcd14/[1-24], mcd15/[1-24], mcd17/[1-24], mcd18/[1-24],
-- and xyp/{XY39,XY124,XY84,XY89} all 404. Nulling lets cardImage.js fall
-- through to the placeholder pane. Future weekly eBay residual backfill
-- will refill if listings exist.
UPDATE ptcg_cards SET image_high = NULL, image_low = NULL
WHERE lang = 'en' AND card_id IN ('2014xy-1','2014xy-10','2014xy-11','2014xy-12','2014xy-2','2014xy-3','2014xy-4','2014xy-5','2014xy-6','2014xy-7','2014xy-8','2014xy-9','2015xy-1','2015xy-10','2015xy-11','2015xy-12','2015xy-2','2015xy-3','2015xy-4','2015xy-5','2015xy-6','2015xy-7','2015xy-8','2015xy-9','2017sm-1','2017sm-10','2017sm-11','2017sm-12','2017sm-2','2017sm-3','2017sm-4','2017sm-5','2017sm-6','2017sm-7','2017sm-8','2017sm-9','2018sm-1','2018sm-10','2018sm-11','2018sm-12','2018sm-2','2018sm-3','2018sm-4','2018sm-5','2018sm-6','2018sm-7','2018sm-8','2018sm-9','xyp-XY39','xyp-XY46','xyp-XY68')
  AND image_high LIKE 'https://images.pokemontcg.io/%';
