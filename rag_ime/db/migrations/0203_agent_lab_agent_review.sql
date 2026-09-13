-- Preserve all existing job identities, frozen inputs, receipts and ordering
-- while adding the independently executed Agent standard-review job kind.
CREATE TABLE agent_lab_golden_jobs_with_review (
    job_id TEXT PRIMARY KEY,
    suite_id TEXT NOT NULL REFERENCES agent_lab_golden_suites(suite_id),
    kind TEXT NOT NULL CHECK (kind IN ('draft', 'review', 'calibrate', 'experiment')),
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted')),
    payload_json TEXT NOT NULL,
    input_json TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    execution_started INTEGER NOT NULL DEFAULT 0 CHECK (execution_started IN (0, 1)),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
INSERT INTO agent_lab_golden_jobs_with_review
    (rowid, job_id, suite_id, kind, state, payload_json, input_json, cancel_requested, execution_started, created_at_ms, updated_at_ms)
SELECT rowid, job_id, suite_id, kind, state, payload_json, input_json, cancel_requested, execution_started, created_at_ms, updated_at_ms
FROM agent_lab_golden_jobs;
DROP TABLE agent_lab_golden_jobs;
ALTER TABLE agent_lab_golden_jobs_with_review RENAME TO agent_lab_golden_jobs;
CREATE INDEX agent_lab_golden_jobs_by_suite ON agent_lab_golden_jobs(suite_id, created_at_ms DESC);
CREATE TRIGGER agent_lab_golden_job_inputs_no_update
BEFORE UPDATE OF input_json ON agent_lab_golden_jobs
BEGIN
    SELECT RAISE(ABORT, 'Golden job inputs are immutable');
END;
