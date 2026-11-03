# The website accounts service (README.md): its own image, apart from the game server's.
#   docker build -t forza-accounts accounts/
# The base is pinned by digest (python:3.12-slim, multi-arch index); the dependencies by version
# and hash (requirements.txt, pip --require-hashes). It runs as a user without privileges.
FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=fh_accounts.settings FORZA_ACCOUNTS_DATA=/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --root-user-action=ignore --require-hashes -r requirements.txt
COPY manage.py docker-entrypoint.sh ./
COPY fh_accounts ./fh_accounts
COPY members ./members
# The admin's static files (no secret is needed to collect them; this key exists only here).
RUN DJANGO_SECRET_KEY=collectstatic-only python manage.py collectstatic --noinput -v 0 \
    && useradd --uid 10001 --no-create-home --home-dir /nonexistent accounts \
    && mkdir -p /data && chown accounts:accounts /data
USER accounts
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/api/health', timeout=4)"]
ENTRYPOINT ["./docker-entrypoint.sh"]
