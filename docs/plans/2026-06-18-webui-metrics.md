# Web UI Metrics Implementation Plan

## Goal

Add a `/stats` page that shows live backend and pool metrics.

## Completed Tasks

- Add client support for the metrics SSE endpoint.
- Add stats route and navigation entry.
- Add metric cards for stream counters and buffer state.
- Add derived throughput calculation from adjacent byte samples.
- Add a hand-written SVG sparkline.
- Add pool member tiles for clients and bots.
- Add CSS consistent with the existing Web UI.
- Run frontend typecheck and build.

## Important Constraints

- No new charting dependency.
- EventSource handles reconnects.
- Bad samples are ignored while the last good snapshot remains visible.

## Verification

Use `npm run typecheck`, `npm run build`, and a manual browser check against
`tgshelf serve`.
