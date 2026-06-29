# Web UI Tree Management Design

## Goal

Expose existing filesystem management operations in the React Web UI:

- new folder;
- rename;
- set folder channel;
- soft delete;
- restore;
- purge;
- move;
- copy;
- show deleted toggle.

## Backend Change

`GET /api/v1/nodes/{id}/children` accepts `state=ACTIVE|DELETED|TEMP|all`, with
`ACTIVE` as the default. This lets the UI display deleted nodes for restore or
purge workflows.

All other operations use existing endpoints.

## UI Behavior

- A compact toolbar contains new-folder and show-deleted controls.
- Each row has an action menu.
- Move and copy use a modal tree picker.
- Destructive actions use confirmation.
- Mutations invalidate the current browse query and relevant destination queries.

## Verification

Backend tests cover the `state` parameter and OpenAPI route coverage. Frontend
verification is `npm run typecheck`, `npm run build`, and manual e2e checks.
