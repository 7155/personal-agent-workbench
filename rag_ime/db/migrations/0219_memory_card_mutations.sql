-- Receipts fence retries; facts remain exclusively in versioned memory_atoms.
ALTER TABLE memory_atoms ADD COLUMN user_edit_revision INTEGER NOT NULL DEFAULT 0;
CREATE TABLE memory_card_mutation_receipts (
    client_request_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
    result_json TEXT NOT NULL CHECK(json_valid(result_json)),
    created_at_ms INTEGER NOT NULL
);
