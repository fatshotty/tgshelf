#!/bin/sh
set -eu

config_path="${TGSHELF_CONFIG:-/config/config.yaml}"

run_tgshelf() {
    exec tgshelf --config "$config_path" "$@"
}

if [ "$#" -eq 0 ]; then
    set -- serve
fi

case "$1" in
    serve)
        if [ "${TGSHELF_RUN_MIGRATIONS:-1}" != "0" ] && [ "${TGSHELF_RUN_MIGRATIONS:-1}" != "false" ]; then
            alembic -c /app/alembic.ini upgrade head
        fi
        run_tgshelf serve
        ;;
    tgshelf)
        shift
        run_tgshelf "$@"
        ;;
    alembic)
        shift
        exec alembic -c /app/alembic.ini "$@"
        ;;
    *)
        run_tgshelf "$@"
        ;;
esac
