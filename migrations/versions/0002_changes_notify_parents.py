"""enrich changes pg_notify payload with parent ids

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-22

The rclone control-plane bridge invalidates rclone's VFS directory cache via
`vfs/forget dir=<path>`, so it needs the affected DIRECTORY, not just the node:

- on DELETE/purge the node row may be gone by the time the bridge resolves it;
- on MOVE both the source and the destination directory must be invalidated.

This redefines the two trigger FUNCTIONS (the triggers themselves are unchanged,
so the toggle in `changes_feed.py` keeps working) to add `parent_id` and, on a
MOVE, `old_parent_id` to the `pg_notify` JSON. The `changes` TABLE is untouched
(it still records only node_id + op): retention and existing consumers are
unaffected. Paths are still resolved in Python by the bridge (no path functions
in the DB — convention, see rev 0001), so the trigger only emits ids.
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


_NODES_FN_V2 = """
CREATE OR REPLACE FUNCTION nodes_changes_fn() RETURNS trigger AS $$
DECLARE
  v_op TEXT;
  v_node_id TEXT;
  v_parent_id TEXT;
  v_old_parent_id TEXT := NULL;
  v_seq BIGINT;
BEGIN
  IF TG_OP = 'INSERT' THEN
    v_op := 'CREATE';
    v_node_id := NEW.id;
    v_parent_id := NEW.parent_id;
  ELSIF TG_OP = 'UPDATE' THEN
    IF NEW.parent_id IS DISTINCT FROM OLD.parent_id THEN
      v_op := 'MOVE';
      v_old_parent_id := OLD.parent_id;
    ELSE
      v_op := 'UPDATE';
    END IF;
    v_node_id := NEW.id;
    v_parent_id := NEW.parent_id;
  ELSE
    v_op := 'DELETE';
    v_node_id := OLD.id;
    v_parent_id := OLD.parent_id;
  END IF;
  INSERT INTO changes (node_id, op) VALUES (v_node_id, v_op) RETURNING seq INTO v_seq;
  PERFORM pg_notify(
    'changes',
    json_build_object(
      'seq', v_seq, 'node_id', v_node_id, 'op', v_op,
      'parent_id', v_parent_id, 'old_parent_id', v_old_parent_id
    )::text
  );
  RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

_PARTS_FN_V2 = """
CREATE OR REPLACE FUNCTION parts_changes_fn() RETURNS trigger AS $$
DECLARE
  v_node_id TEXT;
  v_parent_id TEXT;
  v_seq BIGINT;
BEGIN
  v_node_id := COALESCE(NEW.file_id, OLD.file_id);
  -- cascade delete of the file already emitted its DELETE event
  SELECT parent_id INTO v_parent_id FROM nodes WHERE id = v_node_id;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  INSERT INTO changes (node_id, op) VALUES (v_node_id, 'UPDATE') RETURNING seq INTO v_seq;
  PERFORM pg_notify(
    'changes',
    json_build_object(
      'seq', v_seq, 'node_id', v_node_id, 'op', 'UPDATE',
      'parent_id', v_parent_id, 'old_parent_id', NULL
    )::text
  );
  RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""


# the original (rev 0001) bodies, for downgrade
_NODES_FN_V1 = """
CREATE OR REPLACE FUNCTION nodes_changes_fn() RETURNS trigger AS $$
DECLARE
  v_op TEXT;
  v_node_id TEXT;
  v_seq BIGINT;
BEGIN
  IF TG_OP = 'INSERT' THEN
    v_op := 'CREATE';
    v_node_id := NEW.id;
  ELSIF TG_OP = 'UPDATE' THEN
    IF NEW.parent_id IS DISTINCT FROM OLD.parent_id THEN
      v_op := 'MOVE';
    ELSE
      v_op := 'UPDATE';
    END IF;
    v_node_id := NEW.id;
  ELSE
    v_op := 'DELETE';
    v_node_id := OLD.id;
  END IF;
  INSERT INTO changes (node_id, op) VALUES (v_node_id, v_op) RETURNING seq INTO v_seq;
  PERFORM pg_notify(
    'changes',
    json_build_object('seq', v_seq, 'node_id', v_node_id, 'op', v_op)::text
  );
  RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

_PARTS_FN_V1 = """
CREATE OR REPLACE FUNCTION parts_changes_fn() RETURNS trigger AS $$
DECLARE
  v_node_id TEXT;
  v_seq BIGINT;
BEGIN
  v_node_id := COALESCE(NEW.file_id, OLD.file_id);
  -- cascade delete of the file already emitted its DELETE event
  IF NOT EXISTS (SELECT 1 FROM nodes WHERE id = v_node_id) THEN
    RETURN NULL;
  END IF;
  INSERT INTO changes (node_id, op) VALUES (v_node_id, 'UPDATE') RETURNING seq INTO v_seq;
  PERFORM pg_notify(
    'changes',
    json_build_object('seq', v_seq, 'node_id', v_node_id, 'op', 'UPDATE')::text
  );
  RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.execute(_NODES_FN_V2)
    op.execute(_PARTS_FN_V2)


def downgrade() -> None:
    op.execute(_NODES_FN_V1)
    op.execute(_PARTS_FN_V1)
