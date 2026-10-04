FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Configuration comes from environment variables (see README); flags may be appended at run time.
ENTRYPOINT ["qbo-airbyte-token-rotator"]
