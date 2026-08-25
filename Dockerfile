FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir .
ENV TAK_ILI_INACHE_DATA_DIR=/data
VOLUME ["/data"]
CMD ["tak-ili-inache-polling"]
