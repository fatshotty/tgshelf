# Concurrent TCP Connections Design

## Goal

Allow each Telegram client to open more than one data-path TCP sender per DC for
upload/download calls. Telegram throttles sustained transfer per connection, so
multiple senders can increase throughput.

## Configuration

`concurrent_tcp_connections` is a global setting:

- `0` or `1`: normalized to one connection.
- `2`: recommended high-throughput value.
- `3+`: allowed but discouraged unless measured.

The setting applies to user and bot clients.

## Behavior

`TgClient` keeps a small sender pool for high-volume raw upload/download calls
and uses round-robin selection. Control-plane calls remain on the normal client
path.

## Risks

More connections do not bypass per-IP limits. The setting should be treated as a
throughput tuning knob, not as a flood-wait solution.

## Verification

Unit tests cover normalization and sender selection where fakeable. Real
throughput impact requires smoke testing.
