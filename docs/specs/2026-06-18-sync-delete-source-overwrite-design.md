# Sync Delete-Source And Overwrite Design

## Goal

Extend `tgshelf sync` with:

- `--delete-source`: remove local files only after they are safely uploaded.
- `--overwrite`: replace an existing ACTIVE drive file with a newly uploaded
  version.

## Delete Source

After a file upload succeeds, the local file is deleted only if the uploaded
node size still matches the local file size. Parent directories are pruned
best-effort up to, but never including, the root directory passed to `sync`.

Skipped files are not deleted. Failed uploads are not deleted.

## Overwrite

Without `--overwrite`, existing same-size files are skipped and different-size
files are reported as mismatches.

With `--overwrite`, upload proceeds into a TEMP node. After upload succeeds, the
old ACTIVE node is soft-deleted and the new node is activated in one commit. If
upload fails, the old ACTIVE node remains intact.

## Multi-Job Context

Multiple independent `sync` processes may run from cron. They share PostgreSQL
and configuration but do not coordinate through IPC. Account partitioning per job
is recommended when sustained throughput matters.

## Verification

Tests cover delete guards, directory pruning, overwrite swap ordering, old-file
survival on upload failure, and stats/recap counters.
