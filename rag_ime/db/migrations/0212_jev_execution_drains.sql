-- Immutable receipts from Pi settlement and the existing causal resource owners.
CREATE TABLE agent_jev_execution_drains (
 dispatch_id TEXT PRIMARY KEY REFERENCES agent_jev_runtime_effects(effect_id),
 proof_json TEXT NOT NULL,
 recorded_at_ms INTEGER NOT NULL
);
