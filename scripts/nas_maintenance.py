#!/usr/bin/env python3
"""GitHub OIDC authenticated outbox sender. Uses only the Python standard library."""
import json
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, getaddresses
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


class Client:
    def __init__(self):
        self.base = os.environ['NAS_API_BASE'].rstrip('/')
        parsed = urlsplit(self.base)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.path:
            raise ValueError('NAS_API_BASE must be an HTTPS origin')
        self.token = None
        self.expires = 0

    def oidc(self):
        if time.time() < self.expires:
            return self.token
        url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
        url += ('&' if '?' in url else '?') + urlencode({'audience': 'camping-check-nas-mail'})
        req = Request(url, headers={'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
        with urlopen(req, timeout=20) as response:
            self.token = json.load(response)['value']
        self.expires = time.time() + 180
        return self.token

    def call(self, path, body=None):
        req = Request(self.base + '/api/internal/' + path, data=json.dumps(body or {}).encode(),
                      headers={'Authorization': 'Bearer ' + self.oidc(), 'Content-Type': 'application/json'})
        with urlopen(req, timeout=60) as response:
            return None if response.status == 204 else json.load(response)

    def ack(self, mail, action, error=None):
        for attempt in range(3):
            try:
                return self.call('mail/' + mail['id'], {'attempt_id': mail['attempt_id'],
                                                      'action': action, 'error': error})
            except (URLError, TimeoutError, OSError) as exc:
                if isinstance(exc, HTTPError) and exc.code < 500 or attempt == 2:
                    raise
                time.sleep(2 ** attempt)


def message_for(mail, sender, recipients):
    message = EmailMessage()
    message['From'], message['To'] = sender, ', '.join(recipients)
    message['Date'] = formatdate(localtime=False)
    message['Message-ID'] = '<camping-' + mail['id'] + '@camping-check-3cad5.web.app>'
    if mail['kind'] == 'test':
        message['Subject'] = 'Camping Check：NAS 邮件通知测试'
        body = '这是一封测试邮件，用于验证 NAS 监控的通知链路。\n没有预订或锁定任何营位。'
    else:
        report = mail['report']
        rows = report['new_openings']
        message['Subject'] = 'Camping Check：发现 {} 个新的露营窗口'.format(len(rows))
        lines = ['发现符合已保存条件的新空位（{}）。'.format(report.get('generated_at_display', '')),
                 '余位可能随时变化，请打开官方链接查看和预订；本邮件不代表已预留。', '']
        for row in rows[:150]:
            lines.extend(['{} · {} · Site {}'.format(row.get('provider', ''), row['campground_name'], row['site']),
                          '{} · {} 晚'.format(row.get('stay_dates') or row['date'], row['nights']), row['url'], ''])
        if len(rows) > 150:
            lines.append('邮件显示前 150 条，完整结果请在网站查看。')
        body = '\n'.join(lines)
    message.set_content(body + '\n\n查看监控：https://camping-check-3cad5.web.app\n')
    return message


def deliver(client, mail):
    user = os.environ.get('GMAIL_SMTP_USER', '')
    password = os.environ.get('GMAIL_SMTP_APP_PASSWORD', '').replace(' ', '')
    recipients = [address for _, address in getaddresses([os.environ.get('EMAIL_TO', '')]) if address]
    sender = os.environ.get('EMAIL_FROM') or user
    if not user or not password or not recipients:
        client.ack(mail, 'retry', 'Email configuration is incomplete')
        return 'retry'
    smtp = None
    sending = False
    try:
        message = message_for(mail, sender, recipients)
        smtp = smtplib.SMTP('smtp.gmail.com', 587, timeout=30)
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.login(user, password)
        # A lost begin response is ambiguous: do not send without the acknowledgement.
        client.call('mail/' + mail['id'], {'attempt_id': mail['attempt_id'], 'action': 'begin'})
        sending = True
        refused = smtp.send_message(message)
    except Exception as exc:
        # send_message raises these only when no recipient/data was accepted.
        definitely_rejected = isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                                               smtplib.SMTPDataError))
        action = 'retry' if not sending or definitely_rejected else 'unknown'
        # Never dump SMTP responses, tokens, or recipient addresses into Actions logs.
        try:
            client.ack(mail, action, type(exc).__name__)
        except Exception:
            pass  # Lease expiry preserves ambiguous sends as unknown.
        return action
    finally:
        if smtp is not None:
            smtp.close()
    # Partial acceptance must not be retried for the accepted recipients.
    client.ack(mail, 'sent', 'Some recipients were refused' if refused else None)
    return 'sent'


def main():
    client = Client()
    client.call('tick')
    counts = {}
    for _ in range(10):
        mail = client.call('mail/claim')
        if not mail:
            break
        outcome = deliver(client, mail)
        counts[outcome] = counts.get(outcome, 0) + 1
    print('NAS maintenance complete; notification outcomes:', json.dumps(counts))
    if counts.get('retry') or counts.get('unknown'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
