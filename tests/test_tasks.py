import json
import tempfile
import unittest
from pathlib import Path

from akari_tasks import TaskStore, scope_of, split_full


def event(user='owner', group='', key='m1'):
    return dict(scene='group' if group else 'c2c', user_openid=user,
                group_openid=group, msg_id=key, key=key, ts=1, attachments=[])


class TaskStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = TaskStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_text_round_trip_without_punctuation(self):
        text = '```python\n' + '中文@x' * 2600 + '\n```'
        parts = split_full(text, 600)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(0 < len(p) <= 600 for p in parts))
        self.assertGreater(len(parts), 3)

    def test_submission_is_durable_idempotent_and_isolated(self):
        a = self.db.submit(event(key='one'), 'A\n@file', 90, str(self.root))
        same = self.db.submit(event(key='one'), 'changed', 90, str(self.root))
        b = self.db.submit(event(key='two'), 'B', 90, str(self.root))
        self.assertEqual(a['id'], same['id'])
        self.assertNotEqual(a['directory'], b['directory'])
        reopened = TaskStore(self.root)
        self.assertEqual(reopened.get(a['id'], scope_of(event()))['prompt'], 'A\n@file')
        self.assertIsNone(reopened.get(a['id'], scope_of(event(user='stranger'))))

    def test_group_members_have_separate_sessions(self):
        a = self.db.submit(event(user='a', group='g', key='a'), 'A', 90, str(self.root))
        b = self.db.submit(event(user='b', group='g', key='b'), 'B', 90, str(self.root))
        self.assertNotEqual(a['session_id'], b['session_id'])

    def test_claim_serializes_same_session_but_allows_other_scope(self):
        a = self.db.submit(event(key='a'), 'A', 90, str(self.root))
        self.db.submit(event(key='b'), 'B', 90, str(self.root))
        c = self.db.submit(event(user='other', key='c'), 'C', 90, str(self.root))
        self.assertEqual(self.db.claim()['id'], a['id'])
        self.assertEqual(self.db.claim()['id'], c['id'])
        self.assertIsNone(self.db.claim())

    def test_cancel_queued_never_runs(self):
        a = self.db.submit(event(), 'A', 90, str(self.root))
        self.db.cancel(a['id'], a['scope'])
        self.assertIsNone(self.db.claim())
        self.assertEqual(self.db.get(a['id'], a['scope'])['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
