# Video converter for community videos

The game's movie upload sends a replay clip to the server: an ASF (WMV) file with VC-1 video and
WMA 2 audio, 640x360 from the TU4 game, through the same large-file upload as photo mode's Big
Shot. Browsers do not play WMV. This worker turns each clip into an MP4 that every browser plays,
with the frames the [screening worker](../screening/README.md) scores (the poster is one of them), so the website's
Videos tab can show it. It is off until you run it: without it the server keeps clips waiting for
a day, then drops them, and publishes no videos.

```
game --LargeFileUpload + UploadContent--> forza-live (videos.py: the ASF header, caps)
                                              | writes, as root
                                              v
                                   data/videos/incoming/<id>.wmv, <id>.json
                                              | read-only
                                              v
                                   video-converter (no network, read-only root, ffmpeg)
                                              | writes, as 10003
                                              v
                                   data/videos/converted/<id>/  (one rename)
                                              |
      forza-live checks it as untrusted: no links, plain files, MP4 boxes, JPEGs cleaned, budget
                                              v
                             held --screening (frames) or the operator--> live
                                              v
                    /community/videos.json, /community/video/<id>.mp4 and .jpg
```

## What a clip goes through

1. **The server** (`forza_live/content/videos.py`) tells a clip from a picture by its first
   bytes (the ASF Header Object; the game names the file `Forza<digits>`, no extension) and reads
   its header with the standard library. It refuses a clip that is not signed by the player's key,
   from a banned player or one without a gamertag, with no or two video streams, smaller than
   96x64 or larger than 1920x1088, shorter than 1 s or longer than 120 s, larger than 32 MiB, and
   over the caps (5 a day and 50 in all per player, 20 an hour and 20 waiting on the server, and
   the videos' own disk budget, `FORZA_VIDEOS_CAP_MB`, 2048 by default, apart from the quota of
   the other uploads). Then it copies the clip to `incoming/<id>.wmv` and writes the job
   `<id>.json` last, with a random seed that only the converter reads. The uploaded clip is never
   kept as one of the player's files, whatever its size.
2. **The worker** (`video_worker.py`) takes each whole job, oldest first, one at a time:
   - `ffprobe` with the ASF demuxer forced (`-f asf`: no format probing, so no playlist, concat
     or image sequence), the file protocol only and the Windows Media decoders only (VC-1,
     WMV 1-3, WMA). One video stream within the sizes above with square pixels, at most one
     audio stream, 1 to 121 s.
   - `ffmpeg` to H.264 (Main, yuv420p, 30 fps, CRF 23, at most 2.5 Mbit/s, at most 1280 px
     wide) and AAC (128 kbit/s stereo), cut at 120 s, at most 32 MiB, `faststart` (the player
     can start before the whole file has arrived), with no metadata: the game's title, the
     driver's name and the car's tags are dropped.
   - The converted video read back: 2 frames a second as tiny greyscale pictures. A clip whose
     every frame is one flat colour is refused as blank (the game recorded no picture).
   - The frames for screening: one in each 2 s of the video (8 at least, 60 at most), each at an
     instant inside its 2 s that the job's seed chooses, at the video's own size. The server never
     publishes the seed, so nobody can build a clip around the instants that are looked at.
   - The poster is one of those frames (the one with the most detail): no picture is published
     that the screening has not seen.
   - Each ffmpeg run has its own limits: 1 GiB of address space, 300 s of CPU and of wall clock,
     the output's size, 64 open files, no core dumps, and a clean environment with
     `MALLOC_ARENA_MAX=2`. It uses one thread for decoding, filtering and encoding
     (`-threads 1`, `-filter_threads 1`), so its memory does not grow with the machine's CPU count
     (without that, a host with many cores refuses every clip at the address space limit). Its
     whole process group is killed when it ends.
   - A clip that stopped the worker twice (killed for its memory, say) is refused the third time,
     so no clip can stop the queue.
   - The result goes to `converted/.<id>.part/` and then, in one rename, to `converted/<id>/`:
     `result.json` (`ok`, the length, the size, the number of frames and which one is the poster,
     or the reason), `video.mp4`, `frame-<n>.jpg`. A refused clip leaves only its result. A job
     that is gone before its turn is skipped; half-written results and old attempt notes are
     cleared before each pass.
