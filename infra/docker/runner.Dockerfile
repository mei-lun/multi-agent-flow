FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN apt-get update \
    && apt-get install -y --no-install-recommends git docker.io docker-cli \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir . \
    && groupadd --gid 1000 maf \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/sh maf \
    && mkdir -p /var/lib/maf/workspaces \
    && chown -R maf:maf /var/lib/maf
ENV PYTHONPATH=/app/apps/runner/src:/app/apps/server/src:/app/packages/contracts_py/src:/app/packages/artifact_schemas/src:/app/packages/domain/src:/app/packages/observability/src:/app/packages/policy/src:/app/packages/provider_adapters/src:/app/packages/repository_adapters/src:/app/packages/tool_adapters/src
USER maf
CMD ["python", "-m", "maf_runner.main"]
