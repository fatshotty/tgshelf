# Web UI Metrics Design

## Goal

Add a live `/stats` view to the React Web UI using the existing metrics stream.

## Backend Contract

The UI consumes `/api/v1/metrics/stream` through `EventSource`. Snapshots include
stream counters, buffer estimates, pool availability, member in-flight counts,
cooldown state, quarantine state, and channel eligibility.

## UI Behavior

- Route: `/stats`.
- Cards: active streams, throughput, buffered bytes, total streams, total bytes,
  degraded stream count.
- Sparkline: derived throughput from adjacent `bytes_total` samples.
- Pool sections: clients and bots with available/total, in-flight, cooldown, and
  ineligible-channel indicators.
- SSE reconnects are handled by the browser; invalid JSON samples are ignored.

## Constraints

No charting dependency is added. The sparkline is a small hand-written SVG.

## Verification

The Web UI has no test runner. Verification is `npm run typecheck`,
`npm run build`, and manual browser checks against a live backend.
