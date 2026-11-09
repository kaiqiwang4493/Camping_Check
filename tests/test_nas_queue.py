import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from yosemite_monitor.nas_queue import QueueService, QueueError, digest
from yosemite_monitor import web


class MemoryStore:
    def __init__(self):
        self.state, self.generation, self.objects = None, 0, {}
        self.lock = threading.Lock()
        self.pruned = None

    def read_control(self):
        with self.lock:
            return copy.deepcopy(self.state), self.generation

    def cas(self, generation, state):
        with self.lock:
            if generation != self.generation:
                return False
            self.state = copy.deepcopy(state)
            self.generation += 1
            return True

    def put(self, name, raw):
        if name in self.objects and raw != self.objects[name]:
            raise QueueError(409, 'collision')
        self.objects[name] = raw
        return name

    def get(self, name):
        return self.objects[name]

    def prune(self, references, older_than):
        self.pruned = references, older_than


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.now = 10000
        self.store = MemoryStore()
        def plan(config):
            return {'requests': [{'key': 'one', 'provider': 'ReserveCalifornia'}]}
        def report(job, payloads, previous):
            data = list(payloads)[0][1]
            new = data['open'] and not previous.get('open')
            return {'new_openings_count': int(new), 'current_openings_count': int(data['open']),
                    'job_id': job['job_id']}, data
        self.q = QueueService(self.store, plan, report, lambda query: plan({}), clock=lambda: self.now)
        self.config = {'settings': {'schedule_enabled': True, 'query_interval_hours': 2},
                       'watched_campgrounds': [{'campground_id': '618'}]}
        self.q.initialize(self.config, report={'old': True})
        self.q.register_device('x' * 40)
        self.device = self.q.authenticate('x' * 40)

    def upload(self, job, value=True):
        self.q.put_part(job['job_id'], job['attempt_id'], self.device, digest(b'one'),
                        json.dumps({'open': value}).encode())

    def complete(self, job):
        return self.q.complete(job['job_id'], job['attempt_id'], self.device)

    def test_only_one_concurrent_claim(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(lambda _: self.q.claim(self.device), range(4)))
        self.assertEqual(sum(j is not None for j in claims), 1)

    def test_manual_dedup_and_baseline_untouched(self):
        first = self.q.enqueue()
        self.assertEqual(first['job_id'], self.q.enqueue()['job_id'])
        job = self.q.claim(self.device)
        self.upload(job)
        self.complete(job)
        self.assertIsNone(self.q.read()['baseline'])
        self.assertFalse(self.q.read()['outbox'])

    def test_scheduled_delivery_is_durable_and_idempotent(self):
        job = self.q.claim(self.device)
        self.upload(job)
        self.complete(job)
        self.complete(job)
        self.assertEqual(len(self.q.read()['outbox']), 1)
        self.assertIsNotNone(self.q.read()['baseline'])
        self.assertEqual(self.q.read()['next_due_at'], self.now + 7200)
        self.now += 7201
        again = self.q.claim(self.device)
        self.upload(again)
        self.complete(again)
        self.assertEqual(len(self.q.read()['outbox']), 1)

    def test_failed_incomplete_and_malformed_preserve_report(self):
        job = self.q.claim(self.device)
        with self.assertRaises(QueueError): self.complete(job)
        self.assertEqual(self.q.report(), {'old': True})
        self.q.fail(job['job_id'], job['attempt_id'], self.device, 'ReserveCalifornia', 403, 'HTTPError')
        self.assertEqual(self.q.report(), {'old': True})
        self.assertIsNone(self.q.claim(self.device))
        self.now += 1801
        self.assertIsNotNone(self.q.claim(self.device))

    def test_reclaim_rejects_old_attempt(self):
        old = self.q.claim(self.device)
        self.now += 121
        new = self.q.claim(self.device)
        self.assertEqual(old['job_id'], new['job_id'])
        self.assertNotEqual(old['attempt_id'], new['attempt_id'])
        with self.assertRaises(QueueError): self.upload(old)
        self.upload(new)
        self.complete(new)

    def test_config_revision_invalidates_job_and_conflict(self):
        job = self.q.claim(self.device)
        self.q.save_config(self.config, 1)
        with self.assertRaises(QueueError): self.upload(job)
        with self.assertRaises(QueueError): self.q.save_config(self.config, 1)
        self.assertEqual(self.q.job_status(job['job_id'])['status'], 'superseded')

    def test_token_rotation_revokes_old_token_and_attempt(self):
        job = self.q.claim(self.device)
        self.q.register_device('y' * 40)
        with self.assertRaises(QueueError): self.q.authenticate('x' * 40)
        with self.assertRaises(QueueError): self.upload(job)

    def test_duplicate_upload_must_match(self):
        job = self.q.claim(self.device)
        self.upload(job)
        self.upload(job)
        with self.assertRaises(QueueError): self.upload(job, False)
        self.complete(job)

    def test_email_lease_before_send_retries_after_expiry(self):
        self.q.test_mail()
        first = self.q.claim_mail()
        self.now += 601
        second = self.q.claim_mail()
        self.assertEqual(first['id'], second['id'])
        self.assertNotEqual(first['attempt_id'], second['attempt_id'])

    def test_email_sending_timeout_is_unknown_not_resent(self):
        self.q.test_mail()
        mail = self.q.claim_mail()
        self.q.mail_update(mail['id'], mail['attempt_id'], 'begin')
        self.now += 601
        self.assertIsNone(self.q.claim_mail())
        self.assertEqual(self.q.status()['mail_unknown'], 1)

    def test_sent_ack_idempotent(self):
        self.q.test_mail()
        mail = self.q.claim_mail()
        self.q.mail_update(mail['id'], mail['attempt_id'], 'begin')
        for _ in range(2):
            self.assertEqual(self.q.mail_update(mail['id'], mail['attempt_id'], 'sent')['status'], 'sent')
        self.assertIsNone(self.q.claim_mail())

    def test_filter_change_cancels_old_mail(self):
        job = self.q.claim(self.device)
        self.upload(job)
        self.complete(job)
        mail = self.q.claim_mail()
        self.config['settings']['stay_nights'] = 3
        self.q.save_config(self.config, 1)
        with self.assertRaises(QueueError): self.q.mail_update(mail['id'], mail['attempt_id'], 'begin')
        self.assertIsNone(self.q.read()['baseline'])

    def test_maintenance_protects_reports(self):
        self.now += 86401
        self.q.maintain()
        self.assertIn(self.q.read()['latest_report'], self.store.pruned[0])

    def test_worker_cannot_use_user_endpoints(self):
        from fastapi.testclient import TestClient
        app = web.create_app()
        app.dependency_overrides[web.get_queue] = lambda: self.q
        with patch.object(web.auth, 'verify_id_token', side_effect=ValueError('not Firebase')), patch.object(web, 'initialize_firebase'):
            client = TestClient(app)
            self.assertEqual(client.get('/api/config', headers={'Authorization': 'Bearer ' + 'x' * 40}).status_code, 401)
        self.assertEqual(client.post('/api/worker/claim').status_code, 401)
        self.assertEqual(client.post('/api/worker/claim', headers={'Authorization': 'Bearer ' + 'x' * 40}).status_code, 200)


