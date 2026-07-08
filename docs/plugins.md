# Plugin Hooks

tgshelf plugins are trusted Python extensions loaded in-process. They are not a
sandbox boundary. The plugin API is intentionally small so plugins can work with
logical filesystem metadata without depending on SQLAlchemy internals,
Telegram gateways, or physical file parts.

## Design Goals

- Plugins observe logical tgshelf nodes, not Telegram message parts.
- Hooks receive stable snapshot objects and a narrow `PluginHost` API.
- Plugins can navigate the logical tree, read small text files, and update
  metadata through domain APIs.
- `nodes.info["notes"]` can be read and replaced by plugins; caption updates are
  handled by tgshelf when the active caption template uses `{info}`.
- Initial hooks are file-level: upload, move, copy, rename, delete, and import.

## Configuration

The first plugin configuration is deliberately small:

```yaml
plugins:
  enabled: false
  paths: []
  modules: []
```

Example:

```yaml
plugins:
  enabled: true
  paths:
    - ./plugins
  modules:
    - tmdb_notes:TmdbNotesPlugin
```

`paths` are added to Python import lookup while loading plugins. `modules` use
`module:attribute` syntax. The attribute can be a plugin class, a zero-argument
factory, or an already-created plugin object. Modules are loaded in list order
and hooks run in that same order.

Plugins share the same Python environment as tgshelf. Extra third-party
dependencies are installed by the operator in that environment; dependency
installation is not part of the first plugin implementation.

## PluginNode

Plugins receive lightweight snapshots instead of ORM objects:

```python
@dataclass(frozen=True)
class PluginNode:
    id: str
    name: str
    parent_id: str | None
    is_folder: bool
    mime: str | None
    size: int
    state: str
    info: Mapping[str, Any]
    path: str | None = None
```

If a plugin needs to mutate metadata, it calls `ctx.host`; it does not mutate
`PluginNode` directly.

## PluginHost

The host API wraps tgshelf domain functions and exposes only plugin-supported
capabilities:

```python
await host.get_node(node_id)
await host.parent(node_or_id)
await host.ancestors(node_id)
await host.path_of(node_id)
await host.list_children(folder_id)
await host.get_child_by_name(parent_id, name)
await host.read_text(node_id, max_bytes=1_048_576)
await host.get_info(node_id)
await host.get_info_notes(node_id)
await host.update_info(node_id, patch)
await host.set_info_notes(node_id, notes)
await host.resync_caption(node_id)
```

`read_text()` reads through the FileSystem facade, enforces a default 1 MiB
limit, and decodes UTF-8. It fails cleanly if the node is not a file, if the
content is too large, or if the content is not valid text.

`update_info()` performs a shallow metadata merge but must not update `notes`.
Plugins use `set_info_notes()` for notes so tgshelf can enforce the 200-character
limit and resync Telegram captions when needed.

## Hooks

```text
before_file_upload
after_file_upload
before_file_move
after_file_move
before_file_copy
after_file_copy
before_file_rename
after_file_rename
before_file_delete
after_file_delete
after_file_import
```

Hook contexts always expose `ctx.operation`, `ctx.host`, `ctx.node`,
`ctx.old_parent_id`, `ctx.new_parent_id`, `ctx.old_path`, `ctx.new_path`,
`ctx.source_node`, and `ctx.target_node`. Fields that do not apply to the
operation are `None`.

`before_*` hooks can block an operation by raising `PluginError`. Unexpected
exceptions in `before_*` hooks are wrapped as `PluginError` and also block the
operation. `before_file_upload` runs after the temporary logical node has been
created and before bytes are uploaded; if it blocks, the temporary node is
purged.

`after_*` hooks run after the core operation has committed. They may update
metadata, including notes, but they never roll back the already committed core
operation. Errors are logged with the `[plugin]` marker.

`after_file_upload` runs after the upload transaction, caption sync, and size
check have completed. The hook receives the uploaded logical file node, not its
Telegram parts.

`before_file_copy` receives the source file as `ctx.node` and `ctx.source_node`;
`after_file_copy` receives the copied file as `ctx.node` and `ctx.target_node`,
with `ctx.source_node` still pointing to the original logical file.

`after_file_import` runs when an uncataloged Telegram message is imported into
the logical tree or when a deleted same-name node is resurrected. Already
cataloged messages and active same-name siblings are no-op imports and do not run
the hook.

Folder-level hook names are intentionally out of scope for the first
implementation. Some folder operations still emit file-level hooks for affected
files:

- moving a folder emits file-move hooks for active descendant files;
- copying a folder emits file-copy hooks for each copied descendant file;
- deleting a folder emits file-delete hooks for active descendant files.

`before_file_import` is also intentionally out of scope because imports are
driven by existing Telegram messages and currently have no pre-created logical
file node to expose.

## Example: TMDB Notes From tvshow.nfo

Given an uploaded logical file:

```text
/media/movies/XXX (2026)/Season 01/XXX (2026) - S01E01 - Episode 1.mkv
```

a plugin can climb to the show folder, read `tvshow.nfo`, extract a TMDB id, and
write a note:

```python
import re


class TmdbNotesPlugin:
    async def after_file_upload(self, ctx):
        ancestors = await ctx.host.ancestors(ctx.node.id)
        if len(ancestors) < 2:
            return

        show_folder = ancestors[-2]
        nfo = await ctx.host.get_child_by_name(show_folder.id, "tvshow.nfo")
        if nfo is None:
            return

        text = await ctx.host.read_text(nfo.id)
        match = re.search(r"<tmdbid>(\d+)</tmdbid>", text)
        if not match:
            return

        notes = await ctx.host.get_info_notes(ctx.node.id)
        lines = [line for line in notes.splitlines() if not line.startswith("TMDB:")]
        lines.append(f"TMDB: {match.group(1)}")
        await ctx.host.set_info_notes(ctx.node.id, "\n".join(lines))
```

`set_info_notes()` replaces the whole notes field. Plugins that want to update a
single logical line must read the current notes, edit the text, and write the
final value back.
