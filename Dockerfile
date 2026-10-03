FROM python:3.12-slim

# ffmpeg/ffprobe: technical QA and video assembly.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /srv/hatch
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/srv/hatch/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app app
COPY integrations integrations
COPY migrations migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

RUN useradd --create-home hatch && mkdir -p var && chown -R hatch var
USER hatch
EXPOSE 8321
# Apply migrations, then serve. The worker overrides the command.
CMD ["sh", "-c", "alembic upgrade head && hatch serve --host 0.0.0.0 --port 8321"]
