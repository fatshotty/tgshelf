# Sync Delete-Source And Overwrite Implementation Plan

## Goal

Add safe local cleanup and overwrite semantics to `tgshelf sync`.

## Completed Tasks

- Add CLI flags `--delete-source` and `--overwrite`.
- Extend sync stats and recap output for overwritten and deleted counts.
- Implement guarded local file deletion after successful upload.
- Implement best-effort parent directory pruning that never removes the sync
  root.
- Add overwrite support to the write path through a TEMP-node swap.
- Ensure upload failure leaves the old ACTIVE node intact.
- Add tests for local deletion, pruning, overwrite success, and overwrite
  failure.

## Important Constraints

- Delete-source never deletes skipped or failed files.
- Overwrite always uploads a fresh payload before changing the visible ACTIVE
  node.
- Existing HTTP upload behavior remains conflict-first unless explicitly forced
  by its own endpoint rules.

## Verification

Run unit tests for sync helpers and FileSystem overwrite behavior. Use a fake
gateway for Telegram operations.
