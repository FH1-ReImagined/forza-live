# The mods and texture packs hub's scanning worker and its ClamAV signature updater (README.md in
# this folder): one image, two services in compose.yaml.
#
#   docker build -t forza-hub-scanner hub/        (from forza-live's root)
#
# The base is the accounts service's, pinned by digest; ClamAV comes from its Debian packages.
# No signatures are baked in: the updater service downloads them into a volume that the scanner
# mounts read-only. The scanner runs with no network, a read-only root and no privileges.
FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
RUN apt-get update \
    && apt-get install -y --no-install-recommends clamav clamav-freshclam \
    && rm -rf /var/lib/apt/lists/* /var/lib/clamav/* \
    && groupadd --gid 10002 hub && useradd --uid 10002 --gid 10002 --no-create-home --home-dir /nonexistent hub
WORKDIR /app
COPY hub_check.py hub_worker.py freshclam.conf ./
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10002:10002
ENTRYPOINT ["python3", "-I", "/app/hub_worker.py"]
