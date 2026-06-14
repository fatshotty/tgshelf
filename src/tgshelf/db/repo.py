"""Node repository: hierarchy queries (recursive CTE) and node primitives.

All tree reads happen here, in single round-trips: ancestors/subtree/path are
recursive CTEs (the legacy ran a Mongo $graphLookup per path segment), name
matching is always case-insensitive via lower() so it agrees with the partial
unique index uq_nodes_parent_lower_name_active.
"""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import Select, bindparam, delete, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from tgshelf.constants import ROOT_ID, ROOT_NAME
from tgshelf.db.ids import generate_node_id
from tgshelf.db.models import Node, Part

_ID_RETRIES = 3


class DuplicateNameError(Exception):
    """An ACTIVE sibling with the same (case-insensitive) name already exists."""


class NodeRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    # -- bootstrap ---------------------------------------------------------

    async def bootstrap_root(self) -> None:
        """Ensure the root node exists (idempotent).

        Root never carries a channel_id: the master channel lives in config so
        it can be rotated without touching the tree.
        """
        stmt = (
            pg_insert(Node)
            .values(id=ROOT_ID, parent_id=None, name=ROOT_NAME, is_folder=True, state="ACTIVE")
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await self.session.execute(stmt)

    # -- primitives --------------------------------------------------------

    async def get(self, node_id: str, *, state: str | None = None) -> Node | None:
        # populate_existing: refresh the identity-map instance from this query, so
        # a prior bulk UPDATE (which bypasses the ORM) doesn't return stale state
        stmt = (
            select(Node)
            .where(Node.id == node_id)
            .execution_options(populate_existing=True)
        )
        if state is not None:
            stmt = stmt.where(Node.state == state)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create(
        self,
        *,
        name: str,
        parent_id: str,
        is_folder: bool,
        mime: str | None = None,
        channel_id: int | None = None,
        state: str = "ACTIVE",
        size: int = 0,
        content: bytes | None = None,
        info: dict[str, Any] | None = None,
        node_id: str | None = None,
    ) -> Node:
        """Insert a node; generated ids retry on the (astronomically rare) PK
        collision, an ACTIVE same-name sibling raises DuplicateNameError."""
        explicit_id = node_id is not None
        attempts = 1 if explicit_id else _ID_RETRIES
        last_error: IntegrityError | None = None

        for _ in range(attempts):
            values = dict(
                id=node_id if explicit_id else generate_node_id(),
                parent_id=parent_id,
                name=name,
                is_folder=is_folder,
                mime=mime,
                channel_id=channel_id,
                state=state,
                size=size,
                content=content,
                info=info or {},
            )
            try:
                # Core INSERT..RETURNING inside a savepoint: on failure nothing
                # pending is left in the session, so retrying is clean
                async with self.session.begin_nested():
                    result = await self.session.execute(
                        pg_insert(Node).values(**values).returning(Node)
                    )
                    return result.scalar_one()
            except IntegrityError as exc:
                last_error = exc
                message = str(exc.orig)
                if "uq_nodes_parent_lower_name_active" in message:
                    raise DuplicateNameError(
                        f"'{name}' already exists in folder {parent_id}"
                    ) from exc
                if "nodes_pkey" in message and not explicit_id:
                    continue  # id collision: regenerate and retry
                raise
        raise last_error  # type: ignore[misc]  # only reachable after retries

    async def set_fields(self, node_id: str, **fields: Any) -> None:
        await self.session.execute(
            update(Node).where(Node.id == node_id).values(**fields)
        )

    async def purge(self, node_id: str) -> None:
        """Hard-delete a node (parts cascade)."""
        await self.session.execute(delete(Node).where(Node.id == node_id))

    async def content_of(self, node_id: str) -> bytes | None:
        result = await self.session.execute(
            select(Node.content).where(Node.id == node_id)
        )
        return result.scalar_one_or_none()

    # -- parts -------------------------------------------------------------

    async def add_part(
        self,
        file_id: str,
        *,
        idx: int,
        channel_id: int,
        message_id: int,
        doc_id: int | None,
        size: int,
        original_filename: str | None,
    ) -> None:
        await self.session.execute(
            pg_insert(Part).values(
                file_id=file_id,
                idx=idx,
                channel_id=channel_id,
                message_id=message_id,
                doc_id=doc_id,
                size=size,
                original_filename=original_filename,
            )
        )

    async def clear_parts(self, file_id: str) -> None:
        await self.session.execute(delete(Part).where(Part.file_id == file_id))

    async def parts_of(self, file_id: str) -> Sequence[Part]:
        result = await self.session.execute(
            select(Part).where(Part.file_id == file_id).order_by(Part.idx)
        )
        return result.scalars().all()

    async def get_file_by_message(self, channel_id: int, message_id: int) -> Node | None:
        """The file that owns a given Telegram message (dedupe for imports)."""
        result = await self.session.execute(
            select(Node)
            .join(Part, Part.file_id == Node.id)
            .where(Part.channel_id == channel_id, Part.message_id == message_id)
            .limit(1)
        )
        return result.scalars().first()

    async def get_child_by_name(
        self, parent_id: str, name: str, *, state: str | None = None
    ) -> Node | None:
        stmt = select(Node).where(
            Node.parent_id == parent_id, func.lower(Node.name) == name.lower()
        )
        if state is not None:
            stmt = stmt.where(Node.state == state)
        return (await self.session.execute(stmt.limit(1))).scalars().first()

    async def parts_in_subtree(self, root_id: str) -> Sequence[Part]:
        """All parts of every file in the subtree (for purge → Telegram delete)."""
        result = await self.session.execute(
            text(
                """
                WITH RECURSIVE sub AS (
                  SELECT id FROM nodes WHERE id = :root
                  UNION ALL
                  SELECT n.id FROM nodes n JOIN sub ON n.parent_id = sub.id
                )
                SELECT * FROM parts WHERE file_id IN (SELECT id FROM sub)
                """
            ).columns(*Part.__table__.columns),
            {"root": root_id},
        )
        return [Part(**row._mapping) for row in result]

    # -- recursive subtree state changes -----------------------------------

    async def set_state_subtree(
        self, root_id: str, new_state: str, *, from_states: Sequence[str]
    ) -> None:
        await self.session.execute(
            text(
                """
                WITH RECURSIVE sub AS (
                  SELECT id FROM nodes WHERE id = :root
                  UNION ALL
                  SELECT n.id FROM nodes n JOIN sub ON n.parent_id = sub.id
                )
                UPDATE nodes SET state = :state, mtime = now()
                WHERE id IN (SELECT id FROM sub) AND state = ANY(:from_states)
                """
            ),
            {"root": root_id, "state": new_state, "from_states": list(from_states)},
        )

    async def purge_subtree(self, root_id: str) -> None:
        await self.session.execute(
            text(
                """
                WITH RECURSIVE sub AS (
                  SELECT id FROM nodes WHERE id = :root
                  UNION ALL
                  SELECT n.id FROM nodes n JOIN sub ON n.parent_id = sub.id
                )
                DELETE FROM nodes WHERE id IN (SELECT id FROM sub)
                """
            ),
            {"root": root_id},
        )

    # -- hierarchy reads ----------------------------------------------------

    async def children(
        self,
        parent_id: str,
        *,
        state: str | None = "ACTIVE",
        folders_only: bool = False,
        files_only: bool = False,
    ) -> Sequence[Node]:
        stmt = select(Node).where(Node.parent_id == parent_id)
        if state is not None:
            stmt = stmt.where(Node.state == state)
        if folders_only:
            stmt = stmt.where(Node.is_folder.is_(True))
        if files_only:
            stmt = stmt.where(Node.is_folder.is_(False))
        stmt = stmt.order_by(func.lower(Node.name))
        return (await self.session.execute(stmt)).scalars().all()

    async def ancestors(self, node_id: str) -> Sequence[Node]:
        """Ancestor chain of a node, root first, excluding the node itself."""
        parent_of = select(Node.parent_id).where(Node.id == node_id).scalar_subquery()
        base = (
            select(Node.id, Node.parent_id, literal(0).label("depth"))
            .where(Node.id == parent_of)
            .cte("ancestors", recursive=True)
        )
        step = aliased(Node)
        cte = base.union_all(
            select(step.id, step.parent_id, base.c.depth + 1).join(
                base, step.id == base.c.parent_id
            )
        )
        stmt = select(Node).join(cte, Node.id == cte.c.id).order_by(cte.c.depth.desc())
        return (await self.session.execute(stmt)).scalars().all()

    async def subtree(self, root_id: str, *, state: str | None = None) -> Sequence[Node]:
        """All descendants of a node (any depth), shallow first."""
        stmt = (
            select(Node)
            .join(self._subtree_cte(root_id), Node.id == text("subtree.id"))
            .order_by(text("subtree.depth"), func.lower(Node.name))
        )
        if state is not None:
            stmt = stmt.where(Node.state == state)
        return (await self.session.execute(stmt)).scalars().all()

    def _subtree_cte(self, root_id: str):
        base = (
            select(Node.id, literal(0).label("depth"))
            .where(Node.parent_id == root_id)
            .cte("subtree", recursive=True)
        )
        step = aliased(Node)
        return base.union_all(
            select(step.id, base.c.depth + 1).join(base, step.parent_id == base.c.id)
        )

    async def path_of(self, node_id: str) -> str | None:
        """Full path of a node in a single query; '/' for root, None if missing."""
        if node_id == ROOT_ID:
            return "/"
        stmt = text(
            """
            WITH RECURSIVE anc AS (
              SELECT id, parent_id, name, 0 AS depth
              FROM nodes WHERE id = :node_id
              UNION ALL
              SELECT n.id, n.parent_id, n.name, a.depth + 1
              FROM nodes n JOIN anc a ON n.id = a.parent_id
            )
            SELECT '/' || string_agg(name, '/' ORDER BY depth DESC)
            FROM anc WHERE id <> :root_id
            """
        )
        result = await self.session.execute(stmt, {"node_id": node_id, "root_id": ROOT_ID})
        return result.scalar()

    async def resolve(self, path: str, *, state: str = "ACTIVE") -> Node | None:
        """Resolve a '/a/b/c' path to a node, case-insensitively, in one query."""
        segments = [s for s in path.split("/") if s]
        if not segments:
            return await self.get(ROOT_ID)
        stmt = text(
            """
            WITH RECURSIVE walk AS (
              SELECT CAST(:root_id AS TEXT) AS id, 0 AS depth
              UNION ALL
              SELECT n.id, w.depth + 1
              FROM nodes n JOIN walk w ON n.parent_id = w.id
              WHERE n.state = :state
                AND lower(n.name) = lower((CAST(:segments AS text[]))[w.depth + 1])
            )
            SELECT id FROM walk WHERE depth = :n_segments
            """
        ).bindparams(bindparam("segments", type_=None))
        result = await self.session.execute(
            stmt,
            {
                "root_id": ROOT_ID,
                "state": state,
                "segments": segments,
                "n_segments": len(segments),
            },
        )
        node_id = result.scalar()
        return await self.get(node_id) if node_id else None

    async def search(
        self, term: str, *, root_id: str | None = None, state: str = "ACTIVE"
    ) -> Sequence[Node]:
        """Case-insensitive substring search on names (LIKE metachars escaped)."""
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt: Select = (
            select(Node)
            .where(
                func.lower(Node.name).like(f"%{escaped.lower()}%", escape="\\"),
                Node.state == state,
                Node.id != ROOT_ID,
            )
            .order_by(func.lower(Node.name))
        )
        if root_id is not None and root_id != ROOT_ID:
            cte = self._subtree_cte(root_id)
            stmt = stmt.join(cte, Node.id == cte.c.id)
        return (await self.session.execute(stmt)).scalars().all()
