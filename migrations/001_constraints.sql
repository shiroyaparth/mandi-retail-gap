-- Run ONCE against the existing database. Safe to re-run.
-- Back up first (pg_dump). The only DELETE below removes exact duplicate retail rows.

-- 1) retail_price had no unique key, so "ON CONFLICT (crop_id, city, platform, price_date)"
--    in load_retail_manual.py / generate_retail_model.py fails with
--    "there is no unique or exclusion constraint matching the ON CONFLICT specification"
--    (reproduced against schema.sql on PostgreSQL 16).
--    Remove exact duplicates first (keeps the lowest retail_id of each group).
DELETE FROM retail_price a USING retail_price b
 WHERE a.retail_id > b.retail_id
   AND a.crop_id = b.crop_id AND a.city = b.city
   AND a.platform = b.platform AND a.price_date = b.price_date;

CREATE UNIQUE INDEX IF NOT EXISTS uq_retail_price_key
    ON retail_price (crop_id, city, platform, price_date);

-- 2) wholesale_price: NULL variety defeats UNIQUE (NULLs never compare equal), so every rerun
--    inserted a duplicate row (reproduced: 2 identical inserts -> 2 rows). Normalise existing
--    NULLs to 'Unknown'; the new loader writes 'Unknown' itself.
--    Rows that would collide with an existing 'Unknown' row are left untouched.
UPDATE wholesale_price w SET variety = 'Unknown'
 WHERE variety IS NULL
   AND NOT EXISTS (SELECT 1 FROM wholesale_price x
                    WHERE x.crop_id = w.crop_id AND x.mandi_id = w.mandi_id
                      AND x.price_date = w.price_date AND x.variety = 'Unknown');
