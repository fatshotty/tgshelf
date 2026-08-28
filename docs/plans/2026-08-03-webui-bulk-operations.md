# Web UI Bulk Operations Implementation Record

## Completed Work

- Added Alembic revision `0003` with durable operation-job tables and indexes.
- Added the job repository and asynchronous serial runner, including restart
  interruption handling and 30-day cleanup.
- Added the `/api/v1/jobs` create, list, and detail endpoints and documented
  them in OpenAPI.
- Added Web UI selection and bulk actions in Browse and Search.
- Added the Operations view for shared job visibility and item-level errors.
- Rebuilt the static Web UI bundle.

## Verification

- Python test suite.
- Web UI typecheck and production build.
- Alembic history inspection.
- OpenAPI YAML parse and diff whitespace check.
