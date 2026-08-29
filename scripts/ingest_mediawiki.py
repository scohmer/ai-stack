#!/usr/bin/env python3
"""AI-Stack — bulk-ingest a MediaWiki instance's pages into an Open WebUI Knowledge base.

Open WebUI has no native MediaWiki connector, so this logs into a MediaWiki
instance's Action API (clientlogin), enumerates pages in the requested
namespace(s), pulls each page's current wikitext, uploads what survives the
size filter through Open WebUI's file API, and attaches them all to a
Knowledge base (created if it doesn't already exist) — which any chat can
then retrieve from, either per-chat (# / paperclip) or permanently via a
custom Model's Knowledge section (Workspace -> Models).

Requires: python3 with `requests` (already present on this host).

Config comes from .env (MEDIAWIKI_BASE_URL, MEDIAWIKI_USERNAME,
MEDIAWIKI_PASSWORD, MEDIAWIKI_NAMESPACES, MEDIAWIKI_KNOWLEDGE_NAME,
OPEN_WEBUI_API_KEY, OPEN_WEBUI_PORT, INGEST_MAX_FILE_SIZE_KB) with CLI flags
to override per-run. See --help.

NOTE ON AUTH: MediaWiki's git-http-style PRIVATE-TOKEN-equivalent doesn't
apply here — this always authenticates via the Action API's clientlogin
flow (username + password, or a Special:BotPasswords username@bagname +
generated password, which clientlogin accepts identically to a normal
login). Anonymous editing/reading may be enabled on a given wiki, but
clientlogin is used unconditionally here for predictable behavior across
instances with different anonymous-access configurations.

NOTE ON RE-RUNS: Open WebUI does not dedupe file uploads by content. Re-running
this against the same --knowledge-name will add duplicate copies of every page.
For a strict re-ingest, delete the existing Knowledge base first (Workspace ->
Knowledge -> ... -> Delete) or pass a distinct --knowledge-name.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print("ERROR: the 'requests' python package is required (python3 -m pip install --user requests)", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"


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


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "wiki"


def safe_filename(title: str) -> str:
    """MediaWiki titles can contain '/', ':', spaces, etc. — all fine on
    Linux filenames except '/', which would otherwise be read as a path
    separator on upload."""
    return title.replace("/", "_") + ".wiki"


class MediaWikiClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/api.php"
        self.session = requests.Session()
        self.username = username
        self.password = password

    def _api(self, **params) -> dict:
        params["format"] = "json"
        r = self.session.post(self.api_url, data=params)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"MediaWiki API error: {data['error']}")
        return data

    def login(self) -> str:
        """clientlogin with username/password (works identically for a
        regular account or a Special:BotPasswords username@bagname).
        Returns the sitename for use as a default Knowledge base label."""
        login_token = self._api(action="query", meta="tokens", type="login")["query"]["tokens"]["logintoken"]
        resp = self._api(
            action="clientlogin",
            username=self.username,
            password=self.password,
            logintoken=login_token,
            loginreturnurl=self.base_url + "/",
        )
        status = resp.get("clientlogin", {}).get("status")
        if status != "PASS":
            raise RuntimeError(f"MediaWiki login failed: {resp.get('clientlogin', resp)}")
        print(f"==> Logged into {self.base_url} as {resp['clientlogin']['username']}")

        site = self._api(action="query", meta="siteinfo", siprop="general")["query"]["general"]
        return site.get("sitename", urlparse(self.base_url).netloc)

    def iter_pages(self, namespace: int):
        """Yield (title, wikitext) for every non-redirect page in one
        namespace, paginating through MediaWiki's 'continue' mechanism."""
        params = {
            "action": "query",
            "generator": "allpages",
            "gapnamespace": namespace,
            "gaplimit": 50,
            "gapfilterredir": "nonredirects",
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
        }
        cont = {}
        while True:
            data = self._api(**params, **cont)
            pages = data.get("query", {}).get("pages", {})
            for page in pages.values():
                revisions = page.get("revisions")
                if not revisions:
                    continue  # e.g. a page with no current revision
                content = revisions[0].get("slots", {}).get("main", {}).get("*", "")
                yield page["title"], content
            if "continue" not in data:
                break
            cont = data["continue"]


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

    def upload_bytes(self, filename: str, content: bytes, source_path: str) -> str:
        r = self.session.post(
            f"{self.base_url}/api/v1/files/",
            files={"file": (filename, content)},
            data={"metadata": json.dumps({"source_path": source_path})},
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
    p.add_argument("--wiki-url", default=os.environ.get("MEDIAWIKI_BASE_URL", ""),
                   help="Base URL of the wiki, e.g. http://192.168.10.91 (script appends /api.php).")
    p.add_argument("--username", default=os.environ.get("MEDIAWIKI_USERNAME", ""))
    p.add_argument("--password", default=os.environ.get("MEDIAWIKI_PASSWORD", ""))
    p.add_argument("--namespace", action="append", type=int, default=None,
                   help="Namespace ID to ingest (repeatable). Default: MEDIAWIKI_NAMESPACES "
                        "from .env (comma-separated), or just 0 (Main) if unset.")
    p.add_argument("--knowledge-name", default=os.environ.get("MEDIAWIKI_KNOWLEDGE_NAME", ""))
    p.add_argument("--max-file-size-kb", type=int,
                    default=int(os.environ.get("INGEST_MAX_FILE_SIZE_KB", "1024")))
    p.add_argument("--base-url", default=f"http://localhost:{os.environ.get('OPEN_WEBUI_PORT', '3000')}",
                   help="Open WebUI base URL (not the wiki's).")
    p.add_argument("--api-key", default=os.environ.get("OPEN_WEBUI_API_KEY", ""))
    p.add_argument("--dry-run", action="store_true", help="List what would be ingested; don't call Open WebUI.")
    args = p.parse_args()

    if not args.wiki_url:
        p.error("Provide --wiki-url (or set MEDIAWIKI_BASE_URL in .env).")
    if not args.username or not args.password:
        p.error("Provide --username/--password (or set MEDIAWIKI_USERNAME/MEDIAWIKI_PASSWORD in .env).")
    if not args.dry_run and not args.api_key:
        p.error("Provide --api-key (or set OPEN_WEBUI_API_KEY in .env) — required unless --dry-run.")

    if args.namespace:
        namespaces = args.namespace
    else:
        env_ns = os.environ.get("MEDIAWIKI_NAMESPACES", "").strip()
        namespaces = [int(n) for n in env_ns.split(",") if n.strip()] if env_ns else [0]

    wiki = MediaWikiClient(args.wiki_url, args.username, args.password)
    sitename = wiki.login()

    included = []  # list of (title, content_bytes, size_kb)
    skipped_empty = 0
    skipped_size = 0
    for ns in namespaces:
        print(f"==> Enumerating namespace {ns}")
        for title, content in wiki.iter_pages(ns):
            if not content.strip():
                skipped_empty += 1
                continue
            content_bytes = content.encode("utf-8")
            size_kb = len(content_bytes) / 1024
            if size_kb > args.max_file_size_kb:
                skipped_size += 1
                continue
            included.append((title, content_bytes, size_kb))

    total_kb = sum(size for _, _, size in included)
    print(f"==> {len(included)} pages to ingest ({total_kb:.0f} KB) — "
          f"skipped {skipped_empty} empty, {skipped_size} oversized")

    if args.dry_run:
        print("\n--dry-run: not uploading. Sample of included pages:")
        for title, _, size_kb in included[:40]:
            print(f"  {title} ({size_kb:.1f} KB)")
        if len(included) > 40:
            print(f"  ... and {len(included) - 40} more")
        return

    if not included:
        print("Nothing to ingest — check your namespace/size cap.", file=sys.stderr)
        sys.exit(1)

    name = args.knowledge_name or f"mediawiki-{slugify(sitename)}"
    client = OpenWebUIClient(args.base_url, args.api_key)
    knowledge_id = client.find_or_create_knowledge(
        name, f"Ingested from {args.wiki_url} (namespaces={namespaces})"
    )

    file_ids = []
    for i, (title, content_bytes, _) in enumerate(included, 1):
        fid = client.upload_bytes(safe_filename(title), content_bytes, source_path=title)
        file_ids.append(fid)
        if i % 25 == 0 or i == len(included):
            print(f"    uploaded {i}/{len(included)}")

    print(f"==> Attaching {len(file_ids)} files to Knowledge base '{name}'")
    client.batch_add_to_knowledge(knowledge_id, file_ids)

    print(f"\nDone. Knowledge base '{name}' (id={knowledge_id}) has {len(file_ids)} pages.")
    print("Open WebUI processes/embeds files in the background — give it a bit before querying.")
    print("Attach it in a chat via # / paperclip, or permanently via Workspace -> Models -> (your model) -> Knowledge.")


if __name__ == "__main__":
    main()
