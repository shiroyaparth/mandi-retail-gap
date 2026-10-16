-- Migration 004: Enhance price_anomaly table with confidence_level, retail_data_type, and anomaly_reason
-- Safe and additive. Preserves all existing columns and data.

ALTER TABLE price_anomaly 
    ADD COLUMN IF NOT EXISTS confidence_level VARCHAR(30) NOT NULL DEFAULT 'high',
    ADD COLUMN IF NOT EXISTS retail_data_type VARCHAR(20) NOT NULL DEFAULT 'scraped',
    ADD COLUMN IF NOT EXISTS anomaly_reason TEXT;
