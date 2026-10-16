-- Migration 003: Expand retail_price data_type constraint to include 'official'
-- Safe and additive. Preserves all existing data.

ALTER TABLE retail_price DROP CONSTRAINT IF EXISTS retail_price_data_type_check;

ALTER TABLE retail_price ADD CONSTRAINT retail_price_data_type_check 
    CHECK (data_type IN ('scraped', 'modeled', 'official'));
