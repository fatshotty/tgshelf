# syntax=docker/dockerfile:1.7

FROM node:20-bookworm-slim AS webui-build
WORKDIR /build

COPY webui/package*.json ./webui/
RUN cd webui && npm ci

COPY webui ./webui
RUN mkdir -p src/tgshelf/webui && cd webui && npm run build


FROM python:3.13-slim AS wheel-build
WORKDIR /build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=webui-build /build/src/tgshelf/webui/static ./src/tgshelf/webui/static
RUN python -m pip wheel . --no-deps --wheel-dir /wheels


FROM python:3.13-slim AS runtime
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TGSHELF_CONFIG=/config/config.yaml \
    TGSHELF_RUN_MIGRATIONS=1

RUN groupadd --system --gid 10001 tgshelf \
    && useradd --system --uid 10001 --gid tgshelf --home-dir /nonexistent --shell /usr/sbin/nologin tgshelf \
    && mkdir -p /config /data /app \
    && chown -R tgshelf:tgshelf /data /app

COPY --from=wheel-build /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/tgshelf-*.whl \
    && rm -rf /wheels

COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY docker/entrypoint.sh /usr/local/bin/tgshelf-entrypoint
RUN chmod +x /usr/local/bin/tgshelf-entrypoint

USER tgshelf
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:3000/ping', timeout=3).read()"

ENTRYPOINT ["tgshelf-entrypoint"]
CMD ["serve"]
