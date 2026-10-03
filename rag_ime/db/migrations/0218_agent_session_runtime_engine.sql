-- Engine choice belongs to the product Session, before its first Host binding.
-- Existing Sessions keep their classic transcript and are never converted.
ALTER TABLE agent_sessions
ADD COLUMN runtime_engine TEXT NOT NULL DEFAULT 'classic'
CHECK (runtime_engine IN ('classic', 'durable'));
