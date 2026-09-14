-- Add the account authorization fence introduced after the original 0008.
-- Existing grants have no trustworthy account revision, so they are revoked
-- during upgrade instead of being activated under a guessed default.
ALTER TABLE team_connection_grants
ADD COLUMN user_authorization_revision INTEGER NOT NULL DEFAULT 0
CHECK (user_authorization_revision >= 0);

UPDATE team_connection_grants
SET status = 'revoked',
    revoked_at_ms = COALESCE(
        revoked_at_ms,
        CAST(strftime('%s', 'now') AS INTEGER) * 1000
    )
WHERE status = 'active';
