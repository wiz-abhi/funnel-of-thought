# signoz-funnel-mcp -- MCP stdio server for SigNoz Trace Funnels.
#
# Build (from the repository root, so the package directory is in context):
#   docker build -f signoz_funnel_mcp/Dockerfile -t signoz-funnel-mcp .
#
# Run (stdio -- note -i is required; MCP speaks over stdin/stdout):
#   docker run --rm -i \
#     -e SIGNOZ_URL=http://host.docker.internal:8080 \
#     -e SIGNOZ_JWT="$SIGNOZ_JWT" \
#     signoz-funnel-mcp
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so edits to the source don't invalidate the layer.
COPY signoz_funnel_mcp/requirements.txt ./signoz_funnel_mcp/requirements.txt
RUN pip install --no-cache-dir -r signoz_funnel_mcp/requirements.txt

COPY signoz_funnel_mcp/ ./signoz_funnel_mcp/

# Run unprivileged.
RUN useradd --create-home --uid 10001 mcp
USER mcp

# Default target is a SigNoz on the container host; override at run time.
ENV SIGNOZ_URL=http://host.docker.internal:8080

# stdio transport -- no port is exposed.
ENTRYPOINT ["python", "-m", "signoz_funnel_mcp.server"]
