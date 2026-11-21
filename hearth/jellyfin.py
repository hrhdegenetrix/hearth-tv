"""
Jellyfin for Hearth — the library behind the remote's Library tab and the
TV's Jellyfin screen.

Jellyfin (usually a Docker container on the server, :8096) owns the catalog:
posters, seasons/episodes, watched state and resume points. Media lives in
one share (TV_MEDIA_ROOT on the host, TV_JELLYFIN_MEDIA_ROOT inside the
container). Playback still goes through the Pi's mpv (smooth hardware
decode): the Pi streams the original file from Jellyfin (direct, no
transcode) and the server reports progress back so Jellyfin's resume points
stay true.

Two addresses: JELLYFIN_URL is how this server reaches Jellyfin, and
JELLYFIN_PI_URL (default: the same) is how the Pi does, because the stream
URLs the Pi's mpv opens are built from it. With Jellyfin on the server,
JELLYFIN_URL can be http://127.0.0.1:8096 but JELLYFIN_PI_URL must be the
server's LAN (or Tailscale) address.

Doc: docs/server.md § Jellyfin.
"""

import asyncio
import os

import httpx

TICKS = 10_000_000   # Jellyfin positions are in 100-ns ticks


class Jellyfin:
    def __init__(self, url=None, key=None, pi_url=None, user=None):
        self.url = (url or os.getenv('JELLYFIN_URL', 'http://127.0.0.1:8096')).rstrip('/')
        self.pi_url = (pi_url or os.getenv('JELLYFIN_PI_URL') or self.url).rstrip('/')
        self.key = key or os.getenv('JELLYFIN_API_KEY', '')
        self.user = user or os.getenv('JELLYFIN_USER', '')
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=4.0))
        self._user_id = None
        self._libs = None
        self._user_token = None

    @property
    def headers(self):
        return {'Authorization': f'MediaBrowser Client="Hearth", Device="hearth-server", '
                                 f'DeviceId="hearth-service", Version="1.0", Token="{self.key}"'}

    async def user_token(self):
        """The household user's own Jellyfin session (JELLYFIN_USER /
        JELLYFIN_PASSWORD). Playback reports have to come from a user: one
        signed with the API key belongs to nobody, and Jellyfin silently drops
        it — no resume point, no watched mark. Its own DeviceId, so signing in
        again replaces the old token instead of piling up devices."""
        if not self._user_token:
            if not self.user:
                raise RuntimeError('JELLYFIN_USER is not set in .env')
            pw = os.getenv('JELLYFIN_PASSWORD', '')
            auth = ('MediaBrowser Client="Hearth", Device="hearth-server", '
                    'DeviceId="hearth-playback", Version="1.0"')
            r = await self.client.post(self.url + '/Users/AuthenticateByName',
                                       json={'Username': self.user, 'Pw': pw},
                                       headers={'Authorization': auth})
            r.raise_for_status()
            self._user_token = r.json()['AccessToken']
        return self._user_token

    def _user_headers(self):
        return {'Authorization': 'MediaBrowser Client="Hearth", Device="hearth-server", '
                                 f'DeviceId="hearth-playback", Version="1.0", Token="{self._user_token}"'}

    async def get(self, path, **params):
        r = await self.client.get(self.url + path, params=params, headers=self.headers)
        r.raise_for_status()
        return r.json()

    async def post(self, path, json=None, **params):
        r = await self.client.post(self.url + path, params=params, json=json, headers=self.headers)
        r.raise_for_status()
        return r

    async def user_id(self):
        if not self._user_id:
            users = await self.get('/Users')
            user = next((u for u in users if self.user and u.get('Name', '').lower() == self.user.lower()), users[0])
            self._user_id = user['Id']
        return self._user_id

    async def libraries(self):
        if self._libs is None:
            views = await self.get(f'/Users/{await self.user_id()}/Views')
            self._libs = {v['Name']: v['Id'] for v in views.get('Items', [])}
        return self._libs

    # ------------------------------------------------------------ catalog
    @staticmethod
    def _card(item):
        data = item.get('UserData') or {}
        resume = (data.get('PlaybackPositionTicks') or 0) / TICKS
        runtime = (item.get('RunTimeTicks') or 0) / TICKS
        return {
            'id': item['Id'],
            'name': item.get('Name', ''),
            'type': item.get('Type'),
            'year': item.get('ProductionYear'),
            'runtime': round(runtime) or None,
            'resume': round(resume) if resume > 30 else None,
            'played': bool(data.get('Played')),
            'unplayed': data.get('UnplayedItemCount'),
            'season': item.get('ParentIndexNumber'),
            'episode': item.get('IndexNumber'),
            'series': item.get('SeriesName'),
            'series_id': item.get('SeriesId'),
            'has_image': bool((item.get('ImageTags') or {}).get('Primary')),
        }

    async def _items(self, **params):
        params.setdefault('Fields', 'ProductionYear,RunTimeTicks')
        data = await self.get(f'/Users/{await self.user_id()}/Items', **params)
        return [self._card(i) for i in data.get('Items', [])]

    async def movies(self):
        lib = (await self.libraries()).get('Movies')
        if not lib:
            return []
        return await self._items(ParentId=lib, IncludeItemTypes='Movie', Recursive='true',
                                 SortBy='SortName')

    async def shows(self):
        lib = (await self.libraries()).get('Shows')
        if not lib:
            return []
        return await self._items(ParentId=lib, IncludeItemTypes='Series', Recursive='true',
                                 SortBy='SortName')

    async def episodes(self, series_id):
        data = await self.get(f'/Shows/{series_id}/Episodes', UserId=await self.user_id(),
                              Fields='RunTimeTicks')
        return [self._card(i) for i in data.get('Items', [])]

    async def continue_watching(self):
        data = await self.get(f'/Users/{await self.user_id()}/Items/Resume', Limit=12,
                              MediaTypes='Video', Fields='RunTimeTicks')
        return [self._card(i) for i in data.get('Items', [])]

    async def item(self, item_id):
        return self._card(await self.get(f'/Users/{await self.user_id()}/Items/{item_id}'))

    # ------------------------------------------------------------ music
    # Jellyfin's Music library. Albums and tracks come from the files' own tags.
    @staticmethod
    def _album(item):
        return {
            'id': item['Id'],
            'type': 'MusicAlbum',
            'name': item.get('Name', ''),
            'artist': item.get('AlbumArtist') or ', '.join(item.get('Artists') or []),
            'year': item.get('ProductionYear'),
            'has_image': bool((item.get('ImageTags') or {}).get('Primary')),
        }

    @staticmethod
    def _track(item):
        return {
            'id': item['Id'],
            'title': item.get('Name', ''),
            'artist': ', '.join(item.get('Artists') or []) or item.get('AlbumArtist') or '',
            'album': item.get('Album') or '',
            'album_id': item.get('AlbumId'),
            'number': item.get('IndexNumber'),
            'disc': item.get('ParentIndexNumber'),
            'duration': round((item.get('RunTimeTicks') or 0) / TICKS) or None,
            # the album's cover, else the track's own
            'image': item.get('AlbumId') if item.get('AlbumPrimaryImageTag') else (
                item['Id'] if (item.get('ImageTags') or {}).get('Primary') else None),
        }

    async def _music(self, kind, **params):
        lib = (await self.libraries()).get('Music')
        if not lib:
            return []
        params.setdefault('Recursive', 'true')
        data = await self.get(f'/Users/{await self.user_id()}/Items', ParentId=lib,
                              IncludeItemTypes=kind, Fields='ProductionYear', **params)
        shape = self._album if kind == 'MusicAlbum' else self._track
        return [shape(i) for i in data.get('Items', [])]

    async def recent_albums(self, limit=30):
        return await self._music('MusicAlbum', SortBy='DateCreated', SortOrder='Descending', Limit=limit)

    async def artists(self):
        lib = (await self.libraries()).get('Music')
        if not lib:
            return []
        data = await self.get('/Artists/AlbumArtists', ParentId=lib, UserId=await self.user_id(),
                              SortBy='SortName')
        return [{'id': a['Id'], 'name': a.get('Name', '')} for a in data.get('Items', [])]

    async def artist_albums(self, artist_id):
        return await self._music('MusicAlbum', AlbumArtistIds=artist_id,
                                 SortBy='ProductionYear,SortName')

    async def album_tracks(self, album_id):
        data = await self.get(f'/Users/{await self.user_id()}/Items', ParentId=album_id,
                              IncludeItemTypes='Audio', Recursive='true',
                              SortBy='ParentIndexNumber,IndexNumber,SortName')
        return [self._track(i) for i in data.get('Items', [])]

    async def track(self, track_id):
        return self._track(await self.get(f'/Users/{await self.user_id()}/Items/{track_id}'))

    async def artist_tracks(self, artist_id):
        return await self._music('Audio', AlbumArtistIds=artist_id,
                                 SortBy='ProductionYear,Album,ParentIndexNumber,IndexNumber')

    async def shuffled_tracks(self, limit=300):
        return await self._music('Audio', SortBy='Random', Limit=limit)

    async def music_items(self):
        """Every album, song and album artist in the Music library, raw, for the
        TV's A-to-Z browsing (music_index.py). A library of ~7,400 songs comes back in ~1 s."""
        lib = (await self.libraries()).get('Music')
        if not lib:
            return [], [], []
        uid = await self.user_id()
        items = f'/Users/{uid}/Items'
        albums, tracks, artists = await asyncio.gather(
            self.get(items, ParentId=lib, IncludeItemTypes='MusicAlbum', Recursive='true',
                     Fields='DateCreated,ProductionYear', EnableUserData='false'),
            # genres and paths for the radio (radio.py; music_history.py matches iTunes by path)
            self.get(items, ParentId=lib, IncludeItemTypes='Audio', Recursive='true',
                     Fields='DateCreated,ProductionYear,Genres,Path', EnableUserData='false'),
            self.get('/Artists/AlbumArtists', ParentId=lib, UserId=uid, EnableUserData='false'))
        return albums.get('Items', []), tracks.get('Items', []), artists.get('Items', [])

    async def search_music(self, term):
        lib = (await self.libraries()).get('Music')
        if not lib:
            return {'artists': [], 'albums': [], 'tracks': []}
        uid = await self.user_id()
        data = await self.get(f'/Users/{uid}/Items', ParentId=lib, Recursive='true', SearchTerm=term,
                              IncludeItemTypes='MusicArtist,MusicAlbum,Audio', Limit=80,
                              Fields='ProductionYear')
        out = {'artists': [], 'albums': [], 'tracks': []}
        for i in data.get('Items', []):
            if i.get('Type') == 'MusicArtist':
                out['artists'].append({'id': i['Id'], 'name': i.get('Name', '')})
            elif i.get('Type') == 'MusicAlbum':
                out['albums'].append(self._album(i))
            elif i.get('Type') == 'Audio':
                out['tracks'].append(self._track(i))
        return out

    async def favorite(self, item_id, on=True):
        """A heart on a song (the radio's ♥), as the household user, so Jellyfin's apps
        (Finamp, the web UI) show it too. Jellyfin
        10.9 moved the route; the old one is the fallback."""
        await self.user_token()
        uid = await self.user_id()
        for path, params in ((f'/UserFavoriteItems/{item_id}', {'userId': uid}),
                             (f'/Users/{uid}/FavoriteItems/{item_id}', {})):
            call = self.client.post if on else self.client.delete
            r = await call(self.url + path, params=params, headers=self._user_headers())
            if r.status_code == 401:
                self._user_token = None
                await self.user_token()
                r = await call(self.url + path, params=params, headers=self._user_headers())
            if r.status_code != 404:
                r.raise_for_status()
                return True
        return False

    # ------------------------------------------------------------ the household's own playlists
    # Jellyfin's playlists, so Jellyfin's apps (Finamp…) show them too. They
    # belong to the household user, so every call is signed with that user's
    # token. The server keeps the ids of the ones made here (radio.json 'mine');
    # Jellyfin also lists any .m3u files it found in the Music folder, which are
    # left alone.
    async def _as_user(self, method, path, **kw):
        await self.user_token()
        call = getattr(self.client, method)
        r = await call(self.url + path, headers=self._user_headers(), **kw)
        if r.status_code == 401:
            self._user_token = None
            await self.user_token()
            r = await call(self.url + path, headers=self._user_headers(), **kw)
        r.raise_for_status()
        return r

    async def create_playlist(self, name, ids):
        r = await self._as_user('post', '/Playlists', json={'Name': name, 'Ids': list(ids),
                                                             'UserId': await self.user_id(), 'MediaType': 'Audio'})
        return r.json()['Id']

    async def playlist_entries(self, playlist_id):
        """[(song id, playlist entry id)] in the playlist's order."""
        r = await self._as_user('get', f'/Playlists/{playlist_id}/Items', params={'userId': await self.user_id()})
        return [(i['Id'], i.get('PlaylistItemId')) for i in r.json().get('Items', [])]

    async def add_to_playlist(self, playlist_id, ids):
        await self._as_user('post', f'/Playlists/{playlist_id}/Items',
                            params={'ids': ','.join(ids), 'userId': await self.user_id()})

    async def remove_from_playlist(self, playlist_id, entry_ids):
        await self._as_user('delete', f'/Playlists/{playlist_id}/Items', params={'entryIds': ','.join(entry_ids)})

    async def rename_playlist(self, playlist_id, name):
        await self._as_user('post', f'/Playlists/{playlist_id}', json={'Name': name})

    async def delete_playlist(self, playlist_id):
        await self._as_user('delete', f'/Items/{playlist_id}')

    async def playlist_info(self, ids):
        """{id: {name, count}} for these playlists (gone ones are left out)."""
        if not ids:
            return {}
        data = await self.get(f'/Users/{await self.user_id()}/Items', Ids=','.join(ids), Fields='ChildCount')
        return {i['Id']: {'name': i.get('Name', ''), 'count': i.get('ChildCount') or 0} for i in data.get('Items', [])}

    def audio_url(self, item_id):
        """The original audio file (no transcode) for the Pi's mpv, like stream_url."""
        return f'{self.pi_url}/Audio/{item_id}/stream?static=true&api_key={self.key}'

    async def image(self, item_id, width=360):
        r = await self.client.get(f'{self.url}/Items/{item_id}/Images/Primary',
                                  params={'maxWidth': width, 'quality': 85}, headers=self.headers)
        if r.status_code != 200:
            return None, None
        return r.content, r.headers.get('content-type', 'image/jpeg')

    def stream_url(self, item_id):
        """The original file, streamed with Range support (no transcode) — what
        the Pi's mpv plays. The key rides the URL because mpv can't send headers
        here; keep that path on the home network (or Tailscale), and the server
        strips it from everything it sends to the phone (server._no_key)."""
        return f'{self.pi_url}/Videos/{item_id}/stream?static=true&api_key={self.key}'

    # ------------------------------------------------------------ playback reporting
    async def report(self, kind, item_id, position, paused=False):
        """kind: 'start' | 'progress' | 'stop' — keeps Jellyfin's resume point and
        watched state in step with what the TV actually played."""
        path = {'start': '/Sessions/Playing', 'progress': '/Sessions/Playing/Progress',
                'stop': '/Sessions/Playing/Stopped'}[kind]
        body = {'ItemId': item_id, 'PositionTicks': int((position or 0) * TICKS),
                'IsPaused': bool(paused), 'PlayMethod': 'DirectPlay', 'CanSeek': True}
        await self.user_token()
        r = await self.client.post(self.url + path, json=body, headers=self._user_headers())
        if r.status_code == 401:          # token revoked (e.g. signed out in Jellyfin's dashboard)
            self._user_token = None
            await self.user_token()
            r = await self.client.post(self.url + path, json=body, headers=self._user_headers())
        r.raise_for_status()

    async def refresh(self):
        await self.post('/Library/Refresh')
