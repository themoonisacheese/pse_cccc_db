-- One durable record for the tenth anniversary, independent of clue ingestion.
CREATE TABLE IF NOT EXISTS anniversary_announcements (
    key VARCHAR(80) PRIMARY KEY,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    body TEXT,
    attempted_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    message_id BIGINT
);
INSERT INTO anniversary_announcements (key) VALUES ('cccc-10-years')
ON CONFLICT (key) DO NOTHING;
