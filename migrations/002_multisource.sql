-- Migration 002: Multi-source wholesale ingestion and audit logging
-- Additive only. Preserves all existing data and unique constraints.

-- 1) Add source column to wholesale_price (default 'datagov')
ALTER TABLE wholesale_price 
    ADD COLUMN IF NOT EXISTS source VARCHAR(50) NOT NULL DEFAULT 'datagov';

-- 2) Create ingestion_log table to track per-source runs
CREATE TABLE IF NOT EXISTS ingestion_log (
    log_id SERIAL PRIMARY KEY,
    source VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('success', 'failure', 'skipped', 'empty')),
    records_fetched INTEGER NOT NULL DEFAULT 0,
    records_inserted INTEGER NOT NULL DEFAULT 0,
    records_updated INTEGER NOT NULL DEFAULT 0,
    records_rejected INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_ingestion_log_source_started 
    ON ingestion_log (source, started_at DESC);
