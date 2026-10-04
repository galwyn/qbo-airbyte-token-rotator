FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# Which cloud SDK to install: gcp (default), aws or azure.
ARG TOKEN_STORE_EXTRA=gcp
RUN pip install --no-cache-dir ".[${TOKEN_STORE_EXTRA}]"

# Configuration comes from environment variables (see README); flags may be appended at run time.
ENTRYPOINT ["qbo-airbyte-token-rotator"]
