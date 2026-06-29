# Web UI Tree Management Implementation Plan

## Goal

Expose existing filesystem operations in the Web UI browse view.

## Completed Tasks

- Add `state` query support to the children endpoint.
- Update OpenAPI for the new `state` parameter.
- Add API client methods for tree mutations.
- Add browse toolbar controls for new folder and show deleted.
- Add row action menus.
- Add modals for rename, set channel, move, copy, and confirmations.
- Add tree picker for move/copy destinations.
- Invalidate react-query caches after successful mutations.
- Run backend route tests and frontend typecheck/build.

## Important Constraints

- Folder move/copy can return `202 Accepted` because backend work may continue in
  the background.
- Purge is irreversible and must require explicit confirmation.
- Set-channel remains metadata-only for existing descendants under the current
  domain decision.

## Verification

Run backend tests covering `state` filtering and route coverage. Run Web UI
typecheck/build and manually exercise all menu actions.
