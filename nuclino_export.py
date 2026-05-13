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


def write_markdown(md_path: Path, item: dict, workspace_name: str) -> None:
    title = item.get("title", "") or ""
    content = item.get("content", "") or ""
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
    md_path.write_text("\n".join(fm), encoding="utf-8")


def export_workspace(ws: dict, all_workspaces: list[dict], formats: set[str],
                     download_files: bool, counters: dict) -> dict:
    ws_id = ws["id"]
    ws_name = ws["name"]
    ws_dir = _OUTPUT / "workspaces" / workspace_dirname(ws, all_workspaces)
    items_dir = ws_dir / "items"
    files_dir = ws_dir / "files"
    items_dir.mkdir(parents=True, exist_ok=True)
    if download_files:
        files_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "_workspace.json").write_text(
        json.dumps(ws, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    summary = {"workspace": ws_name, "items": 0, "collections": 0, "files": 0, "errors": 0}
    write_json = "json" in formats
    write_md = "markdown" in formats

    after: str | None = None
    page = 0
    while True:
        page += 1
        params: dict = {"workspaceId": ws_id, "limit": 100}
        if after:
            params["after"] = after
        try:
            resp = api_get("/items", params)
        except Exception as e:
            log_err(f"[{ws_name}] list page {page}: {e}")
            summary["errors"] += 1
            break
        results = resp.get("data", {}).get("results", [])
        if not results:
            break
        print(f"  [{ws_name}] page {page}: {len(results)} entries", flush=True)

        for entry in results:
            obj_type = entry.get("object")
            entry_id = entry.get("id")
            entry_title = entry.get("title", "") or ""
            base = item_basename(entry_title, entry_id)
            json_path = items_dir / f"{base}.json"
            md_path = items_dir / f"{base}.md"
            # If the title changed since last export, an old file with a
            # different slug but the same short-id may exist. Reuse it.
            existing_json = find_existing(items_dir, entry_id, "json")
            existing_md = find_existing(items_dir, entry_id, "md")
            if existing_json:
                json_path = existing_json
            if existing_md:
                md_path = existing_md

            if obj_type == "item":
                json_ready = json_path.exists()
                md_ready = md_path.exists()
                need_fetch = (write_json and not json_ready) or (write_md and not md_ready)

                full: dict | None = None
                if need_fetch:
                    try:
                        full = api_get(f"/items/{entry_id}")["data"]
                    except Exception as e:
                        log_err(f"[{ws_name}] get item {entry_id}: {e}")
                        summary["errors"] += 1
                        continue
                    if write_json and not json_ready:
                        json_path.write_text(
                            json.dumps(full, indent=2, ensure_ascii=False), encoding="utf-8"
                        )
                    if write_md and not md_ready:
                        try:
                            write_markdown(md_path, full, ws_name)
                        except Exception as e:
                            log_err(f"[{ws_name}] write md {entry_id}: {e}")
                            summary["errors"] += 1
                    counters["items_fetched"] += 1
                else:
                    counters["items_skipped"] += 1
                summary["items"] += 1

                if download_files:
                    # Reload JSON if not just fetched, to get fileIds
                    if full is None and json_path.exists():
                        try:
                            full = json.loads(json_path.read_text(encoding="utf-8"))
                        except Exception:
                            full = None
                    if not full:
                        continue
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
                        dest = files_dir / attachment_filename(fname, fid)
                        if not url:
                            log_err(f"[{ws_name}] file {fid}: no download url")
                            summary["errors"] += 1
                            continue
                        try:
                            download_url(url, dest)
                            summary["files"] += 1
                            counters["files_fetched"] += 1
                        except Exception as e:
                            log_err(f"[{ws_name}] download {fid} ({fname}): {e}")
                            summary["errors"] += 1

            elif obj_type == "collection":
                if write_json and not json_path.exists():
                    json_path.write_text(
                        json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                summary["collections"] += 1
            else:
                log_err(f"[{ws_name}] unknown object type: {obj_type} id={entry_id}")

        if len(results) < 100:
            break
        after = results[-1]["id"]

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
        "--format", choices=("both", "markdown", "json"), default="both",
        help="Per-item output format (default: %(default)s).",
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

    formats = {"markdown", "json"} if args.format == "both" else {args.format}
    download_files = not args.skip_files
    workspace_filter = set(args.workspace)

    start = time.time()
    print(f"Output: {_OUTPUT}", flush=True)
    print("== Listing teams ==", flush=True)
    teams = api_get("/teams")["data"]["results"]
    if not teams:
        print("No teams accessible with this token.", file=sys.stderr)
        return 1

    manifest = {
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tool_version": "0.1.0",
        "options": {
            "format": args.format,
            "download_files": download_files,
            "throttle": _THROTTLE,
            "workspace_filter": sorted(workspace_filter) or None,
        },
        "teams": [],
    }
    counters = {
        "items_fetched": 0, "items_skipped": 0,
        "files_fetched": 0, "files_skipped": 0,
    }

    for team in teams:
        team_id = team["id"]
        team_name = team["name"]
        print(f"\n== Team: {team_name} ({team_id}) ==", flush=True)
        ws_list = api_get("/workspaces", {"teamId": team_id})["data"]["results"]
        team_summary = {"team": team_name, "team_id": team_id, "workspaces": []}
        for ws in ws_list:
            if workspace_filter and ws["id"] not in workspace_filter:
                continue
            print(f"\n-- Workspace: {ws['name']} --", flush=True)
            s = export_workspace(ws, ws_list, formats, download_files, counters)
            team_summary["workspaces"].append(s)
        manifest["teams"].append(team_summary)

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
