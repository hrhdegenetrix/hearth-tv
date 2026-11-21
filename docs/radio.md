# Radio: stations from your own library, and what Music Flamingo heard

Hearth's radio aims for the Pandora feel: pick a station and it plays on and on. The songs come from your own music library, chosen by your own listening, and nothing goes to the cloud. This doc covers how the stations are built (`hearth/radio.py`) and how Music Flamingo, NVIDIA's music-understanding model, fills in genres for songs that have no tags (`flamingo/`, `hearth/flamingo_genres.py`).

Read it before you change a weight or add a station. Every rule below was added for a reason, and the reasons are given.

## The short version

1. Jellyfin knows the songs: titles, artists, albums, years and, when the files carry them, **Genres** tags.
2. `radio.py` folds the many spellings of genre tags into **families** ("Gothic Rock" → goth, "Progessive Rock" → prog). A genre station is one family.
3. Many songs have **no genre tag**: CD rips, downloads, anything ripped without a lookup. Without help they reach only the decade stations and artist or song radio.
4. **Music Flamingo listens to every song once** (a GPU, overnight) and writes a description. The first sentence names the genre: *"This track is a dramatic Classical Opera aria, rooted in…"*.
5. `flamingo_genres.py` folds that sentence into the same families and saves a sidecar file. The radio uses it **only for songs whose own tags give no family**. The music files are never touched.
6. What you play, skip, ♥ and ban steers every station from then on.

## What a station knows

- **Tags → families.** `radio.FAMILIES` is a list of `(key, name, regex)`. A tag can match several families. Plain "Rock" counts only when nothing more specific matched, so "Hard Rock" is classic rock and not also generic rock.
- **What Music Flamingo heard**, for songs with no tag that matches a family (below).
- **Your history.**
  - The play log Hearth keeps itself (`hearth/data/listen_log.jsonl`). A song heard past its halfway point, or into its last minute, counts as a play. Moving on early counts as a skip.
  - Optionally, an old iTunes library (`TV_ITUNES_XML`). It gives years of play counts, skip counts, last-played dates and playlists. `music_history.py` matches iTunes tracks to Jellyfin's in this order: album folder + file name, then title + artist, then title + album, then a file name found only once. Duplicates in iTunes add up onto one song. The result is cached in `hearth/data/itunes_history.json`.
  - With no iTunes XML, history starts empty and fills in as the TV plays. The history stations (Old favorites, Deep cuts, Time capsule) appear once they have 60 songs.
