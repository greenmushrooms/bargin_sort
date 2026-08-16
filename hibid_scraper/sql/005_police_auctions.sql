-- Source: police_auctions
--
-- Same shape as raw.hibid — job, item, payload — because that is all a landing
-- table needs. The payloads differ completely; conforming them is silver's job.

CREATE TABLE IF NOT EXISTS raw.police_auctions (
    id           BIGSERIAL,
    sys_run_name VARCHAR(255) NOT NULL,
    item_id      VARCHAR(255) NOT NULL,
    category     VARCHAR(255),
    raw_json     JSONB        NOT NULL,
    scraped_at   TIMESTAMPTZ  NOT NULL,
    created_at   TIMESTAMPTZ  DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id, scraped_at)
) PARTITION BY RANGE (scraped_at);

CREATE INDEX IF NOT EXISTS idx_police_auctions_item_id      ON raw.police_auctions (item_id);
CREATE INDEX IF NOT EXISTS idx_police_auctions_scraped_at   ON raw.police_auctions (scraped_at);
CREATE INDEX IF NOT EXISTS idx_police_auctions_sys_run_name ON raw.police_auctions (sys_run_name);
CREATE INDEX IF NOT EXISTS idx_police_auctions_gin          ON raw.police_auctions USING GIN (raw_json);
