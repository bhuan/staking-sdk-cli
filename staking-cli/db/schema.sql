-- Monad Staking Events Database Schema
-- PostgreSQL with TimescaleDB extension

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- Main events table
CREATE TABLE staking_events (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_name VARCHAR(50) NOT NULL,
    block_number BIGINT NOT NULL,
    transaction_hash VARCHAR(66) NOT NULL,

    -- Common fields (nullable for events that don't have them)
    validator_id BIGINT,
    address VARCHAR(42),  -- authAddress, delegator, or from field
    amount DECIMAL(78, 18),  -- Store in wei precision, can convert to MON
    epoch BIGINT,

    -- Additional event-specific data stored as JSONB
    event_data JSONB,

    -- Metadata
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Constraints
    CONSTRAINT valid_event_name CHECK (event_name IN (
        'ValidatorRewarded',
        'ValidatorCreated',
        'ValidatorStatusChanged',
        'Delegate',
        'Undelegate',
        'Withdraw',
        'ClaimRewards',
        'CommissionChanged',
        'EpochChanged'
    ))
);

-- Convert to TimescaleDB hypertable (partitioned by timestamp)
SELECT create_hypertable('staking_events', 'timestamp',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- Create indexes for common query patterns
CREATE INDEX idx_events_validator_id ON staking_events(validator_id) WHERE validator_id IS NOT NULL;
CREATE INDEX idx_events_address ON staking_events(address) WHERE address IS NOT NULL;
CREATE INDEX idx_events_block_number ON staking_events(block_number);
CREATE INDEX idx_events_event_name ON staking_events(event_name);
CREATE INDEX idx_events_tx_hash ON staking_events(transaction_hash);
CREATE INDEX idx_events_epoch ON staking_events(epoch) WHERE epoch IS NOT NULL;

-- Composite indexes for common filtering patterns
CREATE INDEX idx_events_validator_time ON staking_events(validator_id, timestamp DESC) WHERE validator_id IS NOT NULL;
CREATE INDEX idx_events_address_time ON staking_events(address, timestamp DESC) WHERE address IS NOT NULL;

-- GIN index for JSONB queries
CREATE INDEX idx_events_event_data ON staking_events USING GIN(event_data);

-- Unique constraint to prevent duplicate events (deduplication at DB level)
-- Note: Using MD5 hash of event_data for NULL-safe comparison
-- This prevents false duplicates when multiple events have NULL fields
CREATE UNIQUE INDEX idx_events_unique ON staking_events(
    block_number,
    transaction_hash,
    event_name,
    validator_id,
    address,
    amount,
    epoch,
    MD5(COALESCE(event_data::text, ''))
);

-- Compression policy (compress chunks older than 7 days)
SELECT add_compression_policy('staking_events', INTERVAL '7 days', if_not_exists => TRUE);

-- Retention policy (optional - keep data for 1 year, adjust as needed)
-- Uncomment if you want automatic data deletion
-- SELECT add_retention_policy('staking_events', INTERVAL '365 days', if_not_exists => TRUE);

-- Create materialized views for common analytics queries

-- 1. Validator statistics view
CREATE MATERIALIZED VIEW validator_stats AS
SELECT
    validator_id,
    COUNT(*) FILTER (WHERE event_name = 'Delegate') as delegation_count,
    SUM(amount) FILTER (WHERE event_name = 'Delegate') as total_delegated,
    COUNT(*) FILTER (WHERE event_name = 'Undelegate') as undelegation_count,
    SUM(amount) FILTER (WHERE event_name = 'Undelegate') as total_undelegated,
    COUNT(*) FILTER (WHERE event_name = 'ValidatorRewarded') as reward_count,
    SUM(amount) FILTER (WHERE event_name = 'ValidatorRewarded') as total_rewards,
    MAX(timestamp) as last_activity
FROM staking_events
WHERE validator_id IS NOT NULL
GROUP BY validator_id;

CREATE UNIQUE INDEX ON validator_stats(validator_id);

-- 2. Daily event summary
CREATE MATERIALIZED VIEW daily_event_summary AS
SELECT
    time_bucket('1 day', timestamp) as day,
    event_name,
    COUNT(*) as event_count,
    COUNT(DISTINCT validator_id) as unique_validators,
    COUNT(DISTINCT address) as unique_addresses,
    SUM(amount) as total_amount
FROM staking_events
GROUP BY day, event_name;

CREATE INDEX ON daily_event_summary(day DESC, event_name);

-- 3. Recent activity view (last 24 hours)
CREATE MATERIALIZED VIEW recent_activity AS
SELECT
    event_name,
    COUNT(*) as count,
    SUM(amount) as total_amount,
    COUNT(DISTINCT validator_id) as unique_validators
FROM staking_events
WHERE timestamp > NOW() - INTERVAL '24 hours'
GROUP BY event_name;

-- Refresh policies for materialized views (every 5 minutes)
CREATE OR REPLACE FUNCTION refresh_materialized_views()
RETURNS void AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY validator_stats;
    REFRESH MATERIALIZED VIEW CONCURRENTLY daily_event_summary;
    REFRESH MATERIALIZED VIEW recent_activity;
END;
$$ LANGUAGE plpgsql;

-- Helper functions for common queries

-- Get validator activity summary
CREATE OR REPLACE FUNCTION get_validator_activity(vid BIGINT, since TIMESTAMPTZ DEFAULT NOW() - INTERVAL '30 days')
RETURNS TABLE(
    event_type VARCHAR,
    event_count BIGINT,
    total_amount DECIMAL,
    first_event TIMESTAMPTZ,
    last_event TIMESTAMPTZ
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        event_name::VARCHAR,
        COUNT(*)::BIGINT,
        SUM(amount)::DECIMAL,
        MIN(timestamp)::TIMESTAMPTZ,
        MAX(timestamp)::TIMESTAMPTZ
    FROM staking_events
    WHERE validator_id = vid
        AND timestamp >= since
    GROUP BY event_name
    ORDER BY last_event DESC;
END;
$$ LANGUAGE plpgsql;

-- Get address delegation history
CREATE OR REPLACE FUNCTION get_address_delegations(addr VARCHAR)
RETURNS TABLE(
    validator_id BIGINT,
    event_name VARCHAR,
    amount DECIMAL,
    epoch BIGINT,
    timestamp TIMESTAMPTZ,
    transaction_hash VARCHAR
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        se.validator_id,
        se.event_name::VARCHAR,
        se.amount::DECIMAL,
        se.epoch,
        se.timestamp,
        se.transaction_hash::VARCHAR
    FROM staking_events se
    WHERE LOWER(se.address) = LOWER(addr)
        AND se.event_name IN ('Delegate', 'Undelegate', 'Withdraw', 'ClaimRewards')
    ORDER BY se.timestamp DESC;
END;
$$ LANGUAGE plpgsql;

-- Comments for documentation
COMMENT ON TABLE staking_events IS 'Stores all Monad staking precompile events from the blockchain';
COMMENT ON COLUMN staking_events.event_name IS 'Type of staking event (ValidatorRewarded, Delegate, etc.)';
COMMENT ON COLUMN staking_events.amount IS 'Amount in wei precision (divide by 1e18 for MON)';
COMMENT ON COLUMN staking_events.event_data IS 'Additional event-specific fields stored as JSON';
COMMENT ON INDEX idx_events_unique IS 'Prevents duplicate events from speculative execution';
