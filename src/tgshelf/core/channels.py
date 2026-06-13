"""Channel semantics: effective channel + cross-channel part forwarding.

ONE implementation of each, replacing the legacy's divergent copies
(four effective-channel walks, three forward_file_between_channels).
"""

from __future__ import annotations

from typing import Any, Sequence


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
