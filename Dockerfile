FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY gateway ./gateway
RUN pip install --no-cache-dir .
RUN useradd --system --uid 10001 companion && mkdir -p /data && chown companion /data
USER companion
ENV DATABASE_PATH=/data/companion.db PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