- **What goes together:**
  - songs that share a playlist (only playlists of ≤100 songs count; bigger ones are "everything" lists that say nothing);
  - artists on the same compilation (from `music_index.py`'s album groups);
  - genre families;
  - era: `exp(-|Δyear| / 10)`.
  
  An artist with no playlists or compilations still gets usable radio from genre and era alone.
- **This session:** a skipped song's artist comes up less, a loved song's artist more.

## The stations

| Station id | Name | Pool and weight |
|---|---|---|
| `favorites` | Old favorites | songs played 3+ times, or ♥'d: weight `(plays + 8·loved)^0.7 × skip factor` |
| `deepcuts` | Deep cuts | never-played songs by artists played 15+ times; never-played songs skipped twice are left out |
| `year:2012` | Time capsule 2012 | what was last played that year |
| `decade:1970` | The 70s | by release year |
| `genre:<key>` | the family's name | every song in that family |
| `spoken` | Spoken word & comedy | readings and comedy (needs 20 songs, not 60) |
| `artist:<id>` | *Artist* radio | about a third the artist, the rest weighted by how well each song goes with them |
| `song:<id>` | Radio from "*Song*" | opens with the song; then its artist's radio, with songs that share a playlist with it weighted highest |
| `playlist:<id>` | an iTunes playlist | plays as it is (in order or shuffled), not as a station |

The genre families are Classical, Opera & art song, Sacred & choral, Goth & industrial, Punk & no wave, Experimental & noise, Prog & psych, Alternative & indie, Classic rock, Jazz & blues, Oldies & soul, Electronic, Hip-hop, Folk & acoustic, Pop, Soundtracks & musicals, Reggae & world, Holiday and Rock. *Holiday* shows only from Nov 15 to Jan 6.

A station needs **60 songs** to be listed (`MIN_STATION`). A small library shows fewer stations; that's expected.

**Weights:**
- `familiar(s) = (1 + 0.35·ln(1 + plays + 5·loved)) × skip factor`. This weight is used by the decade, genre and artist stations, so songs you know come up a bit more often without crowding out the rest.
- Skip factor = `1 / (1 + 2·skips / (plays + skips + 1))`. A song played twice and skipped four times gets 0.47.
- How well song *s* goes with artist *a* = `2.0·link + 0.8·family overlap + 0.3·era`. Here `link` is the playlist/compilation link strength normalized by the artist's strongest link, family overlap is the share of *a*'s songs in *s*'s families, and era is as above (0.3 when a year is missing). A song joins artist radio at a score ≥ 0.9, weighted by `score² × familiar`.

## Rules every station keeps

- **No copies.** The same song in the library twice has the same title, artist and album, with lengths within 3 s. It plays only as its most-played copy. Live and studio versions differ by album, so they are never merged. Nameless tracks ("(-)") are never judged.
- **No song twice in a session.** Once the whole pool has played, the station starts over, minus the last 50 songs played.
- **No artist within 4 songs** (`ARTIST_GAP`) and **no two songs in a row from one album.**
- **Nothing the TV played in the last 12 hours** (`RECENT_HOURS`). When the pool runs dry, the rules relax in this order: artist spacing first, then the 12-hour rule. Recent repeats are what listeners notice most, so that rule holds longest.
- **Not music, never on the radio:** podcasts and audiobooks (by path or tag) and anything over 30 minutes. Spoken word and comedy have their own station.

## How it plays

- `POST /api/radio/play {station}` sends the TV 25 songs. The status poll tops the queue up with 15 more when fewer than 4 are left. A restart of the server picks the station back up (`hearth/data/radio_session.json`), so the queue doesn't run dry after an update.
- **Daily lists.** A station's first 100 songs are fixed for the day: they come from a random generator seeded with `station|date`, using the same pool, weights and rules. Leave a station and come back, and it's the same list. Without this, a station reshuffles every time you open it, and that feels broken. The list lives in `hearth/data/daily.json`, so a restart doesn't reshuffle it either. After the list, the station plays on with fresh picks.
- **Skips** are seen in the status poll, whether they came from the phone or the TV: the next song started while the last one was under 30 s in, or under a third of the way. A skip halves that artist's weight for the rest of the session.
- **♥ Love** works on any song, not only on the radio. It is kept in `hearth/data/radio.json`, multiplies the artist ×1.5 for the session (capped at 3), adds weight in Old favorites, and also sets the song as a Jellyfin favorite, so other Jellyfin apps (Finamp) show the heart.
- **Not for this station** (`ban`) skips the song and keeps it off that one station for good.
- **Not for me** (`dislike`) keeps it off every station, today's lists and Shuffle all. An album or playlist you pick still plays it.

## Music Flamingo: listening to every song once

[Music Flamingo](https://huggingface.co/nvidia/music-flamingo-hf) is NVIDIA's audio-language model for music. It pairs the AF-Whisper audio encoder with Qwen2.5-7B, about 16.5 GB in bf16. It hears up to 10 minutes of a song, as 30-second windows, and answers questions about it in prose.

⛔ **License:** NVIDIA OneWay Noncommercial. Home and research use only. Never sell, host or ship anything built on it. Whoever runs it accepts the license on Hugging Face themselves.

### The pass

```
media server                                  GPU box (can be the same machine)
flamingo/prepare_audio.py  ── 16 kHz mono FLAC, first 600 s ──►  <home>/audio/<jellyfin id>.flac
                           ── manifest.json (ids, durations, genres) ──►  <home>/manifest.json
                                                                  flamingo/listen.py
                                                                    └► <home>/out/describe.jsonl
                                                                    └► <home>/out/emb_song/<id>.npy
hearth/flamingo_genres.py refresh  ◄── first sentences only (local file or ssh) ──┘
  └► hearth/data/flamingo_genres.json   (radio.py reads it; rebuilt when it changes)
```

1. **`prepare_audio.py`** runs where the music is. It asks Jellyfin for every Audio item and maps each path from the container's view to the host's. It then decodes the first 600 s of each song to 16 kHz mono FLAC (ffmpeg at nice 19, idle I/O). With `--remote user@gpu:~/flamingo` it rsyncs each batch over and deletes the local copy. It is resumable: songs already decoded are skipped.
2. **`listen.py`** runs on the GPU and asks one question per song:

   > Describe this track in full detail: its genre and style, tempo and feel, and key if you can hear it; the instruments and production; the vocals; how the song is structured and develops; the mood and how it shifts; and what the lyrics are about. Finish with one sentence on the moments or settings this music would suit.

   - **Sampling:** temperature 0.5, top-p 0.9, up to 700 new tokens. The model card's 0.7 made answers noticeably noisier.
   - **Never names.** Titles, artists and albums never go into the prompt, so the model describes what it hears, not what it might remember about a name.
   - **Spoken word is skipped** (Jellyfin genres Podcast, Audiobook, Spoken Word, Comedy), as is anything under 5 s.
   - **Loop guard.** About 2 % of descriptions fall into a loop: one short phrase over and over ("Cm6, Cm7♭5, Cm6, Cm7♭5, …") until the token cap. A stopping criterion (`descriptions.loop_start`) catches it within 8 tokens. The song is asked again with another seed, then once more with no 12-token phrase allowed twice. Whatever still loops is cut where the loop began and marked `looped: true`.
   - **Out of memory** halves the batch and shrinks later batches. A song that fails alone is recorded with `gen_tokens: -1` and not retried every night.
   - **Order and deadline.** Songs are taken in a fixed-seed random order, so a partial night covers a random slice of the library. `--deadline 06:30` stops before a batch would run past that time.
   - It also saves each song's mean-pooled encoder output (1280-d) for "sounds like" work later.
3. **`python -m hearth.flamingo_genres refresh`** reads just the first sentence of each description, from a local file or over ssh, and writes the sidecar.

**Speed and memory, measured on one RTX 5090 (32 GB):**
- about **6 s per song** on average, about 580 songs an hour. Short songs go 8 to a batch at 3–5 s each; long songs go 1–3 to a batch at 9–15 s each.
- `--batch 8 --max-windows 48` fit; larger defaults hit one out-of-memory error per night and split.
- A 10,000-song library is about 17 GPU hours, which can be spread over several nights.
- **Untested:** 24 GB cards (3090, 4090). The bf16 weights fit, but start with `--batch 2 --max-windows 20` and let the out-of-memory handling find the level.
- **Untested here:** smaller quantized copies of the model.

`listen.py` is the describe stage of the pass that ran over the original library, trimmed to just that stage. The trimmed copy has been tested with a stand-in model (`tests/test_flamingo.py`), but not yet against the real model. **Run `--limit 5` first** and read the five descriptions.

### From a description to families

`flamingo_genres.head_phrase()` takes the first sentence and drops the opening ("This track is a"). It then cuts at the first linking word (that, which, rooted, blending, with, featuring, characterized, leaning, built, driven, set, in the vein, reminiscent):

| First sentence | Head phrase | Families |
|---|---|---|
| This track is a dramatic Classical Opera aria, rooted in the bel canto tradition. | dramatic classical opera aria | opera, classical |
| This piece is a Gregorian chant sung by a male choir in a resonant space. | gregorian chant sung by a male choir in a resonant space. | sacred |
| This track is an upbeat roots reggae song with a skanking guitar. | upbeat roots reggae song | reggae |
| This song is a loud rock number built on two guitars. | loud rock number | rock (nothing finer named) |

`SOUND_FAMILIES` holds regexes tuned for Flamingo's vocabulary ("singer-songwriter", "film score", "bel canto", "motet"), most specific first. Their keys must match `radio.FAMILIES`. If the head phrase names none, the earliest family named anywhere in the first sentence wins. At most two families are kept, so a "symphonic metal ballad" doesn't land on five stations.

On the library this was built for, 712 of 717 untagged CD rips landed in a family (0 before). Classical, Opera & art song and Sacred & choral became real stations. The 3 misses from the first dry run were reggae and afrobeat, which is why Reggae & world exists.

**Tuning on a new library:** after a refresh, run `python -m hearth.flamingo_genres unmatched`. It lists the most common head phrases that landed in no family. Add patterns to `SOUND_FAMILIES` (and to `radio.FAMILIES` if it's a new family, with a name), refresh, and check `stats`.

### Keeping it fresh

New music needs its own pass. A nightly cron does it in order, on whichever machine has each piece:

```cron
# media server: decode songs added since the last run (skips what's already decoded)
30 1 * * *  cd /opt/hearth-tv && .venv/bin/python flamingo/prepare_audio.py --remote user@gpu-box:~/flamingo >> logs/flamingo.log 2>&1
# GPU box: describe them (stop by 06:30)
0 2 * * *   cd ~/flamingo && venv/bin/python listen.py --home ~/flamingo --deadline 06:30 >> listen_cron.log 2>&1
# media server: fold the new descriptions into the radio
10 7 * * *  cd /opt/hearth-tv && .venv/bin/python -m hearth.flamingo_genres refresh >> logs/flamingo.log 2>&1
```

The server notices the sidecar's new modification time and rebuilds the radio on the next request. The refresh refuses to replace the sidecar with one less than half its size (a truncated read), so a bad night can't empty the genre stations.

If the GPU is shared with something else at night (a local LLM, say), stop that first and start it again afterwards, in a wrapper script with a `trap`. The original setup did exactly that: it waited until the other server had been idle 10 minutes, then set a root timer as a safety net in case the wrapper was killed.

## Station pictures

Drop a square picture per station into `hearth/data/station_art/`, named after the station id with every run of non-alphanumerics turned into `_`. For example, `genre:classical` becomes `genre_classical.jpg`, `decade:1970` becomes `decade_1970.jpg`, and `favorites` becomes `favorites.jpg`. Use a 400 px JPEG. The phone and the TV show it on the station's card; without one, the card is text. Artist and song radio and playlists have no pictures.

## Not built yet (ideas, in order of promise)

- **"More like this" by sound.** The song embeddings in `out/emb_song/` are a "sounds like" map. ⛔ They are very anisotropic: the raw cosine between a quiet folk ballad and a loud garage-rock song came out at 0.99. **Subtract the library mean (or whiten) before any similarity search.**
- **Mood stations** ("rainy Sunday", "dinner", "focus") from each description's closing sentence, which says what the music suits. Embed those sentences, or keyword-match them, into a few moods.
- **Lyrics** (e.g. LRCLIB lookups by artist/title/album/duration) for a Now Playing lyric line.

## Files

| File | Role |
|---|---|
| `hearth/radio.py` | the engine: families, songs, pools, weights, sessions, daily lists, feedback |
| `hearth/music_index.py` | albums and artists A to Z; compilations split across artist folders made whole |
| `hearth/music_history.py` | optional iTunes XML → play counts, skips, last played, playlists |
| `hearth/flamingo_genres.py` | descriptions → families sidecar (`refresh`, `stats`, `unmatched`) |
| `flamingo/prepare_audio.py` | Jellyfin → 16 kHz FLAC (+ manifest), locally or to the GPU box |
| `flamingo/listen.py` | Music Flamingo's describe pass (+ song embeddings) |
| `flamingo/descriptions.py` | loop detection; reading and trimming descriptions without torch |
| `tests/test_radio.py`, `tests/test_flamingo.py` | the rules above as tests |
