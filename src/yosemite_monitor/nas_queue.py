"""Durable NAS jobs. GCS generation preconditions arbitrate all state transitions."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import time
import uuid

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage

CONTROL = 'nas/control.json'
MAX_STATE = 2 * 1024 * 1024
MAX_PART = 16 * 1024 * 1024
MAX_JOB_BYTES = 128 * 1024 * 1024
ACTIVE = {'queued', 'running'}


class QueueError(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail
        super().__init__(detail)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def new_id():
    return uuid.uuid4().hex


def filters_hash(config):
    value = deepcopy(config)
    value.pop('revision', None)
    for field in ('schedule_enabled', 'query_interval_hours', 'openai_model'):
        value.get('settings', {}).pop(field, None)
    return digest(encoded(value))


class GCSStore:
    def __init__(self, bucket):
        self.bucket = storage.Client().bucket(bucket)

    def read_control(self):
        for _ in range(6):
            blob = self.bucket.blob(CONTROL)
            try:
                blob.reload(timeout=20)
                generation = blob.generation
                raw = blob.download_as_bytes(if_generation_match=generation, timeout=20)
                return json.loads(raw), generation
            except NotFound:
                return None, 0
            except PreconditionFailed:
                continue
        raise QueueError(409, '状态正在更新，请重试。')

    def cas(self, generation, state):
        raw = encoded(state)
        if len(raw) > MAX_STATE:
            raise QueueError(413, '任务状态超出上限，请检查积压任务。')
        try:
            self.bucket.blob(CONTROL).upload_from_string(raw, content_type='application/json',
                                                       if_generation_match=generation, timeout=30)
            return True
        except PreconditionFailed:
            return False

    def put(self, name, raw):
        try:
            self.bucket.blob(name).upload_from_string(raw, content_type='application/json',
                                                    if_generation_match=0, timeout=30)
        except PreconditionFailed:
            if self.get(name) != raw:
                raise QueueError(409, '同一结果编号包含不同数据。')
        return name

    def get(self, name):
        return self.bucket.blob(name).download_as_bytes(timeout=30)

    def prune(self, references, older_than):
        # Never apply a bucket-wide lifecycle policy or remove current pointers.
        for prefix in ('nas/parts/', 'nas/reports/', 'nas/baselines/', 'nas/search/'):
            for blob in self.bucket.list_blobs(prefix=prefix):
                if blob.name not in references and blob.time_created.timestamp() < older_than:
                    try:
                        blob.delete(if_generation_match=blob.generation, timeout=20)
                    except (NotFound, PreconditionFailed):
                        pass


class QueueService:
    def __init__(self, store, plan_builder, report_builder, search_builder, clock=time.time):
        self.store, self.plan_builder, self.report_builder = store, plan_builder, report_builder
        self.search_builder, self.clock = search_builder, clock

    def read(self):
        state, _ = self.store.read_control()
        if state is None:
            raise QueueError(503, 'NAS 队列尚未初始化。')
        return state

    def update(self, mutate):
        for _ in range(8):
            state, generation = self.store.read_control()
            if state is None:
                raise QueueError(503, 'NAS 队列尚未初始化。')
            original = deepcopy(state)
            result = mutate(state)
            if state == original or self.store.cas(generation, state):
                return result
        raise QueueError(409, '另一个请求正在更新任务，请重试。')

    def initialize(self, config, report=None, baseline=None):
        now = self.clock()
        config = deepcopy(config)
        config['revision'] = 1
        state = {'version': 1, 'config': config, 'jobs': {}, 'outbox': {}, 'device': {},
                 'latest_report': None, 'baseline': None, 'next_due_at': now,
                 'cooldowns': {}, 'last_gc_at': now}
        if report:
            state['latest_report'] = self.save_object('reports', report)
        if baseline:
            state['baseline'] = self.save_object('baselines', baseline)
        if not self.store.cas(0, state):
            raise QueueError(409, 'NAS 队列已经初始化，未覆盖现有状态。')
        return config

    def save_object(self, kind, value):
        raw = encoded(value)
        # A fresh object prevents GC racing with reuse of a formerly unreferenced
        # identical baseline whose original creation date is older than retention.
        return self.store.put(f'nas/{kind}/{new_id()}-{digest(raw)}.json', raw)

    def register_device(self, token):
        def change(s):
            for job in s['jobs'].values():
                if job['status'] == 'running':
                    job.update(status='queued', attempt_id=None, lease_until=0, parts={})
            s['device'] = {'id': 'synology-home', 'enabled': True,
                           'token_hash': digest(token.encode()), 'last_seen_at': None}
        self.update(change)

    def authenticate(self, token):
        device = self.read()['device']
        if (not token or not device.get('enabled') or
                not hmac.compare_digest(digest(token.encode()), device.get('token_hash', ''))):
            raise QueueError(401, 'Invalid device token')
        return device['id']

    def save_config(self, config, expected):
        # Validate all requested dates and provider IDs before mutating anything.
        if config.get('watched_campgrounds'):
            plan = self.plan_builder(config)
            if len(plan['requests']) > 200 or len(encoded(plan)) > 200 * 1024:
                raise QueueError(422, '查询范围过大，请减少营地数量或日期范围。')
        def change(s):
            if expected != s['config']['revision']:
                raise QueueError(409, '设置已在其他页面更新，请刷新后再保存。')
            changed_filters = filters_hash(config) != filters_hash(s['config'])
            updated = deepcopy(config)
            updated['revision'] = expected + 1
            s['config'] = updated
            s['next_due_at'] = self.clock()
            for job in s['jobs'].values():
                if job['status'] in ACTIVE:
                    self.finish_job(job, 'superseded', '查询条件已更新。')
            if changed_filters:
                s['baseline'] = None
                for mail in s['outbox'].values():
                    if mail['status'] in {'pending', 'leased'}:
                        mail['status'] = 'cancelled'
            return deepcopy(updated)
        return self.update(change)

    def finish_job(self, job, status, error=None):
        job.update(status=status, updated_at=self.clock(), error=error)
        job.pop('plan', None)
        job.pop('config', None)
        job.pop('parts', None)

    def expire(self, s):
        now = self.clock()
        for job in s['jobs'].values():
            if job['status'] in ACTIVE and job['expires_at'] <= now:
                self.finish_job(job, 'expired', '任务等待超时；NAS 恢复后会按最新条件定时查询。')
            elif job['status'] == 'running' and job['lease_until'] <= now:
                if job['attempts'] >= 3:
                    self.finish_job(job, 'failed', 'NAS 多次未完成任务，已保留上次成功结果。')
                    s['next_due_at'] = now + 300
                else:
                    job.update(status='queued', attempt_id=None, parts={}, updated_at=now)
        for mail in s['outbox'].values():
            if mail['status'] == 'leased' and mail['lease_until'] <= now:
                mail['status'] = 'pending'
            elif mail['status'] == 'sending' and mail['lease_until'] <= now:
                mail.update(status='unknown', error='发送确认超时；为避免重复邮件，不自动重发。')
        terminal = sorted((j for j in s['jobs'].values() if j['status'] not in ACTIVE),
                          key=lambda j: j['created_at'], reverse=True)
        for job in terminal[30:]:
            s['jobs'].pop(job['job_id'], None)
        finished = sorted((m for m in s['outbox'].values() if m['status'] in {'sent', 'cancelled'}),
                          key=lambda m: m['created_at'], reverse=True)
        for mail in finished[20:]:
            s['outbox'].pop(mail['id'], None)

    def add_job(self, s, trigger, kind='scan', query=None):
        now, config = self.clock(), s['config']
        for job in s['jobs'].values():
            if (job['status'] in ACTIVE and job['config_revision'] == config['revision']
                    and job['kind'] == kind and job.get('query') == query and job['trigger'] == trigger):
                return job
        if sum(j['status'] in ACTIVE for j in s['jobs'].values()) >= 5:
            raise QueueError(429, '已有查询等待 NAS，请等当前任务完成。')
        if kind == 'scan':
            plan = self.plan_builder(config)
        else:
            plan = self.search_builder(query)
        if not 1 <= len(plan['requests']) <= 200 or len(encoded(plan)) > 200 * 1024:
            raise QueueError(422, '查询范围过大，请减少营地数量或日期范围。')
        jid = new_id()
        job = {'job_id': jid, 'kind': kind, 'query': query, 'trigger': trigger, 'status': 'queued',
               'config_revision': config['revision'], 'config': deepcopy(config), 'plan': plan,
               'created_at': now, 'updated_at': now, 'expires_at': now + 1800, 'attempts': 0,
               'parts': {}, 'part_count': len(plan['requests']), 'received_count': 0, 'error': None}
        s['jobs'][jid] = job
        return job

    def tick_state(self, s):
        self.expire(s)
        now, config = self.clock(), s['config']
        if (config['settings']['schedule_enabled'] and config['watched_campgrounds'] and
                s['next_due_at'] <= now and not any(j['kind'] == 'scan' and j['status'] in ACTIVE
                                                    for j in s['jobs'].values()) and
                max(s['cooldowns'].values(), default=0) <= now):
            try:
                self.add_job(s, 'scheduled')
                s.pop('schedule_error', None)
            except (ValueError, QueueError) as exc:
                s['schedule_error'] = str(exc)[:200]
                s['next_due_at'] = now + 3600

    def enqueue(self, kind='scan', query=None):
        def change(s):
            self.expire(s)
            return self.public_job(self.add_job(s, 'manual', kind, query))
        return self.update(change)

    def claim(self, device_id):
        def change(s):
            now = self.clock()
            if not s['device'].get('enabled') or s['device'].get('id') != device_id:
                raise QueueError(401, 'Device disabled')
            self.tick_state(s)
            if not s['device'].get('last_seen_at') or now - s['device']['last_seen_at'] >= 360:
                s['device']['last_seen_at'] = now
            if any(j['status'] == 'running' for j in s['jobs'].values()):
                return None
            queued = sorted((j for j in s['jobs'].values() if j['status'] == 'queued'),
                            key=lambda j: (j['trigger'] == 'scheduled', j['created_at']))
            for job in queued:
                if max((s['cooldowns'].get(r['provider'], 0) for r in job['plan']['requests']), default=0) > now:
                    continue
                attempt = new_id()
                job.update(status='running', attempt_id=attempt, device_id=device_id, lease_until=now + 120,
                           started_at=now, attempts=job['attempts'] + 1, updated_at=now, parts={}, received_count=0)
                s['device']['last_seen_at'] = now
                return {'job_id': job['job_id'], 'attempt_id': attempt, 'kind': job['kind'],
                        'requests': deepcopy(job['plan']['requests'])}
            return None
        return self.update(change)

    def lease(self, s, jid, attempt, device_id, allow_done=False):
        job = s['jobs'].get(jid)
        if not s['device'].get('enabled') or s['device'].get('id') != device_id:
            raise QueueError(401, 'Device disabled')
        if not job or job.get('device_id') != device_id or job.get('attempt_id') != attempt:
            raise QueueError(409, '任务尝试已失效。')
        if allow_done and job['status'] == 'succeeded':
            return job
        if (job['status'] != 'running' or job['lease_until'] <= self.clock() or
                job['config_revision'] != s['config']['revision'] or job['expires_at'] <= self.clock()):
            raise QueueError(409, '任务已过期或设置已经更新。')
        return job

    def heartbeat(self, jid, attempt, device_id):
        def change(s):
            job = self.lease(s, jid, attempt, device_id)
            now = self.clock()
            job.update(lease_until=now + 120, updated_at=now)
            s['device']['last_seen_at'] = now
            return {'status': 'ok'}
        return self.update(change)

    def put_part(self, jid, attempt, device_id, key, raw):
        if len(raw) > MAX_PART or not raw:
            raise QueueError(413, '供应商响应为空或超出 16 MiB。')
        snapshot = self.read()
        job = self.lease(snapshot, jid, attempt, device_id)
        allowed = {digest(r['key'].encode()): r for r in job['plan']['requests']}
        if key not in allowed:
            raise QueueError(422, '响应不属于本次请求列表。')
        checksum = digest(raw)
        path = f'nas/parts/{jid}/{attempt}/{key}-{checksum}.json'
        self.store.put(path, raw)
        def change(s):
            current = self.lease(s, jid, attempt, device_id)
            existing = current['parts'].get(key)
            if existing and existing['sha256'] != checksum:
                raise QueueError(409, '重复上传的数据不一致。')
            current['parts'][key] = {'path': path, 'sha256': checksum, 'size': len(raw)}
            if sum(p['size'] for p in current['parts'].values()) > MAX_JOB_BYTES:
                raise QueueError(413, '本轮原始数据超出 128 MiB，请缩小查询范围。')
            current.update(received_count=len(current['parts']), updated_at=self.clock(), lease_until=self.clock() + 120)
            s['device']['last_seen_at'] = self.clock()
            return {'status': 'stored'}
        return self.update(change)

    def complete(self, jid, attempt, device_id):
        snapshot = self.read()
        job = self.lease(snapshot, jid, attempt, device_id, allow_done=True)
        if job['status'] == 'succeeded':
            return self.public_job(job)
        if set(job['parts']) != {digest(r['key'].encode()) for r in job['plan']['requests']}:
            raise QueueError(422, '查询数据不完整；上次成功结果已保留。')
        def payloads():
            for request in job['plan']['requests']:
                part = job['parts'][digest(request['key'].encode())]
                raw = self.store.get(part['path'])
                if digest(raw) != part['sha256']:
                    raise QueueError(422, '结果校验失败。')
                yield request, json.loads(raw)
        previous = json.loads(self.store.get(snapshot['baseline'])) if snapshot.get('baseline') else {}
        report, baseline = self.report_builder(job, payloads(), previous)
        result_path = self.save_object('search' if job['kind'] == 'search' else 'reports', report)
        baseline_path = self.save_object('baselines', baseline) if baseline is not None else None
        def change(s):
            current = self.lease(s, jid, attempt, device_id, allow_done=True)
            if current['status'] == 'succeeded':
                return self.public_job(current)
            if s.get('baseline') != snapshot.get('baseline'):
                raise QueueError(409, '通知基线已经更新，请重新提交结果。')
            current['result_path'] = result_path
            if current['kind'] == 'scan':
                s['latest_report'] = result_path
                if current['trigger'] == 'scheduled':
                    outstanding = sum(m['status'] not in {'sent', 'cancelled'} for m in s['outbox'].values())
                    if report['new_openings_count'] and outstanding < 32:
                        s['outbox'].setdefault(jid, {'id': jid, 'status': 'pending', 'kind': 'openings',
                            'report_path': result_path, 'created_at': self.clock(), 'attempts': 0,
                            'filters_hash': filters_hash(current['config']), 'next_try_at': self.clock()})
                    if not report['new_openings_count'] or outstanding < 32:
                        s['baseline'] = baseline_path
                    else:
                        current['notification_error'] = '通知积压过多；新空位将等待后续发送。'
                if current['trigger'] == 'scheduled':
                    interval = s['config']['settings']['query_interval_hours'] * 3600
                    s['next_due_at'] = max(self.clock() + 120, current['started_at'] + interval)
            self.finish_job(current, 'succeeded')
            s['device']['last_seen_at'] = self.clock()
            return self.public_job(current)
        return self.update(change)

    def fail(self, jid, attempt, device_id, provider, code, error):
        def change(s):
            job = self.lease(s, jid, attempt, device_id, allow_done=True)
            if job['status'] == 'succeeded':
                return self.public_job(job)
            if provider not in {'Recreation.gov', 'ReserveCalifornia', None}:
                raise QueueError(422, 'Unknown provider')
            cooldown = 1800 if code in (403, 429) else 300
            if provider and code in (403, 429):
                s['cooldowns'][provider] = self.clock() + cooldown
            detail = f'{provider or "NAS"} 查询失败' + (f'（HTTP {code}）' if code else '')
            self.finish_job(job, 'failed', detail + '；上次成功结果已保留。')
            s['next_due_at'] = self.clock() + cooldown
            return self.public_job(job)
        return self.update(change)

    @staticmethod
    def public_job(job):
        return {key: deepcopy(job.get(key)) for key in ('job_id', 'kind', 'trigger', 'status', 'query',
                'created_at', 'updated_at', 'config_revision', 'part_count', 'received_count', 'attempts',
                'error', 'notification_error')}

    def job_status(self, jid):
        job = self.read()['jobs'].get(jid)
        if not job:
            raise QueueError(404, '任务记录不存在或已过期。')
        result = self.public_job(job)
        if job['status'] == 'succeeded' and job['kind'] == 'search':
            result['result'] = json.loads(self.store.get(job['result_path']))
        return result

    def status(self):
        s = self.read()
        last = s['device'].get('last_seen_at')
        scans = sorted((j for j in s['jobs'].values() if j['kind'] == 'scan'),
                       key=lambda j: j['created_at'], reverse=True)
        return {'online': bool(last and self.clock() - last < 720), 'last_seen_at': last,
                'device_enabled': s['device'].get('enabled', False), 'poll_seconds': 120,
                'next_due_at': s['next_due_at'], 'latest_job': self.public_job(scans[0]) if scans else None,
                'mail_pending': sum(m['status'] in {'pending', 'leased', 'sending'} for m in s['outbox'].values()),
                'mail_unknown': sum(m['status'] == 'unknown' for m in s['outbox'].values()),
                'mailer_last_seen_at': s.get('mailer_last_seen_at'),
                'schedule_error': s.get('schedule_error'),
                'cooldowns': {k: v for k, v in s['cooldowns'].items() if v > self.clock()}}

    def report(self):
        state = self.read()
        path = state.get('latest_report')
        if not path:
            return None
        report = json.loads(self.store.get(path))
        mail = state['outbox'].get(report.get('job_id'))
        if mail:
            report['email_status'] = mail['status']
            report['email_messages_sent'] = int(mail['status'] == 'sent')
        return report

    def test_mail(self):
        mid = new_id()
        def change(s):
            if any(m['kind'] == 'test' and m['created_at'] > self.clock() - 300 for m in s['outbox'].values()):
                raise QueueError(429, '测试邮件已提交，请等待几分钟。')
            s['outbox'][mid] = {'id': mid, 'kind': 'test', 'status': 'pending', 'created_at': self.clock(),
                                'next_try_at': self.clock(), 'attempts': 0}
            return {'status': 'queued', 'id': mid}
        return self.update(change)

    def claim_mail(self):
        def change(s):
            self.tick_state(s)
            now = self.clock()
            s['mailer_last_seen_at'] = now
            for mail in sorted(s['outbox'].values(), key=lambda m: m['created_at']):
                if mail['status'] != 'pending' or mail.get('next_try_at', 0) > now:
                    continue
                if mail['kind'] != 'test' and mail['filters_hash'] != filters_hash(s['config']):
                    mail['status'] = 'cancelled'
                    continue
                mail.update(status='leased', attempt_id=new_id(), lease_until=now + 600,
                            attempts=mail['attempts'] + 1)
                return deepcopy(mail)
            return None
        mail = self.update(change)
        if mail and mail.get('report_path'):
            mail['report'] = json.loads(self.store.get(mail['report_path']))
        return mail

    def mail_update(self, mid, attempt, action, error=None):
        def change(s):
            mail = s['outbox'].get(mid)
            if not mail or mail.get('attempt_id') != attempt:
                raise QueueError(409, '通知尝试已经失效。')
            if action == 'sent' and mail['status'] == 'sent':
                return {'status': 'sent'}
            if action == 'begin':
                if (mail['status'] != 'leased' or mail['lease_until'] <= self.clock() or
                        (mail['kind'] != 'test' and mail['filters_hash'] != filters_hash(s['config']))):
                    raise QueueError(409, '通知已取消或过期。')
                mail.update(status='sending', lease_until=self.clock() + 600)
            elif action in {'sent', 'unknown', 'retry'}:
                if mail['status'] not in {'leased', 'sending'}:
                    raise QueueError(409, '通知状态已经变化。')
                status = 'pending' if action == 'retry' and mail['attempts'] < 3 else ('unknown' if action == 'retry' else action)
                mail.update(status=status, next_try_at=self.clock() + 1800, error=(error or '')[:200])
                if status == 'sent':
                    mail['sent_at'] = self.clock()
            else:
                raise QueueError(422, 'Unknown mail action')
            return {'status': mail['status']}
        return self.update(change)

    def maintain(self):
        def change(s):
            self.tick_state(s)
            if self.clock() - s.get('last_gc_at', 0) < 86400:
                return None
            s['last_gc_at'] = self.clock()
            refs = {s.get('latest_report'), s.get('baseline')}
            refs.update(m.get('report_path') for m in s['outbox'].values())
            refs.update(j.get('result_path') for j in s['jobs'].values())
            return refs - {None}
        references = self.update(change)
        if references is not None:
            self.store.prune(references, self.clock() - 7 * 86400)
        return {'status': 'ok'}