3. **The server** looks for results every 5 s and on every operator request. It reads the result
   folder as untrusted: the folder and each file opened without following links and without
   blocking, plain files of bounded sizes only (checked on the open file), at most 68 names. It
   requires exactly the frames the video's length asks for, checks the MP4's top-level boxes
   (`ftyp` with an ISO brand, one `moov` with an H.264 track, `mdat`, padding only), rewrites
   the frames with its own JPEG cleaner (no metadata survives) and takes the poster from them,
   keeps them under `data/videos/public/` and `frames/`, and deletes the clip, the job and the
   result, whatever happened (a result is never looked at twice).
   The video is **held**, always: it is never published before the screening worker or the
   operator releases it.

The sound is converted but not screened. The game records the race's sound only.

## The container

`compose.yaml` in the repository's root has two services under the profile `videos`:

| Service | Network | Root | Mounts |
|---------|---------|------|--------|
| `videos-init` | none | read-only | gives the converter its results folder once (owner 10003), then exits |
| `video-converter` | **none** | read-only, no capabilities, no new privileges, 64 processes, 1.5 GB, 1 CPU, `/tmp` 16 MB | `data/videos/incoming` read-only, `data/videos/converted` |

The image is `python:3.12-slim` (pinned by digest, the same base as the hub's scanner) with
Debian's `ffmpeg` package (libx264 and ffmpeg's own AAC encoder). Debian's package brings ffmpeg's
libraries and their codecs: expect the image to be a few hundred MB larger than the base. The
server's own image does not change: it never runs ffmpeg.

**Keeping ffmpeg up to date.** The base is pinned, but ffmpeg is installed from Debian's mirror
when the image is built, and Docker keeps that layer until something changes. To take Debian's
latest ffmpeg (its security fixes), build again with a new value of `REFRESH`, say the date:

    docker compose build --build-arg REFRESH=$(date +%F) video-converter
    docker compose up -d video-converter

or build with `--no-cache`. Do it when Debian announces an ffmpeg security update, and at least
every few months.

## Running it

With the repository's `compose.yaml`, turn the profile on in `.env` and start it:

    COMPOSE_PROFILES=videos          # in .env
    docker compose up -d --build

Or by hand, beside a server whose data folder is `/path/to/data`. The server must be able to
read and delete what the converter writes: with the server in its Docker image (root), that is
always so. A server that runs as another user (outside Docker, say) needs the converter to run
as that same user: replace `10003:10003` below with that user's `uid:gid`, in the `chown` and in
`--user`.

    docker build -t forza-video-converter video/
    docker run --rm --network none -v /path/to/data/videos:/v --entrypoint /bin/sh --user 0 \
        forza-video-converter -c 'mkdir -p /v/incoming /v/converted && chown 10003:10003 /v/converted'
    docker run -d --name forza-video-converter --restart unless-stopped --network none --user 10003:10003 \
        --read-only --tmpfs /tmp:size=16m --cap-drop ALL --security-opt no-new-privileges \
        --memory 1536m --memory-swap 1536m --cpus 1 --pids-limit 64 \
        -v /path/to/data/videos/incoming:/incoming:ro -v /path/to/data/videos/converted:/converted \
        forza-video-converter --incoming /incoming --converted /converted

The log is one JSON line per clip: its id, `ok`, the reason when refused, the length and the
time it took. There is no XUID or gamertag in it. The server logs each clip too (`videos:` lines).

Settings (environment): `FORZA_VIDEO_MAX_SECONDS` (120), `FORZA_VIDEO_MAX_WIDTH` (1280),
`FORZA_VIDEO_TIMEOUT` (300 s per ffmpeg run), `FORZA_VIDEO_MEMORY_MB` (1024 per ffmpeg run).
They can only lower what the server asks for.

Cost: a 6 s 640x360 test clip took 1.5 s on one core of a desktop CPU, frames included, and a
20 s 1920x1080 one (scaled to 1280x720, 11 frames) 5.2 s. A conversion needs about 400 MB of
address space at 640x360 and 500 MB at 1080p, the same with one core or eight.

## Testing

    python3 -m unittest discover -s video -p 'test_*.py' -v

The conversions are tested on clips ffmpeg makes from a test pattern and a tone (no game footage);
they are skipped when ffmpeg is not installed. The server's side is in
`tests/content/test_videos.py`, which also runs this worker from the game's upload to the feed
when ffmpeg is there.
