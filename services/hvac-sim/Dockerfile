FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY hvac.py .
RUN uv sync --script hvac.py

EXPOSE 47808/udp

CMD ["uv", "run", "--script", "hvac.py"]
