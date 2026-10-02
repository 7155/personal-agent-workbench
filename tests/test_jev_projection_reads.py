"""Projection consistency and read cost on canonical stores, without a live Host."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict
import json
import sqlite3
import threading
from unittest.mock import patch

from tests.test_jev_host_application import JevHostFixture
from rag_ime.jev_tasks.policy import model_cards, normalize_policy
from rag_ime.jev_tasks.types import digest


class JevProjectionReadTests(JevHostFixture):
    def populated(self):
        created = self.create()
        self.app.tick()
        effect = self.app.projection(self.room['id'], created['graphId'])['effects'][0]
        for number in range(4):
            actor = self.room['participants'][number % 2]
            self.service.room_work.create(
                room_id=self.room['id'], objective=f'Child {number}', expected_output='Evidence',
                acceptance_criteria=['Verified'], current_owner_participant_id=actor['id'],
                created_by_participant_id=self.room['participants'][0]['id'],
                accountable_participant_id=self.room['participants'][0]['id'],
                client_message_id=f'child-{number}', root_turn_id=created['rootId'],
                parent_work_id=created['workItemId'], depth=2)
        return created, effect

    def reference(self, created):
        """Independent composition through the pre-existing individual read owners."""
        graph = created['graphId']
        snapshot = self.snapshot(created)
        view = self.app.driver.projection(graph, graph, self.app.executions(snapshot))
        with self.app.ledger.connection() as conn:
            ids = [r[0] for r in conn.execute('SELECT effect_id FROM agent_jev_runtime_effects WHERE graph_id=? ORDER BY updated_at_ms DESC LIMIT 200', (graph,))]
            events = [dict(r) for r in conn.execute('SELECT source_id,kind,state,result_json FROM agent_jev_owner_events WHERE graph_id=? ORDER BY created_at_ms DESC,rowid DESC LIMIT 100', (graph,))]
            owner = conn.execute('SELECT current_objective FROM agent_jev_host_roots WHERE graph_id=?', (graph,)).fetchone()
            superseded_by = dict(conn.execute(
                'SELECT old_task_id,new_task_id FROM agent_jev_task_supersessions WHERE graph_id=?', (graph,)))
            revision_rows = conn.execute(
                'SELECT * FROM agent_jev_task_revisions WHERE graph_id=? ORDER BY created_at_ms,revision_id',
                (graph,)).fetchall()
        active_ids = sorted(task.id for task in snapshot.tasks if task.id not in superseded_by)
        revisions = []
        for row in revision_rows:
            successors = json.loads(row['successor_json'])
            revisions.append({
                'revisionId': row['revision_id'], 'status': row['status'],
                'changedTaskId': row['changed_task_id'],
                'affectedTaskIds': json.loads(row['affected_json']),
                'retainedAcceptedTaskIds': json.loads(row['retained_accepted_json']),
                'requiredDispatchIds': json.loads(row['required_dispatch_ids_json']),
                'successorTaskIds': list(successors.values()), 'successors': successors,
                'createdAtMs': row['created_at_ms'], 'appliedAtMs': row['applied_at_ms'],
            })
        effects = [self.app.effects.get(key) for key in ids]
        for effect in effects:
            effect['executionStatus'] = self.app.execution_status(effect)
        policy = self.app.lifecycle.policy(graph)
        approval = self.app.lifecycle.plan_approval(graph)
        return dict(ok=True, mode='jev', **{**view, 'tasks': [
                        {**asdict(t), 'taskHash': digest(asdict(t)),
                         'activeVersion': t.id in active_ids,
                         'supersededByTaskId': superseded_by.get(t.id, '')}
                        for t in snapshot.tasks]},
                    # This offline evaluator never admits a native classifier.
                    pendingClassifications=[], classificationDrained=True,
                    stopped=bool(policy['stopped']), phase=policy['phase'],
                    requirementsRevision=policy['requirements_revision'],
                    currentRootObjective=owner[0] or snapshot.task(snapshot.root_work_id).objective,
                    roomId=snapshot.room_id,
                    rootAttachmentReceipts=self.app._input_attachments(
                        self.app.binding(snapshot.room_id, graph), policy),
                    activeTaskIds=active_ids, revisions=revisions,
                    participantRemovals=self.app.removal.projection(snapshot.room_id),
                    final=json.loads(policy['final_json']),
                    policy=normalize_policy(json.loads(policy['policy_json']), legacy=policy['policy_json'] == '{}'),
                    modelCards=model_cards(), edges=[asdict(e) for e in snapshot.edges
                                                     if e.prerequisite in active_ids and e.dependent in active_ids],
                    effects=effects, reclaims=[],
                    events=events, **({'planApproval': approval} if approval is not None else {}))

    @contextmanager
    def read_budget(self):
        original = sqlite3.connect
        reader_thread = threading.get_ident()
        connections = []
        denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                  sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE}
        def connect(*args, **kwargs):
            conn = original(*args, **kwargs)
            # The budget belongs to this synchronous projection call. Other
            # service workers may use SQLite concurrently, including writes.
            # Keep every connection on the calling thread in the budget so
            # accidental extra or cross-database reads still fail the test.
            if threading.get_ident() == reader_thread:
                conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
                connections.append(conn)
            return conn
        with patch('sqlite3.connect', side_effect=connect), patch.object(
            self.app.ledger, 'read_in_transaction', wraps=self.app.ledger.read_in_transaction
        ) as snapshots, patch.object(self.service.runtime, 'await_turn_settled') as settlement, patch.object(
            self.service.runtime, 'abort_turn'
        ) as abort:
            yield connections, snapshots
            settlement.assert_not_called()
            abort.assert_not_called()

    def test_projection_read_budget_does_not_restrict_unrelated_background_connections(self):
        created = self.create()
        errors = []

        def background_write():
            try:
                with closing(sqlite3.connect(":memory:")) as conn:
                    conn.execute("CREATE TABLE unrelated_background_job (id INTEGER)")
            except BaseException as exc:
                errors.append(exc)

        with self.read_budget() as (connections, snapshots):
            worker = threading.Thread(target=background_write)
            worker.start()
            try:
                self.app.projection(self.room['id'], created['graphId'])
            finally:
                worker.join(timeout=2.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(connections), 1)
        self.assertEqual(snapshots.call_count, 1)

    def test_full_projection_matches_existing_owners_in_one_read_transaction(self):
        created, _ = self.populated()
        with patch.object(self.service.runtime, 'is_turn_active', return_value=True):
            expected = self.reference(created)
            with self.read_budget() as (connections, snapshots):
                actual = self.app.projection(self.room['id'], created['graphId'])
            self.assertEqual(actual, expected)
            self.assertEqual(len(actual['tasks']), 5)
            self.assertEqual(len(connections), 1)
            self.assertEqual(snapshots.call_count, 1)
        self.assertEqual(len(self.calls), 1)

    def test_root_attachment_receipt_uses_the_projection_read_transaction(self):
        media = self.service.media.import_bytes(room_id=self.room['id'], data=b'Comparison source',
            mime_type='text/markdown', file_name='comparison.md')
        created = self.app.create(self.room['id'], {'clientMessageId': 'projection-file',
            'message': 'Compare the attached document', 'strategy': 'auto',
            'attachmentIds': [media['mediaId']]})
        with self.read_budget() as (connections, _snapshots):
            view = self.app.projection(self.room['id'], created['graphId'])
        self.assertEqual(len(connections), 1)
        self.assertEqual(view['rootAttachmentReceipts'][0]['mediaId'], media['mediaId'])

    def test_projection_reads_applied_successor_and_pending_revision_from_one_snapshot(self):
        created, _ = self.populated()
        graph_id = created['graphId']
        children = [task for task in self.snapshot(created).tasks
                    if task.id != created['workItemId']]
        old_id, pending_id = children[0].id, children[1].id
        actor = self.room['participants'][0]
        successor = self.service.room_work.create(
            room_id=self.room['id'], objective='Revised child',
            expected_output='Revised evidence', acceptance_criteria=['Revised proof'],
            current_owner_participant_id=actor['id'],
            created_by_participant_id=actor['id'],
            accountable_participant_id=actor['id'],
            client_message_id='projection-successor',
            root_turn_id=created['rootId'], parent_work_id=created['workItemId'], depth=2)
        new_id = successor['id']
        with self.app.ledger.connection(write=True) as conn:
            for command_id, task_id in (('fixture:applied', old_id),
                                        ('fixture:pending', pending_id)):
                conn.execute(
                    'INSERT INTO agent_jev_commands '
                    '(command_id,graph_id,intent_hash,operation,task_id,result_json,created_at_ms) '
                    'VALUES(?,?,?,?,?,?,?)',
                    (command_id, graph_id, command_id, 'revise_task', task_id, '{}', 10),
                )
            for revision_id, command_id, state, task_id, successor_json, created_ms, applied_ms in (
                ('revision:applied', 'fixture:applied', 'applied', old_id,
                 json.dumps({old_id: new_id}), 10, 11),
                ('revision:pending', 'fixture:pending', 'awaiting_drain', pending_id,
                 '{}', 20, 0),
            ):
                conn.execute(
                    'INSERT INTO agent_jev_task_revisions '
                    '(revision_id,graph_id,command_id,status,base_topology_revision,'
                    'base_requirements_revision,changed_task_id,request_json,affected_json,'
                    'retained_accepted_json,required_dispatch_ids_json,successor_json,'
                    'created_at_ms,applied_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (revision_id, graph_id, command_id, state, 0, 0, task_id, '{}',
                     json.dumps([task_id]), '[]', '[]', successor_json, created_ms, applied_ms),
                )
            conn.execute(
                'INSERT INTO agent_jev_task_supersessions '
                '(graph_id,old_task_id,new_task_id,revision_id,created_at_ms) '
                'VALUES(?,?,?,?,?)', (graph_id, old_id, new_id, 'revision:applied', 11),
            )
            conn.execute('INSERT INTO agent_jev_revision_targets VALUES(?,?)',
                         ('revision:pending', pending_id))
            conn.execute('INSERT INTO agent_jev_edges VALUES(?,?,?,?)',
                         (graph_id, old_id, pending_id, 'requires'))
            conn.execute('UPDATE agent_jev_graphs SET topology_revision=1 WHERE graph_id=?',
                         (graph_id,))
            conn.execute(
                'UPDATE agent_jev_host_roots SET requirements_revision=2,'
                'current_objective=? WHERE graph_id=?', ('Root objective version 2', graph_id),
            )
            self.service.room_work.retire_superseded_in_transaction(
                conn, work_id=old_id, actor_participant_id=actor['id'],
                reason='Replaced by revised child',
            )
        with patch.object(self.service.runtime, 'is_turn_active', return_value=True):
            expected = self.reference(created)
            with self.read_budget() as (connections, snapshots):
                actual = self.app.projection(self.room['id'], graph_id)
        self.assertEqual(actual, expected)
        self.assertEqual(actual['currentRootObjective'], 'Root objective version 2')
        self.assertNotIn(old_id, actual['activeTaskIds'])
        self.assertIn(new_id, actual['activeTaskIds'])
        self.assertIn(pending_id, actual['activeTaskIds'])
        old = next(task for task in actual['tasks'] if task['id'] == old_id)
        self.assertEqual((old['activeVersion'], old['supersededByTaskId']), (False, new_id))
        self.assertEqual([revision['status'] for revision in actual['revisions']],
                         ['applied', 'awaiting_drain'])
        self.assertEqual(actual['revisions'][0]['successorTaskIds'], [new_id])
        self.assertEqual(actual['revisions'][1]['successorTaskIds'], [])
        self.assertNotIn(old_id, [edge['prerequisite'] for edge in actual['edges']])
        self.assertEqual(len(connections), 1)
        self.assertEqual(snapshots.call_count, 1)

    def test_list_uses_one_read_and_does_not_build_a_graph(self):
        self.create()
        with self.read_budget() as (connections, snapshots):
            result = self.app.projection(self.room['id'])
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(len(connections), 1)
        snapshots.assert_not_called()

    def test_exact_live_identity_and_new_drain_are_rechecked_on_every_get(self):
        created, effect = self.populated()
        with patch.object(self.service.runtime, 'is_turn_active', return_value=True):
            first = self.app.projection(self.room['id'], created['graphId'])
        self.assertIn(created['workItemId'], first['running'])
        with patch.object(self.service.runtime, 'is_turn_active', return_value=False) as active:
            with self.read_budget():
                stale = self.app.projection(self.room['id'], created['graphId'])
            self.assertNotIn(created['workItemId'], stale['running'])
            self.assertEqual(stale, self.reference(created))
            active.assert_called_with(effect['request']['sessionId'], effect['receipt']['turnId'],
                                      client_message_id=effect['effectId'])
        with self.app.ledger.connection(write=True) as conn:
            conn.execute('INSERT INTO agent_jev_execution_drains VALUES(?,?,?)',
                         (effect['effectId'], json.dumps({'proofRef': 'isolated-drain', 'terminal': 'aborted'}), 1))
        with self.read_budget(), patch.object(self.service.runtime, 'is_turn_active') as active:
            drained = self.app.projection(self.room['id'], created['graphId'])
            active.assert_not_called()
        self.assertEqual(drained['effects'][0]['executionStatus'], 'drained')
        self.assertEqual(drained, self.reference(created))
        self.assertEqual(self.snapshot(created).task(created['workItemId']).state, 'active')

    def test_batched_activity_keeps_session_turn_and_terminal_boundaries(self):
        created, effect = self.populated()
        session, turn = effect['request']['sessionId'], effect['receipt']['turnId']
        other_session = next(s['id'] for s in self.sessions if s['id'] != session)
        cases = [(other_session, turn, 'turn_completed', 'admitted'),
                 (session, 'another-turn', 'turn_completed', 'admitted'),
                 (session, turn, 'tool_finished', 'running'),
                 (session, turn, 'turn_completed', 'unknown'),
                 (session, turn, 'tool_progress', 'unknown')]
        for number, (event_session, event_turn, kind, status) in enumerate(cases):
            with self.subTest(kind=kind, session=event_session, turn=event_turn):
                with self.app.ledger.connection(write=True) as conn:
                    sequence = conn.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM agent_runtime_events WHERE session_id=?', (event_session,)).fetchone()[0]
                    conn.execute('INSERT INTO agent_runtime_events(event_id,session_id,turn_id,sequence,event_type,created_at_ms) VALUES(?,?,?,?,?,?)',
                                 (f'activity-fixture:{number}', event_session, event_turn, sequence, kind, number))
                with patch.object(self.service.runtime, 'is_turn_active', return_value=True):
                    expected = self.reference(created)
                    with self.read_budget() as (connections, _):
                        actual = self.app.projection(self.room['id'], created['graphId'])
                self.assertEqual(actual, expected)
                self.assertEqual(actual['effects'][0]['executionStatus'], status)
                self.assertEqual(len(connections), 1)

    def test_database_read_is_closed_before_live_identity_observation(self):
        created, _ = self.populated()
        connection = self.app.ledger.connection
        open_reads = 0
        @contextmanager
        def tracked(*args, **kwargs):
            nonlocal open_reads
            with connection(*args, **kwargs) as conn:
                open_reads += 1
                try:
                    yield conn
                finally:
                    open_reads -= 1
        def observe(*args, **kwargs):
            self.assertEqual(open_reads, 0)
            return True
        with patch.object(self.app.ledger, 'connection', tracked), patch.object(
            self.service.runtime, 'is_turn_active', side_effect=observe
        ) as active:
            self.app.projection(self.room['id'], created['graphId'])
            active.assert_called()

    def test_wal_concurrent_commit_cannot_mix_snapshot_and_later_receipt_policy_reads(self):
        # Enable WAL only on this isolated fixture so a writer can commit while
        # the GET still holds its read view (rollback journals block the writer).
        db = sqlite3.connect(self.service.db_path)
        try:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        finally:
            db.close()
        created, effect = self.populated()
        before = self.app.projection(self.room['id'], created['graphId'])
        read_snapshot = self.app.ledger.read_in_transaction
        def snapshot_then_commit(conn, graph_id, controller_id):
            snapshot = read_snapshot(conn, graph_id, controller_id)
            # A separate real connection commits between the snapshot and the
            # rest of this GET. The next GET, not this one, must see that commit.
            with self.app.ledger.connection(write=True) as writer:
                writer.execute('UPDATE agent_jev_host_roots SET requirements_revision=requirements_revision+1 WHERE graph_id=?', (graph_id,))
                writer.execute('INSERT INTO agent_jev_execution_drains VALUES(?,?,?)',
                               (effect['effectId'], json.dumps({'proofRef': 'concurrent-drain', 'terminal': 'aborted'}), 1))
            return snapshot
        with patch.object(self.app.ledger, 'read_in_transaction', side_effect=snapshot_then_commit):
            during = self.app.projection(self.room['id'], created['graphId'])
        self.assertEqual(during, before)
        with self.read_budget() as (connections, snapshots):
            after = self.app.projection(self.room['id'], created['graphId'])
        self.assertEqual(after['requirementsRevision'], before['requirementsRevision'] + 1)
        self.assertEqual(after['effects'][0]['executionStatus'], 'drained')
        self.assertEqual(len(connections), 1)
        self.assertEqual(snapshots.call_count, 1)

    def test_wrong_room_graph_and_snapshot_identity_are_rejected(self):
        from dataclasses import replace
        from rag_ime.jev_tasks.types import GraphConflict
        created = self.create()
        sessions = [self.service.sessions.create(title=f'Other {n}') for n in range(2)]
        other = self.service.rooms.create(title='Another room', routing_policy='moderator', participants=[
            {'sessionId': session['id'], 'roleId': 'implementer', 'roleVersion': '1',
             'displayName': session['title'], 'collaborationRole': 'implementer'} for session in sessions])
        with self.read_budget(), self.assertRaisesRegex(ValueError, 'does not belong'):
            self.app.projection(other['id'], created['graphId'])
        snapshot = self.snapshot(created)
        with self.assertRaisesRegex(GraphConflict, 'identity mismatch'):
            self.app.driver.projection(created['graphId'], created['graphId'], {},
                                       snapshot=replace(snapshot, controller_id='another-controller'))

    def test_bound_dispatch_older_than_display_limit_still_controls_frontier(self):
        created, effect = self.populated()
        with self.app.ledger.connection(write=True) as conn:
            for number in range(201):
                conn.execute('INSERT INTO agent_jev_runtime_effects SELECT ?,graph_id,command_id,operation,request_json,\'not_sent\',receipt_json,updated_at_ms+? FROM agent_jev_runtime_effects WHERE effect_id=?',
                             (f'old-display-fixture:{number}', number + 1, effect['effectId']))
        with patch.object(self.service.runtime, 'is_turn_active', return_value=True):
            expected = self.reference(created)
            with self.read_budget() as (connections, snapshots):
                actual = self.app.projection(self.room['id'], created['graphId'])
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual['effects']), 200)
        self.assertNotIn(effect['effectId'], [e['effectId'] for e in actual['effects']])
        self.assertIn(created['workItemId'], actual['running'])
        self.assertEqual(len(connections), 1)
        self.assertEqual(snapshots.call_count, 1)
