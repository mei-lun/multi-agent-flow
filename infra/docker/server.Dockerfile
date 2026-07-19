FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .
ENV PYTHONPATH=/app/apps/server/src:/app/packages/contracts_py/src:/app/packages/artifact_schemas/src:/app/packages/domain/src:/app/packages/observability/src:/app/packages/policy/src:/app/packages/provider_adapters/src:/app/packages/repository_adapters/src:/app/packages/tool_adapters/src
EXPOSE 8000
CMD ["uvicorn", "maf_server.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
