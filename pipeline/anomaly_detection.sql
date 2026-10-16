-- KrishiPulse Anomaly Detection Engine v2.0
-- Step 1: Clear existing anomaly rows before re-inserting (idempotent run)
DELETE FROM price_anomaly;

-- Step 2: Documented City to District mapping for Gujarat target consumption centers
WITH city_district_map AS (
    SELECT 'Ahmedabad' AS city, 'Ahmedabad' AS district UNION ALL
    SELECT 'Surat', 'Surat' UNION ALL
    SELECT 'Vadodara', 'Vadodara' UNION ALL
    SELECT 'Rajkot', 'Rajkot' UNION ALL
    SELECT 'Gandhinagar', 'Gandhinagar' UNION ALL
    SELECT 'Bhavnagar', 'Bhavnagar'
),

-- Step 3: Aggregate wholesale prices per crop + district + day (handling grades & mandis)
wholesale_daily AS (
    SELECT
        w.crop_id,
        MIN(w.mandi_id) AS mandi_id,
        TRIM(REGEXP_REPLACE(m.district, '\s*\(.*?\)', '')) AS district,
        w.price_date,
        ROUND(AVG(w.modal_price)::numeric, 2) AS wholesale_price
    FROM wholesale_price w
    JOIN mandi_master m ON w.mandi_id = m.mandi_id
    WHERE w.is_provisional = false
      AND m.district IS NOT NULL
    GROUP BY w.crop_id, TRIM(REGEXP_REPLACE(m.district, '\s*\(.*?\)', '')), w.price_date
),

-- Step 4: Rolling time-based 30-day baseline and stats for wholesale
wholesale_stats AS (
    SELECT
        wd.crop_id,
        wd.mandi_id,
        wd.district,
        wd.price_date,
        wd.wholesale_price,
        AVG(wd.wholesale_price) OVER (
            PARTITION BY wd.crop_id, wd.district
            ORDER BY wd.price_date
            RANGE BETWEEN INTERVAL '30 days' PRECEDING AND INTERVAL '1 day' PRECEDING
        ) AS wholesale_baseline,
        STDDEV(wd.wholesale_price) OVER (
            PARTITION BY wd.crop_id, wd.district
            ORDER BY wd.price_date
            RANGE BETWEEN INTERVAL '30 days' PRECEDING AND INTERVAL '1 day' PRECEDING
        ) AS wholesale_stddev,
        COUNT(*) OVER (
            PARTITION BY wd.crop_id, wd.district
            ORDER BY wd.price_date
            RANGE BETWEEN INTERVAL '30 days' PRECEDING AND INTERVAL '1 day' PRECEDING
        ) AS preceding_count
    FROM wholesale_daily wd
),

wholesale_scored AS (
    SELECT
        ws.*,
        CASE
            WHEN ws.preceding_count >= 5 AND ws.wholesale_stddev > 0
                THEN ROUND(((ws.wholesale_price - ws.wholesale_baseline) / ws.wholesale_stddev)::numeric, 2)
            WHEN ws.preceding_count > 0 AND ws.wholesale_stddev > 0
                THEN ROUND(((ws.wholesale_price - ws.wholesale_baseline) / ws.wholesale_stddev)::numeric, 2)
            ELSE 0.00
        END AS wholesale_zscore,
        CASE
            WHEN ws.preceding_count >= 5 THEN 'high'
            WHEN ws.preceding_count > 0 THEN 'low'
            ELSE 'insufficient_history'
        END AS ws_confidence
    FROM wholesale_stats ws
),

-- Step 5: Retail rolling time-based 30-day baseline and stats per crop + city
retail_daily AS (
    SELECT
        crop_id,
        city,
        price_date,
        data_type,
        ROUND(AVG(price)::numeric, 2) AS retail_price
    FROM retail_price
    GROUP BY crop_id, city, price_date, data_type
),

retail_stats AS (
    SELECT
        rd.crop_id,
        rd.city,
        rd.price_date,
        rd.data_type,
        rd.retail_price,
        AVG(rd.retail_price) OVER (
            PARTITION BY rd.crop_id, rd.city
            ORDER BY rd.price_date
            RANGE BETWEEN INTERVAL '30 days' PRECEDING AND INTERVAL '1 day' PRECEDING
        ) AS retail_baseline,
        STDDEV(rd.retail_price) OVER (
            PARTITION BY rd.crop_id, rd.city
            ORDER BY rd.price_date
            RANGE BETWEEN INTERVAL '30 days' PRECEDING AND INTERVAL '1 day' PRECEDING
        ) AS retail_stddev,
        COUNT(*) OVER (
            PARTITION BY rd.crop_id, rd.city
            ORDER BY rd.price_date
            RANGE BETWEEN INTERVAL '30 days' PRECEDING AND INTERVAL '1 day' PRECEDING
        ) AS preceding_count
    FROM retail_daily rd
),

