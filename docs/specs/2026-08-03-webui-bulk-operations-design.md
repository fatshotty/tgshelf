# Web UI Bulk Operations Design

## Scope

The Web UI supports bulk move and soft-delete from both browse and search
results. Requests must survive the originating browser session and remain
visible to every authenticated user of the instance.

## Durable Job Model

`operation_jobs` stores the operation, target folder (for moves), aggregate
counts, lifecycle state, timestamps, and an optional runner error.
`operation_job_items` stores a selected node's position, path/name snapshot,
item state, timestamps, and detailed error text. Node and destination IDs are
not foreign keys so a hard purge does not erase operational history.

Supported job states are `queued`, `running`, `completed`, `failed`, and
`interrupted`. Item states are `pending`, `running`, `succeeded`, `failed`, and
`skipped`.

## Execution Rules

- Jobs run selected items serially.
- An item failure is recorded and does not block later items.
- Selecting a folder together with one of its descendants records the descendant
  as `skipped`, because the parent operation already includes it.
- At startup, unfinished jobs become `interrupted`; remaining pending/running
  items become `skipped`. Jobs are never automatically resumed.
- Terminal jobs are retained for 30 days and removed by periodic cleanup.

## HTTP Contract

- `POST /api/v1/jobs` creates a move or soft-delete job and returns `202`.
- `GET /api/v1/jobs` lists jobs with optional state filtering and pagination.
- `GET /api/v1/jobs/{job_id}` returns a job with item-level outcomes.

The previous single-node endpoints remain unchanged. The job endpoints are the
asynchronous contract for bulk work.

## UI And Logging

Browse and Search expose active-node selection, select-all/clear controls, and
bulk move/delete actions. The Operations page polls active jobs and displays
detailed failures and skipped items.

Job logs use the stable, grep-friendly prefix `[job] job_id=<id>`.
