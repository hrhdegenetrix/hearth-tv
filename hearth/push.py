"""Phone notifications from the installed remote: Web Push to the phone the
remote is installed on, so a disc that's waiting for a choice, a finished rip
with the tray open, or a failure gets noticed even with the remote closed. A
rip that silently waits on a question nobody saw can otherwise lose a day.

The keys (VAPID, generated into hearth/data/ on first use) and the phones'
subscriptions live in hearth/data/ (gitignored). The remote subscribes from
its Discs tab; static/sw.js shows the notification and opens the remote on a
tap. Web Push needs the remote on HTTPS (a browser only allows it on a secure
origin). pywebpush and py-vapid are optional: without them push is just off.
Doc: docs/server.md § Push.
"""
import asyncio
import base64
import json
import os
import time
from pathlib import Path

from hearth.errors import if_errors

try:
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01
    from pywebpush import WebPushException, webpush
    AVAILABLE = True
except ImportError:          # push is optional: pip install pywebpush py-vapid
    AVAILABLE = False


def _claim_sub():
    """The contact push services may use to reach the server's owner (VAPID `sub`).
    TV_CONTACT in .env (an email or a URL); a neutral placeholder otherwise."""
    contact = (os.getenv('TV_CONTACT') or '').strip()
    if not contact:
        return 'mailto:hearth@localhost'
    if contact.startswith(('mailto:', 'https://')):
        return contact
    return ('mailto:' + contact) if '@' in contact else 'https://' + contact


class Push:
    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.key_path = self.dir / 'vapid_private.pem'
        self.subs_path = self.dir / 'push_subs.json'
        self.vapid = None

    def _keys(self):
        if not AVAILABLE:
            raise RuntimeError('Push needs pywebpush and py-vapid (pip install pywebpush py-vapid)')
        if self.vapid is None:
            if self.key_path.exists():
                self.vapid = Vapid01.from_file(str(self.key_path))
            else:
                self.dir.mkdir(parents=True, exist_ok=True)
                self.vapid = Vapid01()
                self.vapid.generate_keys()
                self.vapid.save_key(str(self.key_path))
                os.chmod(self.key_path, 0o600)
        return self.vapid

    def public_key(self):
        """The key the page subscribes with (uncompressed P-256 point, base64url)."""
        raw = self._keys().public_key.public_bytes(serialization.Encoding.X962,
                                                   serialization.PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')

    def _subs(self):
        try:
            return json.loads(self.subs_path.read_text())
        except (OSError, ValueError):
            return []

    def _save(self, subs):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.subs_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(subs))
        os.replace(tmp, self.subs_path)

    def subscribe(self, sub, device=''):
        endpoint = str(sub.get('endpoint') or '')
        keys = sub.get('keys') or {}
        if not endpoint.startswith('https://') or not keys.get('p256dh') or not keys.get('auth'):
            raise ValueError('Not a push subscription')
        subs = [s for s in self._subs() if s['endpoint'] != endpoint]
        subs.append({'endpoint': endpoint, 'keys': {'p256dh': keys['p256dh'], 'auth': keys['auth']},
                     'device': str(device)[:80], 'added': round(time.time())})
        self._save(subs)
        return len(subs)

    def unsubscribe(self, endpoint):
        subs = self._subs()
        kept = [s for s in subs if s['endpoint'] != endpoint]
        self._save(kept)
        return len(subs) != len(kept)

    def has(self, endpoint):
        return any(s['endpoint'] == endpoint for s in self._subs())

    @if_errors
    async def send(self, title, body, url='/#discs', tag='discs'):
        """One notification to every subscribed phone; gone phones are dropped."""
        subs = self._subs()
        if not subs or not AVAILABLE:
            return 0
        data = json.dumps({'title': title, 'body': body, 'url': url, 'tag': tag})
        vapid = self._keys()
        gone, sent = [], 0
        for s in subs:
            try:
                await asyncio.to_thread(webpush, s, data, vapid_private_key=vapid,
                                        vapid_claims={'sub': _claim_sub()}, ttl=24 * 3600, timeout=15)
                sent += 1
            except WebPushException as e:
                status = getattr(e.response, 'status_code', None)
                if status in (404, 410):
                    gone.append(s['endpoint'])
                else:
                    print('[PUSH] %s failed: %s' % (s.get('device') or 'a phone', str(e)[:200]))
        if gone:
            self._save([s for s in self._subs() if s['endpoint'] not in gone])
        return sent
