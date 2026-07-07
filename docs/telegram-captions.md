# Telegram Caption Templates

This document describes how tgshelf renders Telegram message captions for
Telegram-backed file parts.

Telegram captions are operational metadata. They are not part of the download
path, but they are important for disaster recovery because Telegram channel
history may be the only remaining source that connects physical messages back to
the logical filesystem tree.

## Goals

- Let operators choose which recovery metadata is written into Telegram
  captions.
- Keep caption rendering centralized and consistent across upload, rename, copy,
  move, merge, split, and reorder operations.
- Re-render captions only when an operation changes data that is used by the
  configured template.
- Make future metadata and plugin work possible without changing the basic
  caption rendering contract.

Changing the caption template is not a historical migration. New operations use
the current template from that point forward. Existing Telegram messages are not
rewritten automatically when the template changes.

## Configuration

The caption template is configured with the top-level `caption` section:

```yaml
caption:
  template: |
    {path}
    fileName: {filename}
```

The template defines the whole Telegram caption. There is no mandatory
`fileName:` line and no special line order. If an operator wants a `fileName:`
line, it should be included explicitly in the template.

An empty template disables tgshelf-managed captions:

```yaml
caption:
  template: ""
```

With an empty template, tgshelf does not create or update Telegram captions for
upload, rename, copy, move, merge, split, or reorder operations.

## Placeholders

The first implementation supports these placeholders:

| Placeholder | Meaning |
| --- | --- |
| `{id}` | Stable logical tgshelf node id (`nodes.id`). |
| `{path}` | Logical path of the containing folder. Root is `/`. This does not include the logical file name. |
| `{filename}` | Logical filename for the current Telegram part, for example `Inception.mkv.001`. |
| `{part_idx}` | 1-based logical part index. |
| `{parts}` | Total number of parts in the logical file. |
| `{size}` | Size in bytes of the current Telegram part. |
| `{mime}` | MIME type stored on the node, as calculated or normalized by tgshelf. |
| `{channel_id}` | Physical Telegram channel id for the current part. |

`{size}` intentionally means the current part size only. There is no aggregate
logical file size placeholder in this phase.

`{info}` is reserved for future work and is not implemented yet. The node
metadata field is JSON, and its text representation is still undecided. Until
that design is completed, configuration validation should reject `{info}` with a
clear error.

## Example

Given this template:

```yaml
caption:
  template: |
    {path}
    fileName: {filename}
    id: {id}
    part: {part_idx}/{parts}
    size: {size}
    mime: {mime}
```

For a two-part logical file:

```text
/backup/movies-bk-1/Inception.mkv
```

the first Telegram part caption is rendered as:

```text
/backup/movies-bk-1
fileName: Inception.mkv.001
id: abc123def4
part: 1/2
size: 2097152
mime: video/x-matroska
```

The second Telegram part uses the same logical parent path but its own part
filename, part index, channel id, and part size.

## Re-render Rules

tgshelf should re-render captions when an operation changes any value used by
the configured template.

The renderer should track placeholder dependencies instead of assuming that only
directly updated database fields matter. For example, if `{mime}` is present and
renaming a file changes the MIME value stored by tgshelf, captions must be
updated even though the visible placeholder is not `{filename}`.

Examples:

- A template containing `{filename}` changes after a rename, merge, split, or
  reorder when the logical part filename changes.
- A template containing `{path}` changes after a move and for newly copied
  files in the destination folder.
- A template containing `{mime}` changes when the node MIME changes, including a
  rename that causes tgshelf to recalculate MIME.
- A template containing `{part_idx}` or `{parts}` changes after merge, split,
  or reorder.
- An empty template never triggers caption writes.

## Operation Semantics

### Upload

Every new Telegram part is sent with a caption rendered from the current
template. If the template is empty, tgshelf does not manage a caption for the
new message.

### Rename

After the logical node is renamed, Telegram-backed files have their part
captions re-rendered only if the template depends on data that changed, such as
`{filename}` or `{mime}`. Inline database-only files have no Telegram caption to
update.

### Copy

Copy always duplicates Telegram-backed parts. Newly copied Telegram messages
must receive captions rendered from the current template and the destination
node's current metadata. Copy does not repair stale captions on the source file.

### Move

If move changes the physical channel, copied messages in the destination channel
must receive captions rendered from the current template. If move does not copy
messages physically, existing captions are edited only when the template depends
on data that changed, such as `{path}`.

Best-effort deletion of old physical messages remains separate from caption
rendering.

### Merge

The resulting logical file owns all merged parts. Captions for the final part
set are rendered from the target node's final name, parent path, MIME, part
order, part count, and per-part metadata. Donor nodes are removed and do not
keep independent captions.

### Split

The source file keeps the unselected parts and is re-indexed. Extracted parts
become new logical files, usually one part each. Captions for both the remaining
source parts and the newly created files are rendered from their final logical
state when the template depends on changed values.

### Reorder

Reorder changes logical part positions. If the template depends on position
derived values, such as `{filename}` or `{part_idx}`, affected captions are
updated.

## Future `{info}` Placeholder

The future `{info}` placeholder will depend on `nodes.info`, which belongs to
the logical node rather than an individual Telegram part.

When `{info}` is implemented, updating metadata through plugins, the Web UI, or
another domain API should re-render all captions for a Telegram-backed file if
the active template uses `{info}`. For a five-part file, that means five
Telegram caption edits. If the active template does not use `{info}`, metadata
updates should not touch Telegram captions.

The exact text representation of `nodes.info` is intentionally left open.

## Historical Captions

Existing Telegram history may contain captions rendered by older tgshelf
versions or by a previous template. tgshelf does not rewrite those captions as a
side effect of changing configuration.

Historical reconciliation and bulk caption repair should be handled by a
dedicated command or sanitizer flow.
