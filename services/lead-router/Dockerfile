# syntax=docker/dockerfile:1

# ---- Build stage: install the app and its dependencies into /install ----------
FROM python:3.13-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
RUN pip install --prefix=/install .

# ---- Runtime stage: small image, no compilers, runs as a non-root user --------
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 router
COPY --from=build /install /usr/local
WORKDIR /home/router
USER router

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2)"

# The same image runs the worker: `python -m app.worker` (see docker-compose.yml).
# --proxy-headers honors X-Forwarded-For only from the addresses in FORWARDED_ALLOW_IPS
# (default 127.0.0.1), so clients can't forge the IP recorded as consent evidence.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers"]
