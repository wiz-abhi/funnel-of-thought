# Funnel of Thought — web UI.
# Builds from the *repo root* so the sibling `fot/` package (the SigNoz client)
# is available for live mode:
#
#   docker build -f web/Dockerfile -t fot-web .
#
# HuggingFace Spaces builds with the repo root as context automatically and
# expects the app on port 7860.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7860 \
    FOT_MODE=auto

WORKDIR /app

COPY web/requirements.txt /app/web/requirements.txt
RUN pip install --no-cache-dir -r /app/web/requirements.txt

# The UI (explicit paths, so a local web/.venv is never baked into the image),
# plus the SigNoz client it imports for live mode.
COPY web/app.py /app/web/app.py
COPY web/templates /app/web/templates
COPY web/data /app/web/data
COPY fot /app/fot

# Spaces runs as a non-root user; keep the image consistent with that.
RUN useradd -m -u 1000 user && chown -R user:user /app
USER user

EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:7860/healthz')"

WORKDIR /app/web
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
