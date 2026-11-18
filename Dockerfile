# syntax=docker/dockerfile:1
# Imagen del puente. El SDK hikcloudstream se fija por commit mientras el PR con la paginación y el HEVC
# no esté fusionado (con el SDK publicado solo se ven 30 recursos y el HEVC no se decodifica).
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
ARG HIKCLOUDSTREAM_REPO=https://github.com/amirandap/hikcloudstream.git
ARG HIKCLOUDSTREAM_REF=6c7d007c5bb92be5b6780c87df927c15ee55727c
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir "hikcloudstream @ git+${HIKCLOUDSTREAM_REPO}@${HIKCLOUDSTREAM_REF}" . 

FROM python:${PYTHON_VERSION}-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --no-create-home --shell /usr/sbin/nologin bridge
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" PYTHONUNBUFFERED=1
USER bridge
EXPOSE 8558
ENTRYPOINT ["hikconnect-bridge"]

FROM build AS test
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg procps \
    && rm -rf /var/lib/apt/lists/*
COPY tests ./tests
RUN pip install --no-cache-dir -e ".[dev]"
ENTRYPOINT ["python", "-m", "pytest"]
