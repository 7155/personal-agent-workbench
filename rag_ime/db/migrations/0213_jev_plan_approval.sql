-- Opt-in approval of one complete plan. Existing Roots retain automatic execution.
CREATE TABLE agent_jev_plan_approvals (
 graph_id TEXT PRIMARY KEY REFERENCES agent_jev_graphs(graph_id),
 status TEXT NOT NULL CHECK(status IN ('planning','awaiting_input','awaiting_approval','deferred','approved')),
 requirements_revision INTEGER NOT NULL DEFAULT 1,
 plan_hash TEXT NOT NULL DEFAULT '',
 proposal_json TEXT NOT NULL DEFAULT '{}',
 planner_dispatch_id TEXT NOT NULL DEFAULT '',
 revisions_json TEXT NOT NULL DEFAULT '[]',
 updated_at_ms INTEGER NOT NULL
);
