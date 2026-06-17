"""Channel semantics: effective channel + cross-channel part forwarding.

ONE implementation of each, replacing the legacy's divergent copies
(four effective-channel walks, three forward_file_between_channels).
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from tgshelf.core.upload import PartRecord
from tgshelf.telegram.errors import DocIdMismatch, PartMissing, Severity, TgError

log = logging.getLogger("tgshelf.channels")


def effective_channel(
    node: Any,
    ancestors: Sequence[Any],
    master_channel: int,
    *,
    skip_current: bool = False,
) -> int:
    """The channel a node's files physically live in: the nearest node (self,
    then ancestors closest-first) with a non-NULL channel_id, else the master
    channel from config.

    `ancestors` is root-first (as NodeRepo.ancestors returns). `skip_current`
    ignores the node's own channel — used when computing the channel of a node's
    NEW position during a move, where its stored channel is stale.
    """
    chain = [] if skip_current else [node]
    chain.extend(reversed(ancestors))  # closest ancestor first
    for n in chain:
        if n.channel_id is not None:
            return n.channel_id
    return master_channel


async def forward_parts(
    gateway: Any,
    parts: Sequence[PartRecord],
    dest_channel: int,
    *,
    always_copy: bool = False,
) -> list[PartRecord]:
    """Copy each part to `dest_channel`, returning the new part records.

    Crash-safe ordering (the legacy movebatch's): verify the live message holds
    our document (doc_id), copy it to the destination, record the new
    message_id/doc_id. NO deletion happens here — the caller commits the new
    parts to the DB first, then deletes the originals (delete_originals), so an
    interruption can only leak duplicate copies, never lose data.

    For a MOVE, a part already in `dest_channel` is kept as-is (same file, no
    duplicate). For a COPY (`always_copy=True`) every part is duplicated so the
    new node owns its own messages (no shared-message footgun).
    """
    new_parts: list[PartRecord] = []
    for part in parts:
        if part.channel_id == dest_channel and not always_copy:
            new_parts.append(part)
            continue

        ref = await gateway.get_document(part.channel_id, part.message_id)
        if ref is None:
            raise PartMissing(file_path=str(part.message_id), part_idx=part.idx)
        if part.doc_id is None:
            # legacy record with unknown doc_id: the account-independent integrity
            # check can't run, so this part is forwarded blind. Expected after the
            # Mongo migration (garbage fileids -> NULL), but worth a trace.
            log.warning(
                "[move] forwarding part %s (msg %s in channel %s) WITHOUT doc_id "
                "integrity check (legacy record with no doc_id)",
                part.idx, part.message_id, part.channel_id,
            )
        elif ref.doc_id != part.doc_id:
            raise DocIdMismatch(expected=part.doc_id, found=ref.doc_id)

        message_id, doc_id = await gateway.copy_message(
            part.channel_id, part.message_id, dest_channel
        )
        new_parts.append(
            PartRecord(
                idx=part.idx,
                channel_id=dest_channel,
                message_id=message_id,
                doc_id=doc_id,
                size=part.size,
                original_filename=part.original_filename,
            )
        )
    return new_parts


async def delete_originals(
    gateway: Any, parts: Sequence[PartRecord], *, notifier: Any = None
) -> None:
    """Best-effort deletion of the original messages after a move's DB commit.

    Deletion never fails the move — the move is already committed; at worst a
    duplicate is leaked in the old channel. But not all failures are equal:

    - a *benign* failure (message already gone out-of-band, transient) is just a
      WARNING — nothing to act on;
    - a *critical* one (dead session, lost channel access) means the originals
      could NOT be deleted for a real reason: the duplicate leak is durable and
      the account/channel needs attention. It must be logged at ERROR and, when a
      Notifier is wired, pushed to the alert channel — never hidden as a WARNING.
    """
    for part in parts:
        try:
            await gateway.delete_message(part.channel_id, part.message_id)
        except TgError as exc:
            if exc.severity in (Severity.ERROR, Severity.CRITICAL):
                msg = (
                    f"could not delete original message {part.message_id} in "
                    f"channel {part.channel_id} after move: {exc}"
                )
                log.error("[move] %s", msg)
                if notifier is not None:
                    await notifier.notify(msg, severity=exc.severity)
            else:  # WARNING-class domain error (flood/transient): best-effort
                log.warning(
                    "could not delete original message %s in channel %s: %s",
                    part.message_id, part.channel_id, exc,
                )
        except Exception as exc:  # noqa: BLE001 - best-effort, never fail the move
            log.warning(
                "could not delete original message %s in channel %s: %s",
                part.message_id, part.channel_id, exc,
            )
