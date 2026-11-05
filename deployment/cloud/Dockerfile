FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[server]" && useradd --uid 10001 --create-home broker
USER broker
EXPOSE 8766
ENTRYPOINT ["workswarm-auth", "serve", "--home", "/config", "--listen", "0.0.0.0"]
