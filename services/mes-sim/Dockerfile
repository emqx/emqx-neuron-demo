FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY mes.py .
RUN uv sync --script mes.py

CMD ["uv", "run", "--script", "mes.py"]
