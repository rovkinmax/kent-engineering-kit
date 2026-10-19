from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import runpy
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
import uuid

from workflowkit import operations as op
REAL_STATUS = op._native_comments_status


TASK = 'task-11111111-1111-4111-8111-111111111111'
PROJECT = 'project-22222222-2222-4222-8222-222222222222'
WORKFLOW = '33333333-3333-4333-8333-333333333333'
COMMENT = 'comment-44444444-4444-4444-8444-444444444444'
SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'read-native-task-comments'


class NativeCommentsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.root = self.home / '.kent'
        (self.root / 'db').mkdir(parents=True)
        self.workspace = self.home / 'workspace'
        self.workspace.mkdir()
        self.kent = self.home / 'kent'
        self.kent.write_text('#!/bin/sh\nexit 1\n')
        self.kent.chmod(0o700)
        self.database = self.root / 'db' / 'main.sqlite3'
        self.writer = sqlite3.connect(self.database)
        self.addCleanup(self.writer.close)
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.writer.executescript('''
            CREATE TABLE task_records (id TEXT, project_id TEXT, workflow_id BLOB,
                source_workspace_id TEXT, managed_worktree_id TEXT);
            CREATE TABLE projects (id TEXT);
            CREATE TABLE workflows (id BLOB);
            CREATE TABLE workspaces (id TEXT, project_id TEXT, canonical_root_path TEXT);
            CREATE TABLE worktrees (id TEXT, workspace_id TEXT, canonical_root_path TEXT);
            CREATE TABLE task_comments (id TEXT, task_id TEXT, body TEXT,
                author_kind TEXT, author_id TEXT, created_at_unix_ms INTEGER, updated_at_unix_ms INTEGER);
        ''')
        self.writer.execute('INSERT INTO projects VALUES (?)', (PROJECT,))
        self.writer.execute('INSERT INTO workflows VALUES (?)', (uuid.UUID(WORKFLOW).bytes,))
        self.writer.execute('INSERT INTO workspaces VALUES (?,?,?)', ('workspace', PROJECT, str(self.workspace)))
        self.writer.execute('INSERT INTO task_records VALUES (?,?,?,?,NULL)',
                            (TASK, PROJECT, uuid.UUID(WORKFLOW).bytes, 'workspace'))
        self.insert_comment()
        self.writer.commit()
        self.status = {
            'endpoint': 'http://127.0.0.1:53082', 'health_status': 'ok', 'health_pid': 123,
            'command': [str(self.kent), 'serve', '--persistence-root', str(self.root)],
        }
        info = self.database.stat()
        exe = self.kent.stat()
        self.process = {
            'pid': 123, 'uid': os.geteuid(), 'start': 'start', 'argv': self.status['command'],
            'executable': [exe.st_dev, exe.st_ino], 'database': [info.st_dev, info.st_ino],
        }
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {'HOME': str(self.home)}))
        self.status_mock = self.stack.enter_context(mock.patch.object(op, '_native_comments_status', return_value=self.status))
        self.process_mock = self.stack.enter_context(mock.patch.object(op, '_native_process_evidence', return_value=self.process))
        self.listener_mock = self.stack.enter_context(mock.patch.object(
            op, '_native_listener_evidence', return_value={'pid': 123, 'inode': 'socket'}))

    def insert_comment(self, identity=COMMENT, body='approved', author='user', author_id=''):
        self.writer.execute('INSERT INTO task_comments VALUES (?,?,?,?,?,?,?)',
                            (identity, TASK, body, author, author_id, 10, 11))

    def context(self):
        return op.native_local_context(kent=str(self.kent), project_root=str(self.workspace))

    def read(self, context=None, **overrides):
        return op.read_native_task_comments(
            self.context() if context is None else context,
            **dict({'task_id': TASK, 'project_id': PROJECT, 'workflow_id': WORKFLOW}, **overrides),
        )

    def test_wal_committed_and_uncommitted_data_and_typed_author(self):
        # Keep the writer open, so the committed comment remains WAL-only.
        self.assertGreater(Path(str(self.database) + '-wal').stat().st_size, 0)
        self.insert_comment('comment-' + str(uuid.uuid4()), 'not committed')
        result = self.read()
        self.assertEqual(len(result['comments']), 1)
        self.assertEqual(set(result['comments'][0]), set(op.NATIVE_COMMENT_FIELDS))
        self.writer.rollback()
        self.writer.execute("UPDATE task_comments SET author_kind='agent',author_id='User'")
        self.writer.commit()
        self.assertEqual(self.read()['comments'][0]['author_kind'], 'agent')

    def test_additive_schema_is_compatible(self):
        self.writer.executescript('''
            ALTER TABLE task_comments ADD COLUMN ignored TEXT;
            CREATE TABLE irrelevant (future_column BLOB);
            CREATE INDEX future_index ON task_comments(body);
        ''')
        self.assertEqual(self.read()['comments'][0]['body'], 'approved')

    def test_task_records_view_preserves_used_type_contract(self):
        self.writer.executescript('''
            ALTER TABLE task_records RENAME TO tasks_source;
            CREATE VIEW task_records AS SELECT * FROM tasks_source;
        ''')
        self.assertEqual(len(self.read()['comments']), 1)

    def test_missing_or_incompatible_used_schema_blocks(self):
        for statement in (
            'ALTER TABLE task_comments RENAME COLUMN author_kind TO unknown',
            'ALTER TABLE task_comments RENAME TO hidden',
        ):
            with self.subTest(statement=statement):
                self.writer.execute('SAVEPOINT schema_test')
                self.writer.execute(statement)
                # DDL is uncommitted: reader still sees old committed schema.
                self.writer.execute('RELEASE schema_test')
                with self.assertRaises(op.OperationError):
                    self.read()
                if 'COLUMN' in statement:
                    self.writer.execute('ALTER TABLE task_comments RENAME COLUMN unknown TO author_kind')
                else:
                    self.writer.execute('ALTER TABLE hidden RENAME TO task_comments')
                self.writer.commit()

    def test_wrong_used_column_type_blocks(self):
        self.writer.executescript('''
            ALTER TABLE task_comments RENAME TO old_comments;
            CREATE TABLE task_comments (id TEXT, task_id TEXT, body TEXT,
                author_kind BLOB, author_id TEXT, created_at_unix_ms INTEGER, updated_at_unix_ms INTEGER);
            INSERT INTO task_comments SELECT * FROM old_comments;
        ''')
        with self.assertRaisesRegex(op.EffectBlocked, 'columns/types'):
            self.read()

    def test_unknown_author_and_invalid_values_fail_closed(self):
        for column, value in (
            ('author_kind', 'system'), ('author_kind', None), ('author_id', None),
            ('id', 'fake'), ('body', ''), ('created_at_unix_ms', -1),
            ('updated_at_unix_ms', 9), ('created_at_unix_ms', 'tomorrow'),
        ):
            with self.subTest(column=column, value=value):
                self.writer.execute(f'UPDATE task_comments SET {column}=?', (value,))
                self.writer.commit()
                with self.assertRaises(op.OperationError):
                    self.read()
                self.writer.execute('DELETE FROM task_comments')
                self.insert_comment()
                self.writer.commit()

    def test_duplicate_comments_block(self):
        self.insert_comment()
        self.writer.commit()
        with self.assertRaises(op.EffectBlocked):
            self.read()

    def test_count_body_and_aggregate_limits(self):
        for limit, value in (
            ('NATIVE_COMMENTS_MAX_COUNT', 0), ('NATIVE_COMMENTS_MAX_BYTES', 4),
        ):
            with self.subTest(limit=limit), mock.patch.object(op, limit, value):
                with self.assertRaises(op.EffectBlocked):
                    self.read()
        self.writer.execute('UPDATE task_comments SET body=?', ('x' * 262145,))
        self.writer.commit()
        with self.assertRaises(op.EffectBlocked):
            self.read()

    def test_null_field_cannot_bypass_aggregate_byte_limit(self):
        self.writer.execute('UPDATE task_comments SET id=NULL,author_id=?', ('x' * 8192,))
        self.writer.commit()
        with mock.patch.object(op, 'NATIVE_COMMENTS_MAX_BYTES', 4096):
            with self.assertRaises(op.EffectBlocked):
                self.read()

    def test_expired_snapshot_returns_no_authority(self):
        with mock.patch.object(op, 'NATIVE_COMMENTS_SNAPSHOT_SECONDS', -1):
            with self.assertRaises(op.EffectBlocked):
                self.read()

    def test_connection_is_read_only_and_issues_only_fixed_reads(self):
        connect = sqlite3.connect
        statements = []
        opened = []

        def observe(*args, **kwargs):
            self.assertIn('?mode=ro', args[0])
            self.assertNotIn('immutable', args[0])
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            opened.append(connection)
            return connection

        with mock.patch.object(op.sqlite3, 'connect', side_effect=observe):
            self.read()
        self.assertTrue(statements)
        self.assertTrue(all(statement.startswith(('SELECT ', 'PRAGMA ', 'BEGIN')) for statement in statements))
        self.assertFalse(any('journal_mode' in statement or 'checkpoint' in statement for statement in statements))
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')

    def test_missing_foreign_project_workflow_and_execution_roots(self):
        for kwargs in (
            {'task_id': 'task-' + str(uuid.uuid4())},
            {'project_id': 'project-' + str(uuid.uuid4())},
            {'workflow_id': str(uuid.uuid4())},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(op.EffectBlocked):
                self.read(**kwargs)
        self.writer.execute("UPDATE workspaces SET canonical_root_path='/other'")
        self.writer.commit()
        with self.assertRaises(op.EffectBlocked):
            self.read()

    def test_managed_worktree_native_binding_not_primary_root(self):
        self.writer.execute('INSERT INTO worktrees VALUES (?,?,?)', ('tree', 'workspace', str(self.workspace)))
        self.writer.execute("UPDATE workspaces SET canonical_root_path='/primary'")
        self.writer.execute("UPDATE task_records SET managed_worktree_id='tree'")
        self.writer.commit()
        self.assertEqual(len(self.read()['comments']), 1)
        self.writer.execute("UPDATE worktrees SET workspace_id='foreign'")
        self.writer.commit()
        with self.assertRaises(op.EffectBlocked):
            self.read()

    def test_discovery_and_all_readbacks_fix_local_environment(self):
        context = self.context()
        self.read(context)
        self.assertEqual(context['environment'], {
            'KENT_SERVER_HOST': '127.0.0.1', 'KENT_SERVER_PORT': '53082',
            'KENT_PERSISTENCE_ROOT': str(self.root),
        })
        for call in self.status_mock.call_args_list:
            self.assertEqual(call.args[2]['KENT_SERVER_HOST'], '127.0.0.1')
            self.assertEqual(call.args[2]['KENT_PERSISTENCE_ROOT'], str(self.root))

    def test_remote_tunnel_unsupported_missing_and_process_drift(self):
        for endpoint in ('http://remote:53082', 'http://localhost:53082', 'http://127.0.0.1:53082/path',
                         'http://127.0.0.1:0', None, 12):
            with self.subTest(endpoint=endpoint):
                self.status['endpoint'] = endpoint
                with self.assertRaises(op.EffectBlocked):
                    self.context()
        self.status['endpoint'] = 'http://127.0.0.1:53082'
        with mock.patch.object(op, '_native_listener_evidence', side_effect=op.EffectBlocked('foreign tunnel')):
            with self.assertRaises(op.EffectBlocked):
                self.context()
        with mock.patch.object(op, '_native_process_evidence', side_effect=[self.process, dict(self.process, start='new')]):
            with self.assertRaises(op.EffectBlocked):
                self.context()
        self.status['command'][-1] = '/foreign-root'
        with self.assertRaises(op.EffectBlocked):
            self.context()

    def test_fixed_status_subprocess_ignores_private_stderr(self):
        environment = self.context()['environment']
        with mock.patch.object(op, '_run', return_value=(0, json.dumps(self.status).encode(), b'')) as run:
            self.assertEqual(REAL_STATUS(self.kent, self.workspace, environment), self.status)
            self.assertEqual(run.call_args.kwargs['extra_env'], environment)
            self.assertEqual(run.call_args.args[0], [
                str(self.kent), 'service', 'status', '--json', '--persistence-root', str(self.root),
            ])
        with mock.patch.object(op, '_run', return_value=(1, b'', b'private provider token')):
            with self.assertRaises(op.EffectBlocked) as raised:
                REAL_STATUS(self.kent, self.workspace, environment)
            self.assertNotIn('private provider token', str(raised.exception))

    def test_unobserved_server_command_options_block(self):
        self.status['command'].extend(['--bg-logs-dir', '/logs'])
        with self.assertRaises(op.EffectBlocked):
            self.context()

    def test_context_closed_missing_drift_and_executable_replacement(self):
        context = self.context()
        for invalid in (dict(context, extra=True), {key: value for key, value in context.items() if key != 'process'}):
            with self.assertRaises(op.OperationError):
                self.read(invalid)
        self.kent.write_text('#!/bin/sh\nexit 2\n')
        with self.assertRaises(op.EffectBlocked):
            self.read(context)

    def test_final_context_drift_blocks_and_snapshot_is_closed(self):
        context = self.context()
        original = op.validate_native_local_context
        calls = []

        def validate(value):
            calls.append(value)
            if len(calls) == 2:
                # A SQLite snapshot must not span later native work.
                self.assertEqual(self.writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall(), [(0, 0, 0)])
                raise op.EffectBlocked('process changed')
            return original(value)

        with mock.patch.object(op, 'validate_native_local_context', side_effect=validate):
            with self.assertRaises(op.EffectBlocked):
                self.read(context)
        self.assertEqual(len(calls), 2)

    def test_new_snapshot_observes_changed_comments(self):
        first = self.read()
        self.writer.execute("UPDATE task_comments SET body='new decision',updated_at_unix_ms=12")
        self.writer.commit()
        second = self.read(first['context'])
        self.assertNotEqual(first['comments'], second['comments'])

    def test_symlink_missing_and_copied_database_context_block(self):
        context = self.context()
        self.writer.close()
        renamed = self.database.with_name('old.sqlite3')
        self.database.rename(renamed)
        with self.assertRaises(op.OperationError):
            self.read(context)
        self.database.symlink_to(renamed)
        with self.assertRaises(op.OperationError):
            self.context()
        self.database.unlink()
        self.database.write_bytes(renamed.read_bytes())
        with self.assertRaises(op.EffectBlocked):
            self.read(context)

    def test_corrupt_database_blocks_without_private_payload(self):
        self.writer.close()
        self.database.write_bytes(b'private content not SQLite')
        with self.assertRaises(op.EffectBlocked) as raised:
            self.read()
        self.assertNotIn('private content', str(raised.exception))

    def test_busy_database_has_short_timeout(self):
        self.writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.writer.execute('PRAGMA journal_mode=DELETE')
        self.writer.execute('BEGIN EXCLUSIVE')
        with self.assertRaises(op.EffectBlocked):
            self.read()
        self.writer.rollback()

    def test_cli_stable_json_and_no_partial_error_stdout(self):
        module = runpy.run_path(str(SCRIPT))
        with mock.patch.object(sys, 'argv', [str(SCRIPT), 'context', '--kent', str(self.kent),
                                            '--project-root', str(self.workspace)]):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(module['main'](), 0)
            context = json.loads(output.getvalue())
        args = [str(SCRIPT), 'comments', '--context-json', json.dumps(context),
                '--task-id', TASK, '--project-id', PROJECT, '--workflow-id', WORKFLOW]
        with mock.patch.object(sys, 'argv', args), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(module['main'](), 0)
            self.assertEqual(json.loads(output.getvalue())['comments'][0]['id'], COMMENT)
        self.writer.execute("UPDATE task_comments SET author_kind='private secret'")
        self.writer.commit()
        with mock.patch.object(sys, 'argv', args), redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
            self.assertEqual(module['main'](), 1)
            self.assertEqual(output.getvalue(), '')
            self.assertNotIn('private secret', error.getvalue())


class NativeSocketProofTests(unittest.TestCase):
    def test_darwin_requires_exact_owned_listener_and_ignores_queue_fields(self):
        payload = f'p123\nu{os.geteuid()}\nf12\ntIPv4\nn127.0.0.1:53082\nTST=LISTEN\nTQR=0\nTQS=0\n'.encode()
        with mock.patch.object(op, '_run', return_value=(0, payload, b'')):
            self.assertEqual(op._native_listener_evidence(123, 53082, Path('/'), system='darwin')['pid'], 123)
        for bad in (payload.replace(b'p123', b'p124'), payload.replace(b'127.0.0.1', b'*'), payload + payload):
            with mock.patch.object(op, '_run', return_value=(0, bad, b'')):
                with self.assertRaises(op.EffectBlocked):
                    op._native_listener_evidence(123, 53082, Path('/'), system='darwin')

    def test_linux_requires_socket_inode_in_pid_descriptors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process = root / '123'
            (process / 'net').mkdir(parents=True)
            (process / 'fd').mkdir()
            (process / 'net' / 'tcp').write_text(
                f'header\n0: 0100007F:CF5A 00000000:0000 0A 0:0 0:0 0 {os.geteuid()} 0 987\n')
            with self.assertRaises(op.EffectBlocked):
                op._native_listener_evidence(123, 53082, root, system='linux', proc_root=root)
            (process / 'fd' / '4').symlink_to('socket:[987]')
            proof = op._native_listener_evidence(123, 53082, root, system='linux', proc_root=root)
            self.assertEqual(proof['inode'], '987')

if __name__ == '__main__':
    unittest.main()
