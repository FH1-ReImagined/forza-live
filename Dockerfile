# Forza Live server: docker build -t forza-live . &&
#   docker run --network host -v forza-live-data:/data forza-live --http-port 21201
# (or -p 21200:21200/udp -p 21201:21201; --network host keeps players' public ports intact)
# Its one dependency, SQLAlchemy (the database, forza_live/core/storage.py), pinned by version and
# hash in requirements.txt.
FROM python:3.12-slim
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --root-user-action=ignore --require-hashes -r requirements.txt \
    && rm requirements.txt
COPY forza_live_server.py ./
COPY forza_live ./forza_live
COPY tools/event_admin.py ./tools/
COPY LICENSE NOTICE.md ./
EXPOSE 21200/udp 21201/tcp
VOLUME /data
ENTRYPOINT ["python3", "forza_live_server.py", "--port", "21200", "--http-port", "21201", "--data-dir", "/data"]
