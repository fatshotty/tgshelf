# Cluster Metrics And Redis Coordination Design

## Goal

Describe future cross-instance coordination for deployments that run multiple
tgshelf processes against the same PostgreSQL database.

## Current State

The current implementation is per-process:

- pools live in memory;
- proactive rate limiting is in memory;
- metrics describe the current process;
- sessions can be shared when pooled clients use `receive_updates=False`.

## Future Direction

If multi-instance deployments need a global budget, add Redis-backed
coordination for per-account rate limits and optional cluster metrics. Redis is
not required for the current single-instance path and is intentionally not part
of normal tests.

## Constraints

- PostgreSQL remains the source of truth for metadata.
- Redis must be optional.
- The FileSystem facade must not depend on Redis.
- A failed Redis coordinator should degrade to local safety or fail fast with a
  clear operator message, depending on configuration.

## Status

This is a future design note. It is not implemented.
