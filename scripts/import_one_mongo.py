"""Import ONE legacy Mongo `entries` document into tgshelf Postgres, by id.

Unlike scripts/migrate_mongo.py (which reads the whole collection and rebuilds the
tree via a BFS from ROOT), this targets a single node and skips plan_tree entirely:
the node's parent already exists in Postgres from the bulk migration, so the
self-FK on parent_id is satisfied and we can upsert the one node + its parts
directly. Use it to catch up a file added to the legacy tool after the migration,
WITHOUT re-running the full migration (which would overwrite any post-migration
edits made in the new UI back to their legacy state).

When the destination folder was recreated in the new tool (e.g. via `mkdir`), it
has a fresh id, so the legacy `parentfolder` no longer resolves — pass `--parent
<new_folder_id>` to reparent the imported node under it.

Usage:
  python scripts/import_one_mongo.py --mongo mongodb://localhost/tgdrive --id <NODE_ID> [--parent <FOLDER_ID>] [--db <DSN>] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

# the script depends on the package but is not part of it
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from migrate_mongo import load, map_item  # noqa: E402

log = logging.getLogger("tgshelf.import-one")


def read_one(mongo_uri: str, node_id: str) -> dict | None:
    """Fetch a single `entries` document by its legacy `id` field."""
    from pymongo import MongoClient

    client = MongoClient(mongo_uri)
    dbname = urlparse(mongo_uri).path.lstrip("/")
    try:
        return client[dbname]["entries"].find_one({"id": node_id})
    finally:
        client.close()


async def run(
    mongo_uri: str, db_dsn: str, node_id: str, *, parent_id: str | None, dry_run: bool
) -> int:
    from sqlalchemy import select

    from tgshelf.db.engine import create_engine, create_session_factory
    from tgshelf.db.models import Node

    log.info("[import-one] reading entry id=%s from mongo %s", node_id, mongo_uri)
    doc = read_one(mongo_uri, node_id)
    if doc is None:
        log.error("[import-one] no entry with id=%s found in mongo", node_id)
        return 1

    node = map_item(doc)
    # The legacy parentfolder points at the OLD folder id. If the destination was
    # recreated in the new tool it has a different id, so override it explicitly.
    if parent_id is not None:
        log.info("[import-one] reparenting %s → %s (was %s)", node.id, parent_id, node.parent_id)
        node.parent_id = parent_id
    log.info(
        "[import-one] mapped %s %r (parent=%s, %d part(s), size=%d)",
        "folder" if node.is_folder else "file", node.name, node.parent_id,
        len(node.parts), node.size,
    )

    if dry_run:
        log.info("[import-one] dry-run: no writes")
        print(f"DRY-RUN import-one: would upsert {node.id} ({node.name!r})")
        return 0

    engine = create_engine(db_dsn)
    try:
        async with create_session_factory(engine)() as session:
            # guard the FK: the parent must already exist in Postgres (it would
            # from the bulk migration). Bail with a clear message instead of a
            # raw IntegrityError if it doesn't.
            if node.parent_id is not None:
                parent = await session.scalar(
                    select(Node.id).where(Node.id == node.parent_id)
                )
                if parent is None:
                    log.error(
                        "[import-one] parent %s not in postgres — run the full "
                        "migration first, or import the parent folder too",
                        node.parent_id,
                    )
                    return 2
            await load(session, [node], dry_run=False)
            log.info("[import-one] upserted %s (%r)", node.id, node.name)
    finally:
        await engine.dispose()

    print(f"import-one: upserted {node.id} ({node.name!r})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import ONE legacy Mongo entry → tgshelf Postgres.")
    parser.add_argument("--mongo", required=True, help="legacy MongoDB URI (with db name)")
    parser.add_argument("--id", required=True, help="legacy node id to import")
    parser.add_argument(
        "--parent",
        help="override parent folder id (use the NEW folder's id when it was "
        "recreated in the new tool); defaults to the legacy parentfolder",
    )
    parser.add_argument("--db", default=os.environ.get("DB"), help="Postgres DSN (or env DB)")
    parser.add_argument("--dry-run", action="store_true", help="map + report only, no writes")
    parser.add_argument("--log", default="info", help="log level")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not args.dry_run and not args.db:
        parser.error("--db (or env DB) is required unless --dry-run")

    return asyncio.run(
        run(args.mongo, args.db, args.id, parent_id=args.parent, dry_run=args.dry_run)
    )


if __name__ == "__main__":
    raise SystemExit(main())
