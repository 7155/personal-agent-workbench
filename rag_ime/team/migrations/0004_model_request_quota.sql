CREATE TABLE IF NOT EXISTS team_model_request_usage (
    day TEXT NOT NULL,
    owner_user_id TEXT NOT NULL REFERENCES team_users(id),
    space_id TEXT NOT NULL REFERENCES team_spaces(id),
    requests INTEGER NOT NULL CHECK(requests >= 0),
    PRIMARY KEY(day, owner_user_id, space_id)
);
