# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

WORKDIR /build
RUN python -m pip install --no-cache-dir build
COPY pyproject.toml README.md LICENSE ./
COPY app ./app
RUN python -m build --wheel

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BUGLENS_HOST=0.0.0.0 \
    BUGLENS_PORT=8000 \
    BUGLENS_STATE_DIR=/var/lib/buglens/runs

RUN useradd --create-home --uid 10001 buglens \
    && mkdir -p /var/lib/buglens/runs \
    && chown -R buglens:buglens /var/lib/buglens
COPY --from=builder /build/dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

USER buglens
WORKDIR /home/buglens
VOLUME ["/var/lib/buglens"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["buglens"]
