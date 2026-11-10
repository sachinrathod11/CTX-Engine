# Optional: for Linux machines / servers. On a Mac, install natively instead (Docker can't use the Apple GPU).
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY ctx ./ctx
RUN pip install --no-cache-dir . tiktoken
ENV CTX_HOME=/data \
    CTX_SERVER_HOST=0.0.0.0 \
    CTX_OLLAMA_URL=http://ollama:11434
VOLUME ["/data"]
EXPOSE 7777
CMD ["ctx", "serve", "--no-open"]
