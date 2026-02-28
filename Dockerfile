FROM python:3.12-slim AS base

WORKDIR /app

RUN adduser --system --no-create-home appuser

FROM base AS builder

RUN pip install --no-cache-dir hatch

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir . && \
    pip install --no-cache-dir "cowork-platform[sdk] @ git+https://github.com/suman724/cowork-platform.git@feature/init"

FROM base AS runtime

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn
COPY src/ src/

USER appuser

EXPOSE 8000

CMD ["uvicorn", "session_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
