"""Bounded Gmail REST reader with durable history and message-fetch queues."""
import base64
import email.message
import email.policy
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class GmailAPI:
    def __init__(self, runtime):
        self.a = runtime
        self.token, self.expires, self.credentials = None, 0, None

    def json_request(self, request, limit=524288):
        try:
            with urllib.request.build_opener(self.a.NoRedirect()).open(request, timeout=15) as response:
                raw = response.read(limit + 1)
            if len(raw) > limit:
                raise self.a.Failure('gmail_response_too_large')
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except urllib.error.HTTPError as exc:
            delay = exc.headers.get('Retry-After', '0') if exc.headers else '0'
            raise self.a.Failure('gmail_http_' + str(exc.code), int(delay) if delay.isdigit() else 0) from None
        except (OSError, ValueError):
            raise self.a.Failure('gmail_network_or_response_error') from None

    def account(self):
        return self.a.secret('gmail')['email'].strip().lower()

    def refresh(self):
        cfg = self.a.secret('gmail_oauth')
        credentials = self.a.credential_digest({'oauth': cfg, 'email': self.account()})
        if self.token and credentials == self.credentials and time.monotonic() < self.expires:
            return
        self.token = None
        request = urllib.request.Request('https://oauth2.googleapis.com/token',
            data=urllib.parse.urlencode({k: cfg[k] for k in ('client_id', 'client_secret', 'refresh_token')} |
                                       {'grant_type': 'refresh_token'}).encode(),
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        result = self.json_request(request, 16384)
        token = result['access_token']
        # Check the mailbox after every refresh, including credential rotation.
        profile = self.json_request(urllib.request.Request(
            'https://gmail.googleapis.com/gmail/v1/users/me/profile',
            headers={'Authorization': 'Bearer ' + token}), 16384)
        if profile.get('emailAddress', '').strip().lower() != self.account():
            raise self.a.Failure('oauth_mailbox_mismatch')
        self.token, self.credentials = token, credentials
        self.expires = time.monotonic() + max(1, min(3600, int(result.get('expires_in', 3600))) - 60)

    def get(self, path, **query):
        url = 'https://gmail.googleapis.com/gmail/v1/users/me/' + path
        if query:
            url += '?' + urllib.parse.urlencode(query, doseq=True)
        for attempt in range(2):
            self.refresh()
            try:
                return self.json_request(urllib.request.Request(url, headers={'Authorization': 'Bearer ' + self.token}))
            except self.a.Failure as exc:
                if exc.code != 'gmail_http_401' or attempt:
                    raise
                self.token = None

    def history_id(self, value):
        if type(value) is int:
            value = str(value)
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,40}', value):
            raise self.a.Failure('gmail_invalid_history_id')
        return value

    def message_id(self, value):
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{1,16}', value):
            raise self.a.Failure('gmail_invalid_message_id')
        return format(int(value, 16), 'x'), str(int(value, 16))

    def check_account(self, db):
        profile = self.get('profile')
        account = self.account()
        if profile.get('emailAddress', '').strip().lower() != account:
            raise self.a.Failure('oauth_mailbox_mismatch')
        if self.a.meta(db, 'account') not in (None, account):
            raise self.a.Failure('account_change_requires_separate_data_directory')
        self.history_id(profile['historyId'])
        return profile

    def verify(self, db):
        profile = self.check_account(db)
        messages = self.get('messages', labelIds='INBOX', maxResults=1).get('messages', [])
        if messages:
            api_id, _ = self.message_id(messages[0]['id'])
            self.read_mail(api_id)  # Verify body access without enqueuing or logging it.
        return profile

    def start_resync(self, db):
        # Capture history BEFORE listing. Changes during listing are replayed
        # afterwards, so the resync cannot skip arrivals while it runs.
        profile = self.check_account(db)
        state = {'history': self.history_id(profile['historyId']), 'page': ''}
        with db:
            self.a.put(db, 'gmail_resync', json.dumps(state))
            self.a.put(db, 'gmail_history_page', '')
            self.a.event(db, None, 'gmail_resync_started')
        return state

    def enqueue(self, db, messages):
        for message in messages:
            api_id, key = self.message_id(message['id'])
            if not db.execute('SELECT 1 FROM jobs WHERE id=?', (key,)).fetchone():
                db.execute('INSERT OR IGNORE INTO gmail_fetch(id) VALUES(?)', (api_id,))

    def sync_page(self, db):
        a = self.a
        state = json.loads(a.meta(db, 'gmail_resync', 'null'))
        cursor = a.meta(db, 'gmail_history_cursor')
        if not state and not cursor:
            state = self.start_resync(db)
        if state:
            try:
                result = self.get('messages', labelIds='INBOX', maxResults=50,
                    q='after:' + str(max(0, int(float(a.meta(db, 'start'))) - 1)), pageToken=state['page'])
            except a.Failure as exc:
                if exc.code == 'gmail_http_400' and state['page']:
                    self.start_resync(db)
                    return False
                raise
            next_page = result.get('nextPageToken', '')
            with db:
                self.enqueue(db, result.get('messages', []))
                if next_page:
                    a.put(db, 'gmail_resync', json.dumps({**state, 'page': next_page}))
                else:
                    a.put(db, 'gmail_history_cursor', state['history'])
                    a.put(db, 'gmail_resync', 'null')
                    a.event(db, None, 'gmail_resync_completed')
            # Always replay history after the final listing page.
            return False
        page = a.meta(db, 'gmail_history_page', '')
        try:
            result = self.get('history', startHistoryId=self.history_id(cursor), maxResults=50,
                              historyTypes=['messageAdded', 'labelAdded'], pageToken=page)
        except a.Failure as exc:
            if exc.code == 'gmail_http_404':
                self.start_resync(db)
                return False
            if exc.code == 'gmail_http_400' and page:
                with db:
                    a.put(db, 'gmail_history_page', '')
                return False
            raise
        next_page = result.get('nextPageToken', '')
        new_cursor = self.history_id(result['historyId'])
        if int(new_cursor) < int(cursor):
            raise a.Failure('gmail_history_regressed')
        with db:
            for change in result.get('history', []):
                self.enqueue(db, [entry['message'] for entry in change.get('messagesAdded', [])])
                self.enqueue(db, [entry['message'] for entry in change.get('labelsAdded', [])
                                  if 'INBOX' in entry.get('labelIds', [])])
            a.put(db, 'gmail_history_page', next_page)
            if not next_page:
                # Fetch work commits atomically with progress, before bodies are
                # retrieved. A crash cannot lose a pending Gmail message ID.
                a.put(db, 'gmail_history_cursor', new_cursor)
        return not next_page

    def parse_mail(self, result, truncated=False):
        root = result.get('payload', {})
        headers = {h['name'].lower(): h['value'] for h in root.get('headers', [])}
        plain, rich, stack = [], [], [root]
        size, parts = 0, 0
        while stack:
            part = stack.pop()
            parts += 1
            if parts > 200:
                truncated = True
                break
            mime = email.message.Message(policy=email.policy.default)
            for header in part.get('headers', []):
                if header['name'].lower() in ('content-type', 'content-disposition'):
                    mime[header['name']] = header['value']
            if part.get('filename') or mime.get_content_disposition() == 'attachment':
                continue
            kind = part.get('mimeType', '')
            if kind.startswith('multipart/'):
                stack.extend(reversed(part.get('parts', [])))
                continue
            if kind not in ('text/plain', 'text/html'):
                continue
            body = part.get('body', {})
            if body.get('attachmentId'):
                truncated = True
                continue
            encoded = body.get('data', '')
            raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
            remaining = max(0, self.a.MAX_MAIL - size)
            if len(raw) > remaining:
                truncated = True
            raw = raw[:remaining]
            size += len(raw)
            try:
                text = raw.decode(mime.get_content_charset() or 'utf-8', errors='replace')
            except LookupError:
                text = raw.decode('utf-8', errors='replace')
            (plain if kind == 'text/plain' else rich).append(text)
        body = '\n'.join(plain)
        if not body and rich:
            parser = self.a.PlainHTML()
            parser.feed('\n'.join(rich))
            body = ''.join(parser.parts)
        if not body and result.get('snippet'):
            body, truncated = html.unescape(result['snippet']), True
        return {'from': headers.get('from', '')[:300], 'subject': headers.get('subject', '')[:400],
                'body': body[:20000], 'truncated': truncated or len(body) > 20000, 'attachments_read': False}

    def read_mail(self, api_id):
        truncated = False
        try:
            result = self.get('messages/' + api_id, format='full', fields='id,labelIds,internalDate,payload,snippet')
        except self.a.Failure as exc:
            if exc.code != 'gmail_response_too_large':
                raise
            # Do not allocate a huge response or download attachments on a tiny
            # VPS. A metadata snippet is explicitly marked incomplete.
            result = self.get('messages/' + api_id, format='metadata', metadataHeaders=['From', 'Subject'],
                              fields='id,labelIds,internalDate,payload/headers,snippet')
            truncated = True
        if self.message_id(result['id'])[0] != api_id:
            raise self.a.Failure('gmail_message_id_mismatch')
        return result, self.parse_mail(result, truncated)

    def fetch_pending(self, db):
        a, deadline = self.a, time.monotonic() + 20
        for row in db.execute('SELECT id FROM gmail_fetch ORDER BY rowid LIMIT 10').fetchall():
            if a.STOP.is_set() or time.monotonic() >= deadline:
                break
            api_id, key = self.message_id(row[0])
            mail, received = None, 0
            if not db.execute('SELECT 1 FROM jobs WHERE id=?', (key,)).fetchone():
                try:
                    result, candidate = self.read_mail(api_id)
                    received = int(result['internalDate']) / 1000
                    if 'INBOX' in result.get('labelIds', []) and received >= float(a.meta(db, 'start')):
                        mail = candidate
                except a.Failure as exc:
                    if exc.code != 'gmail_http_404':
                        raise
                    with db:
                        a.event(db, key, 'gmail_message_unavailable')
            with db:
                if mail is not None:
                    inserted = db.execute('INSERT OR IGNORE INTO jobs(id,received,discovered,mail,sender,subject) VALUES(?,?,?,?,?,?)',
                        (key, received, time.time(), json.dumps(mail, ensure_ascii=False), mail['from'], mail['subject']))
                    if inserted.rowcount:
                        a.event(db, key, 'discovered', 'gmail_api')
                db.execute('DELETE FROM gmail_fetch WHERE id=?', (api_id,))

    def poll(self, db):
        if self.a.meta(db, 'account') != self.account():
            raise self.a.Failure('account_change_requires_separate_data_directory')
        complete = self.sync_page(db)
        self.fetch_pending(db)
        return complete and not db.execute('SELECT 1 FROM gmail_fetch LIMIT 1').fetchone()
