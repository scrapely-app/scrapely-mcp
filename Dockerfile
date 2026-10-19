# Hosted (multi-tenant) Scrape.ly MCP server.
# Build from PyPI:   docker build --build-arg VERSION=0.2.1 -t scrapely-mcp .
# Build from source: docker build --build-arg SOURCE=local -t scrapely-mcp .
FROM python:3.12-slim

ARG VERSION=0.2.1
ARG SOURCE=pypi

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SCRAPELY_MCP_HOST=0.0.0.0 \
    SCRAPELY_MCP_PORT=8000

WORKDIR /app
COPY . /src
RUN if [ "$SOURCE" = "local" ]; then pip install /src; else pip install "scrapely-mcp==${VERSION}"; fi \
    && rm -rf /src \
    && useradd --create-home --uid 10001 mcp

USER mcp
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["scrapely-mcp-http"]
