# RevenueProof API.
#
# Two stages so the built image carries the interpreter and the wheels and none of
# the toolchain that produced them. The dependency set is genuinely large — PyMuPDF,
# SciPy, OR-Tools and scikit-learn together are ~300 MB — and that is why this runs
# in a container rather than a serverless function.
#
# Measured: 140 MB resident at rest, 410 MB peak during a full verification run.
# A 512 MB instance fits with roughly 100 MB of headroom.

# ---- build ------------------------------------------------------------------
FROM python:3.13-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build-only toolchain. Kept out of the final image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md* ./
COPY app ./app

# A virtualenv rather than --user, so the runtime stage copies one directory and
# inherits nothing else from the build.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip setuptools wheel && pip install .

# ---- runtime ----------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    ENVIRONMENT=production

# Runtime-only native dependencies:
#   tesseract  — the OCR path for image-only contract PDFs
#   libgl/glib — PyMuPDF's rasteriser links against these
#   curl       — the container healthcheck below
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv

WORKDIR /srv
COPY app ./app
COPY scripts ./scripts

# Evidence bytes are written here. On a host with ephemeral disk this is lost on
# redeploy, which is correct for a demonstration and wrong for real custody — a
# persistent volume or S3 is the production answer, and the storage layer is
# already an interface rather than a filesystem call.
RUN mkdir -p /srv/data/evidence_vault

# Never run as root. A container that processes uploaded PDFs is exactly the one
# where an unexpected parser bug should not have write access to the image.
RUN useradd --create-home --uid 10001 revenueproof \
    && chown -R revenueproof:revenueproof /srv
USER revenueproof

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# One worker deliberately. The pipeline serialises per workspace with an advisory
# lock and a Redis claim; more workers on a 512 MB instance would multiply the
# 410 MB peak rather than the throughput.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
