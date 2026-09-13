ALTER TABLE eval_schedules ADD COLUMN control_state TEXT NOT NULL DEFAULT 'active'
    CHECK (control_state IN ('active', 'paused', 'cancelled'));
