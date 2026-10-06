# The media service: image embeddings (CLIP via fastembed, ONNX on CPU), captioned slides (Pillow) and MP4 slideshows
# (FFmpeg) for the video toolset. Only the API can reach it (no published ports). It never sees the vault, fetches
# only allow-listed hosts (MEDIA_FETCH_HOSTS) and reads and writes only inside run folders of the shared workspace.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/media
COPY requirements.txt .
RUN pip install -r requirements.txt

# Models are downloaded here, at build time: a run never waits on a download and needs no Hugging Face access.
ENV MEDIA_IMAGE_MODEL=Qdrant/clip-ViT-B-32-vision MEDIA_TEXT_MODEL=Qdrant/clip-ViT-B-32-text MEDIA_MODELS_DIR=/models
RUN python -c "import os; from fastembed import ImageEmbedding, TextEmbedding; \
ImageEmbedding(os.environ['MEDIA_IMAGE_MODEL'], cache_dir='/models'); \
TextEmbedding(os.environ['MEDIA_TEXT_MODEL'], cache_dir='/models')" \
    && chmod -R a+rX /models
ENV HF_HUB_OFFLINE=1

# Same uid as the sandbox's `agent` user (the node image's `node` is 1000, so `agent` is 1001): both write run folders.
RUN useradd -m -u 1001 -s /usr/sbin/nologin media && mkdir -p /workspace && chown media:media /workspace
COPY server.py /opt/media/server.py

USER media
WORKDIR /workspace
ENV WORKSPACE_ROOT=/workspace
EXPOSE 7000
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7000/health')"
CMD ["uvicorn", "--app-dir", "/opt/media", "server:app", "--host", "0.0.0.0", "--port", "7000"]
