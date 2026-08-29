#!/usr/bin/env python3
"""AI-Stack — bulk-ingest a git repo's source tree into an Open WebUI Knowledge base.

Open WebUI has no native git/GitLab connector, so this clones a repo, walks its
working tree with sensible source/doc-only filtering, uploads each surviving
file through Open WebUI's file API, and attaches them all to a Knowledge base
(created if it doesn't already exist) — which any chat can then retrieve from,
either per-chat (# / paperclip) or permanently via a custom Model's Knowledge
section (Workspace -> Models).

Requires: git, python3 with `requests` (already present on this host).

Config comes from .env (GITLAB_REPO_URL, GITLAB_ACCESS_TOKEN, OPEN_WEBUI_API_KEY,
OPEN_WEBUI_PORT, INGEST_*) with CLI flags to override per-run. See --help.

NOTE ON RE-RUNS: Open WebUI does not dedupe file uploads by content. Re-running
this against the same --knowledge-name will add duplicate copies of every file.
For a strict re-ingest, delete the existing Knowledge base first (Workspace ->
Knowledge -> ... -> Delete) or pass a distinct --knowledge-name (e.g. including
the commit SHA, which this script's default naming does when --branch resolves
to a specific commit).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print("ERROR: the 'requests' python package is required (python3 -m pip install --user requests)", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

DEFAULT_EXCLUDE_DIRS = {
    ".git", "node_modules", "vendor", "venv", ".venv", "env", "dist", "build",
    "target", "__pycache__", ".idea", ".vscode", ".next", ".terraform",
    "coverage", "bin", "obj", ".mypy_cache", ".pytest_cache", ".tox",
}
DEFAULT_EXCLUDE_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "ico", "svg", "webp", "bmp",
    "woff", "woff2", "ttf", "eot", "otf",
    "pdf", "zip", "tar", "gz", "bz2", "7z", "rar",
    "bin", "exe", "dll", "so", "dylib", "class", "jar", "pyc", "o", "a",
    "lock",
}
DEFAULT_EXCLUDE_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
    "poetry.lock", "Pipfile.lock", "composer.lock", "go.sum",
}


def load_env_file(path: Path) -> None:
    """Populate os.environ from a KEY=VALUE .env file, without overriding
    anything already set in the real environment."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def is_probably_binary(path: Path, sniff_bytes: int = 8192) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(sniff_bytes)
    except OSError:
        return True
    return b"\x00" in chunk


def clone_repo(repo_url: str, token: str, branch: str | None, dest: Path) -> str:
    """Shallow-clone repo_url into dest using an HTTP Basic Authorization
    header (avoids embedding the token in the URL / .git/config, where it'd
    otherwise land in .git/config in plaintext and in shell/process history).

    NOTE: this is deliberately *not* GitLab's PRIVATE-TOKEN header. That
    header is a REST/GraphQL API mechanism only — GitLab's git-over-HTTP
    (smart HTTP) backend authenticates exclusively via HTTP Basic, for every
    token type (personal, project, group, deploy). Confirmed against a real
    instance: PRIVATE-TOKEN header -> 401 on /info/refs, HTTP Basic -> 200.
    The username is conventionally "oauth2" (GitLab ignores its actual
    value for token auth — only the password/token matters)."""
    cmd = ["git"]
    if token:
        basic = base64.b64encode(f"oauth2:{token}".encode()).decode()
        cmd += ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
    cmd += ["clone", "--depth", "1"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [repo_url, str(dest)]
    print(f"==> Cloning {repo_url} (branch={branch or 'default'}) -> {dest}")
    subprocess.run(cmd, check=True)
    sha = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "--short", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return sha


def list_candidate_files(root: Path, exclude_dirs: set):
    """Tracked files only, via `git ls-files`, when root is a git repo — this
    automatically respects the repo's own .gitignore and never touches
    untracked files (important for --local-path: a working directory can have
    untracked secrets sitting next to tracked code). Falls back to a plain
    filesystem walk otherwise."""
    is_git = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    ).returncode == 0
    if is_git:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True, capture_output=True,
        ).stdout
        rels = [Path(p) for p in out.decode("utf-8", "surrogateescape").split("\0") if p]
    else:
        rels = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
            for name in filenames:
                rels.append((Path(dirpath) / name).relative_to(root))
    # git ls-files doesn't know about our exclude_dirs list — still filter it.
    return [r for r in rels if not (set(r.parts) & exclude_dirs)]


def collect_files(root: Path, exclude_dirs: set, exclude_exts: set,
                   exclude_names: set, max_size_kb: int):
    included, skipped_binary, skipped_size, skipped_excluded = [], [], [], []
    for rel in list_candidate_files(root, exclude_dirs):
        fp = root / rel
        if not fp.is_file():
            continue
        ext = fp.suffix.lstrip(".").lower()
        if fp.name in exclude_names or ext in exclude_exts:
            skipped_excluded.append(rel)
            continue
        size_kb = fp.stat().st_size / 1024
        if size_kb > max_size_kb:
            skipped_size.append(rel)
            continue
        if is_probably_binary(fp):
            skipped_binary.append(rel)
            continue
        included.append(fp)
    return included, skipped_binary, skipped_size, skipped_excluded


class OpenWebUIClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"

    def find_or_create_knowledge(self, name: str, description: str) -> str:
        r = self.session.get(f"{self.base_url}/api/v1/knowledge/")
        r.raise_for_status()
        for item in r.json()["items"]:
            if item.get("name") == name:
                print(f"==> Reusing existing Knowledge base '{name}' (id={item['id']})")
                return item["id"]
        r = self.session.post(
            f"{self.base_url}/api/v1/knowledge/create",
            json={"name": name, "description": description},
        )
        r.raise_for_status()
        kid = r.json()["id"]
        print(f"==> Created Knowledge base '{name}' (id={kid})")
        return kid

    def upload_file(self, path: Path, rel_path: str) -> str:
        with path.open("rb") as f:
            r = self.session.post(
                f"{self.base_url}/api/v1/files/",
                files={"file": (rel_path, f)},
                data={"metadata": json.dumps({"source_path": rel_path})},
            )
        r.raise_for_status()
        return r.json()["id"]

    def batch_add_to_knowledge(self, knowledge_id: str, file_ids: list, batch_size: int = 50):
        for i in range(0, len(file_ids), batch_size):
            chunk = file_ids[i:i + batch_size]
            r = self.session.post(
                f"{self.base_url}/api/v1/knowledge/{knowledge_id}/files/batch/add",
                json=[{"file_id": fid} for fid in chunk],
            )
            r.raise_for_status()
            print(f"    attached {i + len(chunk)}/{len(file_ids)} files")


def main():
    load_env_file(ENV_FILE)

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo-url", default=os.environ.get("GITLAB_REPO_URL", ""))
    p.add_argument("--token", default=os.environ.get("GITLAB_ACCESS_TOKEN", ""))
    p.add_argument("--branch", default=os.environ.get("GITLAB_BRANCH", "") or None)
    p.add_argument("--local-path", default="", help="Skip cloning; ingest an already-checked-out directory instead.")
    p.add_argument("--knowledge-name", default=os.environ.get("INGEST_KNOWLEDGE_NAME", ""))
    p.add_argument("--max-file-size-kb", type=int,
                    default=int(os.environ.get("INGEST_MAX_FILE_SIZE_KB", "1024")))
    p.add_argument("--exclude-dir", action="append", default=[],
                    help="Additional directory name to exclude (repeatable).")
    p.add_argument("--base-url", default=f"http://localhost:{os.environ.get('OPEN_WEBUI_PORT', '3000')}")
    p.add_argument("--api-key", default=os.environ.get("OPEN_WEBUI_API_KEY", ""))
    p.add_argument("--dry-run", action="store_true", help="List what would be ingested; don't call Open WebUI.")
    p.add_argument("--keep-clone", action="store_true", help="Don't delete the temp clone directory afterward.")
    args = p.parse_args()

    if not args.local_path and not args.repo_url:
        p.error("Provide --repo-url (or set GITLAB_REPO_URL in .env) or --local-path.")
    if not args.dry_run and not args.api_key:
        p.error("Provide --api-key (or set OPEN_WEBUI_API_KEY in .env) — required unless --dry-run.")

    exclude_dirs = set(DEFAULT_EXCLUDE_DIRS) | set(args.exclude_dir)
    exclude_exts = set(DEFAULT_EXCLUDE_EXTENSIONS)
    exclude_names = set(DEFAULT_EXCLUDE_FILENAMES)

    tmp_ctx = None
    sha = None
    if args.local_path:
        root = Path(args.local_path).resolve()
        repo_label = root.name
    else:
        tmp_ctx = tempfile.mkdtemp(prefix="ai-stack-ingest-")
        root = Path(tmp_ctx) / "repo"
        sha = clone_repo(args.repo_url, args.token, args.branch, root)
        repo_label = Path(urlparse(args.repo_url).path).stem

    try:
        included, skip_bin, skip_size, skip_ex = collect_files(
            root, exclude_dirs, exclude_exts, exclude_names, args.max_file_size_kb
        )
        total_kb = sum(f.stat().st_size for f in included) / 1024
        print(f"==> {len(included)} files to ingest ({total_kb:.0f} KB) — "
              f"skipped {len(skip_bin)} binary, {len(skip_size)} oversized, {len(skip_ex)} excluded")

        if args.dry_run:
            print("\n--dry-run: not uploading. Sample of included files:")
            for f in included[:40]:
                print(f"  {f.relative_to(root)}")
            if len(included) > 40:
                print(f"  ... and {len(included) - 40} more")
            return

        if not included:
            print("Nothing to ingest — check your excludes/size cap.", file=sys.stderr)
            sys.exit(1)

        name = args.knowledge_name or f"{repo_label}" + (f"@{sha}" if sha else "")
        client = OpenWebUIClient(args.base_url, args.api_key)
        knowledge_id = client.find_or_create_knowledge(
            name, f"Ingested from {args.repo_url or args.local_path}" + (f" @ {sha}" if sha else "")
        )

        file_ids = []
        for i, f in enumerate(included, 1):
            rel = str(f.relative_to(root))
            fid = client.upload_file(f, rel)
            file_ids.append(fid)
            if i % 25 == 0 or i == len(included):
                print(f"    uploaded {i}/{len(included)}")

        print(f"==> Attaching {len(file_ids)} files to Knowledge base '{name}'")
        client.batch_add_to_knowledge(knowledge_id, file_ids)

        print(f"\nDone. Knowledge base '{name}' (id={knowledge_id}) has {len(file_ids)} files.")
        print("Open WebUI processes/embeds files in the background — give it a bit before querying.")
        print("Attach it in a chat via # / paperclip, or permanently via Workspace -> Models -> (your model) -> Knowledge.")

    finally:
        if tmp_ctx and not args.keep_clone:
            import shutil
            shutil.rmtree(tmp_ctx, ignore_errors=True)


if __name__ == "__main__":
    main()
