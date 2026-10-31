# Forza Live community photos: the automatic screening worker (README.md in this folder).
#
#   docker build -t forza-screening .        (in this folder: screening/ of forza-live)
#
# The models are downloaded and built here, each file pinned by its repository revision and its
# SHA-256 (models.lock.json), and the Python packages by version and hash (requirements*.txt).
# The running container downloads nothing: it needs the server's HTTP port and nothing else.

# Stage 1: fetch the weights, export the NSFW classifier to ONNX, embed the zero-shot prompts.
# PyTorch, timm and the SigLIP2 text model stay in this stage.
FROM python:3.12-slim AS models
WORKDIR /build
# onnxruntime's Linux wheels upload usage telemetry unless this is set (screen_models.py)
ENV ORT_DISABLE_TELEMETRY=1
COPY requirements-build.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-build.txt
COPY build_models.py models.lock.json prompts.json ./
RUN python3 build_models.py --cache /cache --out /models all && rm -rf /cache

# Stage 2: the worker, onnxruntime on the CPU.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt && rm requirements.txt
COPY --from=models /models /models
COPY screen_client.py screen_image.py screen_models.py screen_policy.py screen_text.py \
     screen_worker.py synth.py screening.toml NOTICE.md ./
COPY wordlists ./wordlists
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MALLOC_ARENA_MAX=2 OMP_NUM_THREADS=1 \
    ORT_DISABLE_TELEMETRY=1
USER 65534:65534
ENTRYPOINT ["python3", "-I", "/app/screen_worker.py", "--config", "/app/screening.toml"]
