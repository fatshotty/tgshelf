# Copy Message Caption Override

`Gateway.copy_message()` accepts an optional `caption` keyword argument. When it
is omitted, the copied message preserves the source caption. When it is set, the
copy is sent server-side with the provided caption.

`core.channels.forward_parts()` exposes the same behavior through an optional
`caption_factory(part)` callback. Existing move/copy flows do not pass a factory,
so their behavior is unchanged. Recovery tools can pass a factory to restore
messages into the main channel with canonical `filename: ...` captions while
still reusing the existing document verification, server-side copy, and Telegram
write rate limiting.

The planned missing-message recovery script should require `--dry-run` only when
the operator wants a dry run. If `--dry-run` is absent, the command is allowed to
perform the restore and must keep a resumable ledger before making Telegram or
database changes.