class OIDCTests(unittest.TestCase):
    def test_signature_and_workflow_identity(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from types import SimpleNamespace
        import time
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        claims = dict(exp=now+300, iat=now, nbf=now, sub='repo:test/repo:ref:refs/heads/main',
                      iss='https://token.actions.githubusercontent.com', aud='camping-check-nas-mail',
                      repository='test/repo', repository_id='123', ref='refs/heads/main',
                      workflow_ref='test/repo/.github/workflows/nas-maintenance.yml@refs/heads/main',
                      event_name='workflow_dispatch')
        with patch.dict('os.environ', {'GITHUB_REPOSITORY': 'test/repo', 'GITHUB_REPOSITORY_ID': '123'}), patch.object(web, 'github_jwks') as jwks:
            jwks.return_value.get_signing_key_from_jwt.return_value = SimpleNamespace(key=key.public_key())
            token = jwt.encode(claims, key, algorithm='RS256')
            self.assertEqual(web.require_mailer('Bearer '+token)['repository_id'], '123')
            for field, value in [('aud', 'other'), ('repository_id', '456'), ('ref', 'refs/heads/fork'),
                                 ('workflow_ref', 'test/repo/.github/workflows/evil.yml@refs/heads/main'),
                                 ('event_name', 'pull_request')]:
                invalid = dict(claims, **{field: value})
                with self.assertRaises(web.HTTPException):
                    web.require_mailer('Bearer '+jwt.encode(invalid, key, algorithm='RS256'))


if __name__ == '__main__': unittest.main()
