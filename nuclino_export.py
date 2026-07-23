#!/usr/bin/env python3
"""Export an entire Nuclino team (all workspaces, items, attachments) to disk.

Resumable: re-running with the same --output skips already-saved items and files.
Stdlib only; works on Python 3.10+.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API = "https://api.nuclino.com/v0"

# --- runtime state -----------------------------------------------------------
_TOKEN: str = ""
_THROTTLE: float = 0.5
_OUTPUT: Path = Path()
_ERRORS_PATH: Path = Path()
_last_request_time: float = 0.0


def log_err(msg: str) -> None:
    with _ERRORS_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    print(f"ERR: {msg}", file=sys.stderr, flush=True)


def slugify(name: str, max_len: int = 60) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return (s or "unnamed")[:max_len]


def safe_filename(name: str) -> str:
    s = name.replace("/", "_").replace("\\", "_").strip()
    return s or "unnamed"


def short_id(full_id: str) -> str:
    """First UUID segment (8 hex chars) for short, human-tolerable filenames."""
    return full_id.split("-", 1)[0] if full_id else "noid"


def item_basename(title: str, full_id: str) -> str:
    """Base filename (without extension) for an item or collection."""
    return f"{slugify(title)}__{short_id(full_id)}"


def attachment_filename(original_name: str, full_id: str) -> str:
    """Filename for an attachment: <stem>__<short-id>.<suffix>."""
    name = safe_filename(original_name or full_id)
    sid = short_id(full_id)
    if "." in name:
        stem, suffix = name.rsplit(".", 1)
        return f"{stem}__{sid}.{suffix}"
    return f"{name}__{sid}"


def find_existing(items_dir: Path, full_id: str, ext: str) -> Path | None:
    """Find a previously-saved item file by short-id glob, regardless of slug."""
    sid = short_id(full_id)
    matches = list(items_dir.glob(f"*__{sid}.{ext}"))
    return matches[0] if matches else None


def workspace_dirname(ws: dict, all_workspaces: list[dict]) -> str:
    """Resolve a collision-safe directory name for a workspace."""
    slug = slugify(ws["name"])
    siblings = [w for w in all_workspaces if slugify(w["name"]) == slug]
    if len(siblings) > 1:
        return f"{slug}__{short_id(ws['id'])}"
    return slug


# --- format parsing ---------------------------------------------------------

VALID_FORMATS = {"md", "json", "docx"}


def parse_formats(spec: str) -> set[str]:
    """Parse a comma-separated format spec into a set of canonical names.

    Accepts 'md', 'json', 'docx'.
    """
    tokens = [t.strip().lower() for t in spec.split(",") if t.strip()]
    if not tokens:
        raise SystemExit("--format cannot be empty")
    out: set[str] = set()
    for t in tokens:
        if t not in VALID_FORMATS:
            raise SystemExit(
                f"Unknown format '{t}'. Choose from: "
                f"{', '.join(sorted(VALID_FORMATS))} (or comma-separated combos)."
            )
        out.add(t)
    return out


# --- link rewriting ---------------------------------------------------------

_UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Any reference to a Nuclino-hosted file: image embeds (`![alt](url)`) and plain
# links (`[name](url)`) alike, with the URL optionally wrapped in <> (Nuclino does
# this when the filename contains spaces). The file's UUID is captured so we can
# match it to the locally-downloaded attachment by short-id.
_FILE_LINK_RE = re.compile(
    r"(!?)\[([^\]]*)\]\(<?https://files\.nuclino\.com/files/"
    rf"({_UUID_PATTERN})/[^)>]*>?\)"
)
_USER_LINK_RE = re.compile(
    r"\[([^\]]+)\]\(https://app\.nuclino\.com/users/[^)]+\)"
)
_ITEM_TB_LINK_RE = re.compile(
    rf"\[([^\]]+)\]\(https://app\.nuclino\.com/t/b/({_UUID_PATTERN})[^)]*\)"
)
_ITEM_TEAM_LINK_RE = re.compile(
    rf"\[([^\]]+)\]\(https://app\.nuclino\.com/[^/]+/[^/]+/[^)]*-({_UUID_PATTERN})[^)]*\)"
)


def rewrite_links(
    content: str,
    *,
    item_index: dict,
    this_ws_dir: str,
    file_by_shortid: dict,
    target_ext: str,
) -> str:
    """Rewrite Nuclino URLs to local references.

    - File references (both image embeds `![alt](url)` and plain links
      `[name](url)`): rewritten to `../files/<localname>` when the file's UUID
      matches a locally-downloaded attachment (by short-id). Files that weren't
      downloaded (e.g. under --skip-files) are left untouched.
    - User mention links: the URL is dropped, only the display name remains.
    - Cross-doc references (`/t/b/<uuid>` and team-path forms): rewritten to a
      relative path to the target item's local file with `target_ext`.
      Unresolvable references (UUID not in the index) keep only the link text.
    """
    def file_repl(m: re.Match) -> str:
        bang, text, uuid = m.group(1), m.group(2), m.group(3)
        local = file_by_shortid.get(short_id(uuid))
        if not local:
            return m.group(0)
        target = f"../files/{local}"
        # Angle-bracket the target if it contains spaces so markdown parsers
        # (and pandoc) treat the whole path as the URL.
        if " " in target:
            target = f"<{target}>"
        return f"{bang}[{text}]({target})"

    def user_repl(m: re.Match) -> str:
        return m.group(1)

    def item_repl(m: re.Match) -> str:
        text, uuid = m.group(1), m.group(2)
        info = item_index.get(uuid)
        if not info:
            return text
        if info.get("object") != "item":
            return text
        target_ws_dir = info["ws_dir"]
        target_basename = info["basename"]
        if target_ws_dir == this_ws_dir:
            target_path = f"{target_basename}{target_ext}"
        else:
            target_path = f"../../{target_ws_dir}/items/{target_basename}{target_ext}"
        return f"[{text}]({target_path})"

    content = _FILE_LINK_RE.sub(file_repl, content)
    content = _USER_LINK_RE.sub(user_repl, content)
    content = _ITEM_TB_LINK_RE.sub(item_repl, content)
    content = _ITEM_TEAM_LINK_RE.sub(item_repl, content)
    return content


# --- docx output ------------------------------------------------------------

def write_docx(md_source: Path, docx_path: Path) -> None:
    """Convert a markdown file to .docx using pandoc, with --resource-path set
    so relative image references like `../files/foo.png` resolve correctly.
    """
    resource_path = str(md_source.parent.resolve())
    subprocess.run(
        [
            "pandoc",
            "--from", "markdown+yaml_metadata_block",
            "--to", "docx",
            f"--resource-path={resource_path}",
            "--output", str(docx_path),
            str(md_source),
        ],
        check=True,
    )


def require_pandoc() -> None:
    if shutil.which("pandoc") is None:
        raise SystemExit(
            "pandoc not found on PATH. Install pandoc to use --format docx, "
            "or drop docx from --format."
        )


def throttle() -> None:
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < _THROTTLE:
        time.sleep(_THROTTLE - elapsed)
    _last_request_time = time.time()


def api_get(path: str, params: dict | None = None, max_retries: int = 8) -> dict:
    url = f"{API}{path}"
    if params:
        url += "?" + urlencode(params)
    backoff = 5.0
    last_err: Exception | None = None
    for attempt in range(max_retries):
        throttle()
        req = Request(url, headers={"Authorization": _TOKEN})
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                last_err = e
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = backoff
                if retry_after:
                    try:
                        wait = max(float(retry_after), backoff)
                    except ValueError:
                        pass
                wait = min(wait, 300.0)
                print(f"  rate-limited (HTTP {e.code}), sleeping {wait:.1f}s...", flush=True)
                time.sleep(wait)
                backoff = min(backoff * 2, 300.0)
                continue
            raise
        except URLError as e:
            last_err = e
            time.sleep(backoff)
            backoff = min(backoff * 2, 300.0)
    raise RuntimeError(f"GET {path} failed after retries: {last_err}")


def download_url(url: str, dest: Path, max_retries: int = 5) -> None:
    backoff = 2.0
    for attempt in range(max_retries):
        try:
            with urlopen(url, timeout=120) as resp:
                data = resp.read()
            dest.write_bytes(data)
            return
        except (HTTPError, URLError):
            if attempt == max_retries - 1:
                raise
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def yaml_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_markdown(item: dict, content: str, workspace_name: str) -> str:
    """Render an item to markdown text (frontmatter + body) using the
    supplied (already-rewritten) content body.
    """
    title = item.get("title", "") or ""
    meta = item.get("contentMeta") or {}
    file_ids = meta.get("fileIds") or []
    child_ids = meta.get("itemIds") or []
    fields = item.get("fields") or {}

    fm = ["---"]
    fm.append(f"nuclino_id: {item.get('id', '')}")
    fm.append(f"title: {yaml_quote(title)}")
    fm.append(f"workspace: {yaml_quote(workspace_name)}")
    fm.append(f"workspace_id: {item.get('workspaceId', '')}")
    fm.append(f"url: {yaml_quote(item.get('url', ''))}")
    fm.append(f"created: {item.get('createdAt', '')}")
    fm.append(f"updated: {item.get('lastUpdatedAt', '')}")
    fm.append(f"exported: {time.strftime('%Y-%m-%d')}")
    if file_ids:
        fm.append("file_ids:")
        for fid in file_ids:
            fm.append(f"  - {fid}")
    if child_ids:
        fm.append("child_item_ids:")
        for cid in child_ids:
            fm.append(f"  - {cid}")
    if fields:
        fm.append("fields:")
        for k, v in fields.items():
            fm.append(f"  {k}: {yaml_quote(str(v))}")
    fm.append("---")
    fm.append("")
    body = content.lstrip()
    if not body.startswith("#"):
        fm.append(f"# {title}")
        fm.append("")
    fm.append(content)
    return "\n".join(fm)


def list_workspace_items(ws_id: str) -> list[dict]:
    """Paginate through all items/collections in a workspace, returning the
    raw list entries (no per-item GET)."""
    entries: list[dict] = []
    after: str | None = None
    while True:
        params: dict = {"workspaceId": ws_id, "limit": 100}
        if after:
            params["after"] = after
        resp = api_get("/items", params)
        results = resp.get("data", {}).get("results", [])
        if not results:
            break
        entries.extend(results)
        if len(results) < 100:
            break
        after = results[-1]["id"]
    return entries


def discover(teams: list[dict], workspace_filter: set[str]
             ) -> tuple[dict, dict, list[tuple[dict, dict, list[dict]]]]:
    """Discovery pass: enumerate all items across the (filtered) workspaces.

    Returns:
      item_index: item_id -> {ws_dir, ws_name, title, basename, object}
      ws_dir_by_id: ws_id -> resolved directory name (slug or slug__shortid)
      plan: list of (team, workspace, entries) tuples in iteration order
    """
    item_index: dict = {}
    ws_dir_by_id: dict = {}
    plan: list[tuple[dict, dict, list[dict]]] = []
    for team in teams:
        ws_list = api_get("/workspaces", {"teamId": team["id"]})["data"]["results"]
        for ws in ws_list:
            if workspace_filter and ws["id"] not in workspace_filter:
                continue
            ws_dir = workspace_dirname(ws, ws_list)
            ws_dir_by_id[ws["id"]] = ws_dir
            print(f"  listing {ws['name']} ...", flush=True)
            entries = list_workspace_items(ws["id"])
            for e in entries:
                item_index[e["id"]] = {
                    "ws_id": ws["id"],
                    "ws_dir": ws_dir,
                    "ws_name": ws["name"],
                    "title": e.get("title", "") or "",
                    "basename": item_basename(e.get("title", "") or "", e["id"]),
                    "object": e.get("object"),
                }
            plan.append((team, ws, entries))
    return item_index, ws_dir_by_id, plan


def export_workspace(ws: dict, ws_dir_name: str, entries: list[dict],
                     formats: set[str], download_files: bool,
                     item_index: dict, counters: dict) -> dict:
    ws_id = ws["id"]
    ws_name = ws["name"]
    ws_dir = _OUTPUT / "workspaces" / ws_dir_name
    items_dir = ws_dir / "items"
    files_dir = ws_dir / "files"
    items_dir.mkdir(parents=True, exist_ok=True)
    if download_files:
        files_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "_workspace.json").write_text(
        json.dumps(ws, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    summary = {"workspace": ws_name, "items": 0, "collections": 0,
               "files": 0, "docx": 0, "errors": 0}
    write_json = "json" in formats
    write_md = "md" in formats
    write_docx_fmt = "docx" in formats
    # Cumulative file-short-id -> local-attachment-filename map for this
    # workspace. Built incrementally as we download files; used to rewrite both
    # image embeds and plain file links to their local copies. Keyed by short-id
    # (first UUID segment) because it's collision-free, unlike the filename
    # (many attachments are literally named "image.png").
    shortid_to_local: dict = {}
    # Also populate the map from any existing on-disk attachments so resumes
    # can rewrite without re-fetching file metadata.
    if files_dir.exists():
        for p in files_dir.iterdir():
            if not p.is_file() or "__" not in p.name:
                continue
            # Filename is "<stem>__<short-id>[.<ext>]"; take the last __ segment.
            _, _, sid_ext = p.name.rpartition("__")
            sid = sid_ext.split(".", 1)[0]
            if sid:
                shortid_to_local[sid] = p.name

    print(f"  [{ws_name}] processing {len(entries)} entries", flush=True)

    for entry in entries:
        obj_type = entry.get("object")
        entry_id = entry.get("id")
        entry_title = entry.get("title", "") or ""
        base = item_basename(entry_title, entry_id)
        json_path = items_dir / f"{base}.json"
        md_path = items_dir / f"{base}.md"
        docx_path = items_dir / f"{base}.docx"
        # If the title changed since last export, reuse the existing file
        # found by short-id rather than creating a duplicate under the new slug.
        existing_json = find_existing(items_dir, entry_id, "json")
        existing_md = find_existing(items_dir, entry_id, "md")
        existing_docx = find_existing(items_dir, entry_id, "docx")
        if existing_json:
            json_path = existing_json
        if existing_md:
            md_path = existing_md
        if existing_docx:
            docx_path = existing_docx

        if obj_type == "item":
            json_ready = json_path.exists()
            md_ready = md_path.exists()
            docx_ready = docx_path.exists()
            # We need the item content body if any output is missing or if we
            # want files downloaded (the file list lives in contentMeta).
            need_content = (
                (write_json and not json_ready)
                or (write_md and not md_ready)
                or (write_docx_fmt and not docx_ready)
                or download_files
            )

            full: dict | None = None
            if need_content:
                if json_ready:
                    try:
                        full = json.loads(json_path.read_text(encoding="utf-8"))
                        counters["items_skipped"] += 1
                    except Exception as e:
                        log_err(f"[{ws_name}] read json {entry_id}: {e}")
                        summary["errors"] += 1
                        continue
                else:
                    try:
                        full = api_get(f"/items/{entry_id}")["data"]
                    except Exception as e:
                        log_err(f"[{ws_name}] get item {entry_id}: {e}")
                        summary["errors"] += 1
                        continue
                    if write_json:
                        json_path.write_text(
                            json.dumps(full, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                    counters["items_fetched"] += 1
            else:
                counters["items_skipped"] += 1
            summary["items"] += 1

            # Download attachments and accumulate the URL-filename map.
            if download_files:
                if full is None and json_path.exists():
                    try:
                        full = json.loads(json_path.read_text(encoding="utf-8"))
                    except Exception:
                        full = None
                if full:
                    file_ids = (full.get("contentMeta") or {}).get("fileIds") or []
                    for fid in file_ids:
                        existing = list(files_dir.glob(f"*__{short_id(fid)}.*"))
                        existing += list(files_dir.glob(f"*__{short_id(fid)}"))
                        if existing:
                            summary["files"] += 1
                            counters["files_skipped"] += 1
                            continue
                        try:
                            meta = api_get(f"/files/{fid}")["data"]
                        except Exception as e:
                            log_err(f"[{ws_name}] file meta {fid}: {e}")
                            summary["errors"] += 1
                            continue
                        url = (meta.get("download") or {}).get("url")
                        fname = meta.get("fileName") or fid
                        local = attachment_filename(fname, fid)
                        dest = files_dir / local
                        if not url:
                            log_err(f"[{ws_name}] file {fid}: no download url")
                            summary["errors"] += 1
                            continue
                        try:
                            download_url(url, dest)
                            shortid_to_local[short_id(fid)] = local
                            summary["files"] += 1
                            counters["files_fetched"] += 1
                        except Exception as e:
                            log_err(f"[{ws_name}] download {fid} ({fname}): {e}")
                            summary["errors"] += 1

            # Markdown / docx output (with link rewriting).
            need_md_write = write_md and not md_ready
            need_docx_write = write_docx_fmt and not docx_ready
            if (need_md_write or need_docx_write) and full is None:
                # Should already be loaded above; defensive reload.
                if json_path.exists():
                    try:
                        full = json.loads(json_path.read_text(encoding="utf-8"))
                    except Exception:
                        full = None
            if (need_md_write or need_docx_write) and full:
                raw_content = full.get("content", "") or ""
                if need_md_write:
                    md_content = rewrite_links(
                        raw_content,
                        item_index=item_index,
                        this_ws_dir=ws_dir_name,
                        file_by_shortid=shortid_to_local,
                        target_ext=".md",
                    )
                    try:
                        md_path.write_text(
                            render_markdown(full, md_content, ws_name),
                            encoding="utf-8",
                        )
                    except Exception as e:
                        log_err(f"[{ws_name}] write md {entry_id}: {e}")
                        summary["errors"] += 1
                if need_docx_write:
                    docx_content = rewrite_links(
                        raw_content,
                        item_index=item_index,
                        this_ws_dir=ws_dir_name,
                        file_by_shortid=shortid_to_local,
                        target_ext=".docx",
                    )
                    # Pandoc reads from a file so it can resolve --resource-path
                    # for inline images; use a temp markdown file next to the
                    # target so relative `../files/...` references work.
                    tmp_md = items_dir / f".{base}.docx-source.md"
                    try:
                        tmp_md.write_text(
                            render_markdown(full, docx_content, ws_name),
                            encoding="utf-8",
                        )
                        write_docx(tmp_md, docx_path)
                        summary["docx"] += 1
                        counters["docx_written"] += 1
                    except Exception as e:
                        log_err(f"[{ws_name}] write docx {entry_id}: {e}")
                        summary["errors"] += 1
                    finally:
                        if tmp_md.exists():
                            tmp_md.unlink()

        elif obj_type == "collection":
            if write_json and not json_path.exists():
                json_path.write_text(
                    json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            summary["collections"] += 1
        else:
            log_err(f"[{ws_name}] unknown object type: {obj_type} id={entry_id}")

    (ws_dir / "_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return summary


def resolve_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token.strip()
    env = os.environ.get("NUCLINO_API_TOKEN")
    if env:
        return env.strip()
    token_file = Path(args.token_file).expanduser()
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    raise SystemExit(
        f"No API token found. Provide --token, set NUCLINO_API_TOKEN, "
        f"or create {token_file} (chmod 600)."
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="nuclino-export",
        description="Export Nuclino team content (items + attachments) to disk.",
    )
    p.add_argument(
        "--output", "-o", required=True, type=Path,
        help="Output directory (created if missing). Re-runs into the same dir resume.",
    )
    p.add_argument(
        "--token", help="Nuclino API token (overrides env / file).",
    )
    p.add_argument(
        "--token-file", default="~/.config/nuclino/token",
        help="Path to file containing the API token (default: %(default)s).",
    )
    p.add_argument(
        "--workspace", action="append", default=[], metavar="ID",
        help="Limit to specific workspace ID(s). Repeatable. Default: all workspaces.",
    )
    p.add_argument(
        "--format", default="md,json", metavar="FORMATS",
        help="Comma-separated output formats per item: any of 'md', 'json', "
             "'docx'. Default: %(default)s. 'docx' requires pandoc on PATH.",
    )
    p.add_argument(
        "--skip-files", action="store_true",
        help="Skip downloading file attachments (faster, smaller export).",
    )
    p.add_argument(
        "--throttle", type=float, default=0.5, metavar="SECONDS",
        help="Min seconds between API requests (default: %(default)s).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global _TOKEN, _THROTTLE, _OUTPUT, _ERRORS_PATH
    args = parse_args(sys.argv[1:] if argv is None else argv)

    _TOKEN = resolve_token(args)
    _THROTTLE = max(0.0, args.throttle)
    _OUTPUT = args.output.expanduser().resolve()
    _OUTPUT.mkdir(parents=True, exist_ok=True)
    _ERRORS_PATH = _OUTPUT / "errors.log"

    formats = parse_formats(args.format)
    if "docx" in formats:
        require_pandoc()
    download_files = not args.skip_files
    workspace_filter = set(args.workspace)

    start = time.time()
    print(f"Output: {_OUTPUT}", flush=True)
    print(f"Formats: {','.join(sorted(formats))}", flush=True)
    print("== Listing teams ==", flush=True)
    teams = api_get("/teams")["data"]["results"]
    if not teams:
        print("No teams accessible with this token.", file=sys.stderr)
        return 1

    print("\n== Discovery: indexing all items for cross-reference resolution ==",
          flush=True)
    item_index, ws_dir_by_id, plan = discover(teams, workspace_filter)
    (_OUTPUT / "_index.json").write_text(
        json.dumps(item_index, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  indexed {len(item_index)} items across {len(plan)} workspaces",
          flush=True)

    manifest = {
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tool_version": "0.2.0",
        "options": {
            "format": sorted(formats),
            "download_files": download_files,
            "throttle": _THROTTLE,
            "workspace_filter": sorted(workspace_filter) or None,
        },
        "teams": [],
    }
    counters = {
        "items_fetched": 0, "items_skipped": 0,
        "files_fetched": 0, "files_skipped": 0,
        "docx_written": 0,
    }
    team_summaries: dict = {}

    for team, ws, entries in plan:
        team_id = team["id"]
        team_name = team["name"]
        if team_id not in team_summaries:
            print(f"\n== Team: {team_name} ({team_id}) ==", flush=True)
            team_summaries[team_id] = {"team": team_name, "team_id": team_id,
                                        "workspaces": []}
        print(f"\n-- Workspace: {ws['name']} --", flush=True)
        s = export_workspace(
            ws, ws_dir_by_id[ws["id"]], entries, formats, download_files,
            item_index, counters,
        )
        team_summaries[team_id]["workspaces"].append(s)
    manifest["teams"] = list(team_summaries.values())

    manifest["counters"] = counters
    manifest["duration_seconds"] = round(time.time() - start, 1)
    (_OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("\n== Done ==", flush=True)
    print(json.dumps(counters, indent=2), flush=True)
    print(f"Duration: {manifest['duration_seconds']}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