retail_scored AS (
    SELECT
        rs.*,
        CASE
            WHEN rs.preceding_count >= 5 AND rs.retail_stddev > 0
                THEN ROUND(((rs.retail_price - rs.retail_baseline) / rs.retail_stddev)::numeric, 2)
            WHEN rs.preceding_count > 0 AND rs.retail_stddev > 0
                THEN ROUND(((rs.retail_price - rs.retail_baseline) / rs.retail_stddev)::numeric, 2)
            ELSE 0.00
        END AS retail_zscore,
        CASE
            WHEN rs.preceding_count >= 5 THEN 'high'
            WHEN rs.preceding_count > 0 THEN 'low'
            ELSE 'insufficient_history'
        END AS rs_confidence
    FROM retail_stats rs
),

-- Step 6: Join wholesale to retail via city->district mapping
joined_pairs AS (
    SELECT
        ws.crop_id,
        ws.mandi_id,
        ws.district,
        rs.city,
        ws.price_date,
        ws.wholesale_price,
        ROUND(COALESCE(ws.wholesale_baseline, ws.wholesale_price)::numeric, 2) AS wholesale_baseline,
        ws.wholesale_zscore,
        ws.ws_confidence,
        rs.retail_price,
        ROUND(COALESCE(rs.retail_baseline, rs.retail_price)::numeric, 2) AS retail_baseline,
        rs.retail_zscore,
        rs.rs_confidence,
        rs.data_type AS retail_data_type,
        ROUND(((rs.retail_price - ws.wholesale_price) / NULLIF(ws.wholesale_price, 0) * 100)::numeric, 2) AS wedge_pct
    FROM wholesale_scored ws
    JOIN city_district_map cdm ON ws.district = cdm.district
    JOIN retail_scored rs
        ON ws.crop_id = rs.crop_id
        AND ws.price_date = rs.price_date
        AND rs.city = cdm.city
)

INSERT INTO price_anomaly (
    crop_id, mandi_id, district, city, price_date,
    wholesale_price, wholesale_baseline, wholesale_zscore,
    retail_price, retail_baseline, retail_zscore,
    wedge_pct, anomaly_flag,
    confidence_level, retail_data_type, anomaly_reason
)
SELECT
    jp.crop_id,
    jp.mandi_id,
    jp.district,
    jp.city,
    jp.price_date,
    jp.wholesale_price,
    jp.wholesale_baseline,
    jp.wholesale_zscore,
    jp.retail_price,
    jp.retail_baseline,
    jp.retail_zscore,
    jp.wedge_pct,
    -- Flagging rules:
    -- (f) Modeled retail NEVER triggers anomaly flag
    -- (d) Insufficient history rows do not trigger false alarm anomaly flags
    CASE
        WHEN jp.retail_data_type = 'modeled' THEN false
        WHEN jp.ws_confidence = 'insufficient_history' OR jp.rs_confidence = 'insufficient_history' THEN false
        WHEN (jp.wholesale_zscore IS NOT NULL AND ABS(jp.wholesale_zscore) > 2.0)
          OR (jp.retail_zscore IS NOT NULL AND ABS(jp.retail_zscore) > 2.0)
          OR (jp.wedge_pct IS NOT NULL AND (jp.wedge_pct > 120.0 OR jp.wedge_pct < -20.0))
        THEN true
        ELSE false
    END AS anomaly_flag,
    -- Overall confidence level:
    CASE
        WHEN jp.ws_confidence = 'insufficient_history' OR jp.rs_confidence = 'insufficient_history' THEN 'insufficient_history'
        WHEN jp.ws_confidence = 'low' OR jp.rs_confidence = 'low' THEN 'low'
        ELSE 'high'
    END AS confidence_level,
    jp.retail_data_type,
    -- Plain English explanation:
    CASE
        WHEN jp.retail_data_type = 'modeled'
            THEN 'Modeled retail estimate: observation-only (anomaly flag suppressed)'
        WHEN jp.ws_confidence = 'insufficient_history' OR jp.rs_confidence = 'insufficient_history'
            THEN 'Insufficient baseline history (<5 days); baseline computed with available data'
        WHEN jp.wholesale_zscore > 2.0 AND jp.retail_zscore > 2.0
            THEN 'Synchronized surge: both wholesale and retail >2σ above 30-day baseline'
        WHEN jp.wholesale_zscore > 2.0
            THEN 'Wholesale mandi price spike (>2σ above 30-day baseline)'
        WHEN jp.retail_zscore > 2.0
            THEN 'Retail price spike (>2σ above 30-day baseline)'
        WHEN jp.wholesale_zscore < -2.0
            THEN 'Wholesale mandi price collapse (<-2σ below 30-day baseline)'
        WHEN jp.wedge_pct > 120.0
            THEN 'Extreme retail wedge (>120% markup over wholesale mandi rate)'
        WHEN jp.wedge_pct < 0.0
            THEN 'Negative price wedge (retail lower than wholesale rate)'
        ELSE 'Normal price variation within expected 30-day baseline bounds'
    END AS anomaly_reason
FROM joined_pairs jp;