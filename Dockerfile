# syntax=docker/dockerfile:1
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FRAME_BIND_HOST=0.0.0.0 \
    FRAME_ALLOW_CONTAINER_CLIENTS=1 \
    LOCALAPPDATA=/data

WORKDIR /app

COPY requirements.txt ./
RUN --mount=type=secret,id=pip_index_url,env=PIP_INDEX_URL \
    pip install --no-cache-dir --only-binary=:all: -r requirements.txt

RUN groupadd --gid 10001 frame \
    && useradd --uid 10001 --gid frame --home-dir /data --no-create-home --shell /usr/sbin/nologin frame \
    && mkdir -p /data/FramePhotoManager \
    && chown -R frame:frame /data

COPY activity.py app.py images.py thumbnails.py tv.py ./
COPY templates/index.html ./templates/index.html

USER frame:frame
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os; from urllib.request import urlopen; urlopen('http://127.0.0.1:%s/' % os.environ.get('FRAME_PORT', '8765'), timeout=3).close()"]

CMD ["python", "app.py"]
