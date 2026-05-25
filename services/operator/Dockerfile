FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY app.py .
COPY templates/ templates/
COPY static/ static/
RUN uv sync --script app.py

CMD ["uv", "run", "--script", "app.py"]
