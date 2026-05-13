# nuclino-export

Export a Nuclino team's content (workspaces, items, file attachments) to disk.
Useful for backups, offline reading, or migrating content to another system.

The export is **resumable**: re-running into the same output directory skips
items and files that are already on disk. If the run is interrupted or the API
rate-limits you, just run it again with the same `--output` and it picks up
where it left off.

## Requirements

- Python 3.10 or newer
- A Nuclino API token (see below)
- Network access to `api.nuclino.com` and `nuclino-files.s3.eu-central-1.amazonaws.com`
- Optional: [pandoc](https://pandoc.org/) on PATH if you want `.docx` output

No external Python packages are needed; the tool uses the standard library only.

## Getting an API token

1. Sign in to Nuclino and open **Settings & members** -> **API**.
2. Click **Create API key** and copy the generated token.
3. Store it in one of three places (in order of precedence):
   - `--token <value>` on the command line (avoid this; leaks into shell history)
   - `NUCLINO_API_TOKEN` environment variable
   - A file (default: `~/.config/nuclino/token`), `chmod 600` recommended

The token grants read access to everything your Nuclino user can see. Treat it
like a password and rotate it via the same UI if it's exposed.

## Usage

```bash
# Export everything to ./my-backup/
python3 nuclino_export.py --output ./my-backup/

# Use a token file at a non-default path
python3 nuclino_export.py --output ./my-backup/ --token-file ~/secrets/nuclino

# Limit to a single workspace (get the ID from the Nuclino URL)
python3 nuclino_export.py --output ./my-backup/ \
    --workspace 5d26ded0-35ac-4759-9003-838a2e66ea99

# Skip file attachments (text content only)
python3 nuclino_export.py --output ./my-backup/ --skip-files

# JSON only, no markdown rendering
python3 nuclino_export.py --output ./my-backup/ --format json

# Also produce .docx files for each item (requires pandoc on PATH).
# Combine formats with a comma:
python3 nuclino_export.py --output ./my-backup/ --format markdown,json,docx

# Slow it down if you keep hitting 429s (default 0.5s between calls)
python3 nuclino_export.py --output ./my-backup/ --throttle 1.0
```

Run `python3 nuclino_export.py --help` for the full flag list.

## Output layout

```
<output>/
  manifest.json                            # run metadata + counters per team/workspace
  _index.json                              # global item-id -> {workspace, basename, title} map
  errors.log                               # per-failure log lines
  workspaces/
    <slug>/                                # human-readable workspace slug
      _workspace.json                      # workspace API record
      _summary.json                        # per-workspace counts (items, files, errors)
      items/
        <title-slug>__<short-id>.json      # full item JSON (with content body)
        <title-slug>__<short-id>.md        # markdown rendering with YAML frontmatter
        <title-slug>__<short-id>.docx      # Word/Docs-ready rendering (when --format includes docx)
      files/
        <stem>__<short-id>.<ext>           # attachment; original filename preserved, short-id appended
```

`<short-id>` is the first 8 hex chars of the corresponding UUID. The short-id
appears after `__` so that names sort by their human-readable prefix in a file
manager. If two workspaces slug to the same name, the workspace dir gets a
`__<short-id>` suffix too.

Each `.md` file starts with a frontmatter block that mirrors key API fields:

```yaml
---
nuclino_id: 82b65370-f216-466b-a1aa-ca750fa60ac2
title: "2023.05.30. Architect meeting"
workspace: "Engineering"
workspace_id: 350188e0-e3fe-488f-bf9a-55431ec81115
url: "https://app.nuclino.com/t/b/82b65370-..."
created: 2023-05-30T14:10:25.048Z
updated: 2023-11-21T13:10:01.805Z
exported: 2026-05-13
file_ids:
  - 31814da8-d7dd-4d27-abb9-752608d2ea21
child_item_ids:
  - 20bcbd6b-a064-4105-ae05-95f911d88177
---
```

Both `item` (pages) and `collection` (folders/clusters) objects are saved as
JSON. Only `item` objects get a markdown file, since collections have no body.

## Link rewriting

Nuclino-hosted URLs embedded in the markdown content will stop resolving the
moment the source account loses access (subscription ends, token rotated,
permissions changed). To keep the export self-contained, the tool rewrites
those URLs at write time for both `.md` and `.docx` output (raw `.json` keeps
the API response verbatim):

- **Inline images** (`https://files.nuclino.com/files/.../<filename>`) are
  rewritten to a relative path under the workspace's `files/` directory, so
  pandoc embeds them into the `.docx` and markdown viewers render them inline.
- **User mentions** (`[Name](https://app.nuclino.com/users/...)`) lose the
  dead profile link; the display name is kept as plain text.
- **Cross-document references** (`/t/b/<uuid>` and the longer team-path form)
  are resolved to the local relative path of the target item using a global
  index built during a discovery pre-pass. References to items that aren't in
  the export (e.g. when `--workspace` filters the run) keep only the link
  text.

The discovery pre-pass enumerates all items across all workspaces in scope
before any output is written; this adds roughly one list API call per
workspace, but lets every cross-reference resolve to its eventual filename.

## Rate limiting and retries

Nuclino's API enforces request quotas. The tool throttles to one request per
500ms by default and on `HTTP 429`/`5xx` it:

- Honours the `Retry-After` header when present
- Otherwise sleeps with exponential backoff (5s -> capped at 5 min)
- Retries up to 8 times before logging the failure to `errors.log`

If you see repeated 429s, raise `--throttle` to `1.0` or higher.

## What is *not* exported

- **Comments**: not exposed by the public Nuclino API.
- **Version history**: only the current revision of each item is exported.
- **Permissions / ACLs**: not exposed by the public API.
- **Embedded interactive blocks**: rendered to whatever the API returns in the
  `content` markdown field. Some Nuclino-specific block syntax (mention chips,
  drawing canvases) may degrade.

## License

MIT, see [LICENSE](LICENSE).
