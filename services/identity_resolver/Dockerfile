# Identity Resolver Dockerfile — CPU only (no GPU needed)

FROM python:3.11-slim AS base

WORKDIR /app

COPY requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt

COPY . .

RUN useradd -m -u 1001 resolver && chown -R resolver /app
USER resolver

EXPOSE 8003

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "from qdrant_client import QdrantClient; QdrantClient('${QDRANT_URL}').get_collections()" || exit 1

CMD ["python", "main.py"]
