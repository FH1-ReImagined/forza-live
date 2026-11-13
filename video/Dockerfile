# Forza Live community videos: the video converter (README.md in this folder).
#
#   docker build -t forza-video-converter video/        (from forza-live's root)
#
# The base is the hub's and the accounts service's, pinned by digest; ffmpeg comes from its Debian
# packages (with libx264 and its own AAC encoder). The converter runs with no network, a read-only
# root and no privileges (compose.yaml, service video-converter): it reads the game server's clips
# read-only and writes the converted videos beside them.
FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10003 converter \
    && useradd --uid 10003 --gid 10003 --no-create-home --home-dir /nonexistent converter
WORKDIR /app
COPY video_worker.py ./
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MALLOC_ARENA_MAX=2
USER 10003:10003
ENTRYPOINT ["python3", "-I", "/app/video_worker.py"]
