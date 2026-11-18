#!/usr/bin/env python3
"""Publish approved Instagram Reels at their scheduled times.

Runs every 10 minutes on GitHub Actions (.github/workflows/post.yml). Free, because this repo is public.

The queue is one file per post in queue/<id>.json, written by the main repo's
`python scripts/ig_queue.py add --slot <slot> --yes` only after Kyle approves the post. The video is a
release asset (release "videos", <id>.mp4); the cover is covers/<id>.png.

For each post whose time is within the next 40 minutes:
  1. have Instagram fetch the video from this repo's release, and wait until it has processed it
  2. wait until the exact time, then publish it
  3. record the media id and link in its queue file
A post more than 3 hours late is marked "missed", never posted late. Any failure fails the run (GitHub emails Kyle).

Statuses: held (never posted until released) -> queued -> uploaded -> posted | failed | missed | cancelled

    python post.py                      # what the workflow runs
    python post.py --test-upload <id>   # upload one post to Instagram and wait for processing; never publishes
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

API_VERSION = "v27.0"
GRAPH = f"https://graph.facebook.com/{API_VERSION}"
ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / "queue"
RELEASE = "videos"
REPO = os.environ.get("GITHUB_REPOSITORY", "kdh69/atomastra-poster")
LEAD = dt.timedelta(minutes=40)   # start this long before the time: upload + Instagram's processing
LATE = dt.timedelta(hours=3)      # later than this, don't post: mark missed
AUTH_CODES = {102, 190, 463, 467}


class AuthError(Exception):
    pass


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def stamp() -> str:
    return now().isoformat(timespec="seconds")


def scrub(text: str) -> str:
    text = re.sub(r"EAA[A-Za-z0-9]{20,}", "<token>", str(text))
    return re.sub(r"(access_token=|OAuth )[^&\s\"']+", r"\1<hidden>", text)


def log(msg: str) -> None:
    print(f"{now():%H:%M:%S} {msg}", flush=True)


def load_tokens() -> dict[str, str]:
    out = {k: v for k, v in os.environ.items() if k.startswith("IG_PAGE_TOKEN_") and v}
    blob = os.environ.get("IG_SECRETS_JSON", "")
    if blob:
        for k, v in json.loads(blob).items():
            if k.startswith("IG_PAGE_TOKEN_") and v:
                out.setdefault(k, v)
    return out


def graph(method: str, path: str, token: str, **params) -> dict:
    url = path if path.startswith("http") else f"{GRAPH}/{path}"
    r = requests.request(method, url, params={**params, "access_token": token}, timeout=120)
    try:
        body = r.json()
    except ValueError:
        body = {"error": {"message": r.text[:300], "code": r.status_code}}
    err = body.get("error") if isinstance(body, dict) else None
    if err:
        msg = scrub(f"{err.get('message')} (code {err.get('code')}, subcode {err.get('error_subcode')}) "
                    f"{err.get('error_user_msg', '')}").strip()
        if err.get("code") in AUTH_CODES:
            raise AuthError("AUTH ERROR: the Page token is expired or revoked. Renew it in the main repo "
                            "(python scripts/ig_pull.py --setup --gh-secrets) and set it here too "
                            "(python scripts/ig_queue.py token). " + msg)
        raise RuntimeError(msg)
    return body


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check)


def save(path: Path, post: dict, message: str) -> None:
    """Write a queue file and push it, so the state survives this run (and a crash can't double-post)."""
    post["updated_at"] = stamp()
    path.write_text(json.dumps(post, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    git("add", str(path.relative_to(ROOT)))
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    git("commit", "-q", "-m", message)
    for _ in range(5):
        if git("push", "-q", check=False).returncode == 0:
            return
        git("pull", "-q", "--rebase", check=False)
        time.sleep(3)
    raise RuntimeError("could not push the queue update")


def download_video(post: dict, into: Path) -> Path:
    name = post["video_asset"]
    r = subprocess.run(["gh", "release", "download", RELEASE, "-R", REPO, "-p", name, "-D", str(into), "--clobber"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"could not download {name}: {r.stderr.strip()[:300]}")
    return into / name


def wait_processed(container: str, token: str, minutes: int = 20) -> None:
    deadline = time.time() + minutes * 60
    while True:
        s = graph("GET", container, token, fields="status_code,status")
        code = s.get("status_code")
        if code in ("FINISHED", "PUBLISHED"):
            return
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram couldn't process the video: {code} {s.get('status', '')}")
        if time.time() > deadline:
            raise RuntimeError(f"Instagram was still processing after {minutes} minutes ({code})")
        time.sleep(15)


def video_url(post: dict) -> str:
    return f"https://github.com/{REPO}/releases/download/{RELEASE}/{post['video_asset']}"


def upload(post: dict, token: str, with_cover: bool = True) -> str:
    """Instagram fetches the video from this repo's public release (tested 18 Nov 2026: processed in ~40 s).
    The resumable upload returned 400 ProcessingFailedError even though the container then finished, so
    it isn't used. No AI label: is_ai_generated is left unset (Kyle: AI label off)."""
    params = {"media_type": "REELS", "video_url": video_url(post), "caption": post["caption"],
              "share_to_feed": "true" if post.get("share_to_feed", True) else "false"}
    if with_cover and post.get("cover_url"):
        params["cover_url"] = post["cover_url"]
    if post.get("thumb_offset_ms") is not None:
        params["thumb_offset"] = str(post["thumb_offset_ms"])
    c = graph("POST", f"{post['ig_user_id']}/media", token, **params)
    log(f"{post['id']}: Instagram is fetching the video (container {c['id']}); waiting for it to process")
    try:
        wait_processed(c["id"], token)
    except RuntimeError:
        if with_cover and post.get("cover_url"):
            log(f"{post['id']}: processing failed with the cover; trying once more without it")
            return upload(post, token, with_cover=False)
        raise
    return c["id"]


def find_published(post: dict, token: str) -> dict | None:
    """After a crash between publish and save: find the Reel by its caption among the latest posts."""
    body = graph("GET", f"{post['ig_user_id']}/media", token, fields="id,caption,permalink,timestamp", limit=10)
    want = " ".join(post["caption"].split())
    return next((m for m in body.get("data", []) if " ".join((m.get("caption") or "").split()) == want), None)


def publish(path: Path, post: dict, token: str) -> None:
    status = graph("GET", post["container_id"], token, fields="status_code").get("status_code")
    if status == "PUBLISHED":
        m = find_published(post, token) or {}
        log(f"{post['id']}: already published (an earlier run); recording it")
    else:
        if status != "FINISHED":
            raise RuntimeError(f"container not ready to publish: {status} (containers expire after 24 h)")
        res = graph("POST", f"{post['ig_user_id']}/media_publish", token, creation_id=post["container_id"])
        m = graph("GET", res["id"], token, fields="id,permalink,timestamp")
    post.update(status="posted", media_id=m.get("id"), permalink=m.get("permalink"),
                posted_at=m.get("timestamp") or stamp(), error=None)
    save(path, post, f"Posted {post['id']}")
    log(f"{post['id']}: POSTED {m.get('permalink', '')}")
    # the video is on Instagram now; don't keep a public copy here
    subprocess.run(["gh", "release", "delete-asset", RELEASE, post["video_asset"], "-R", REPO, "--yes"],
                   capture_output=True, text=True)


def handle(path: Path, post: dict, tokens: dict[str, str]) -> None:
    when = dt.datetime.fromisoformat(post["publish_at"])
    token = tokens.get(post["token_secret"])
    if not token:
        raise RuntimeError(f"secret {post['token_secret']} is not set in this repo")
    if post["status"] == "queued":
        post["container_id"] = upload(post, token)
        post["status"] = "uploaded"
        save(path, post, f"Uploaded {post['id']} to Instagram")
    wait = (when - now()).total_seconds()
    if wait > 0:
        log(f"{post['id']}: ready; publishing at {when.isoformat()} (in {wait / 60:.1f} min)")
        time.sleep(wait)
    publish(path, post, token)


def run() -> int:
    tokens = load_tokens()
    failures = 0
    files = sorted(QUEUE.glob("*.json"))
    posts = [(p, json.loads(p.read_text(encoding="utf-8"))) for p in files]
    due = []
    for path, post in posts:
        if post.get("status") not in ("queued", "uploaded"):
            continue
        when = dt.datetime.fromisoformat(post["publish_at"])
        if now() - when > LATE:
            post.update(status="missed", error=f"not posted: the run came more than {LATE} after {post['publish_at']}")
            save(path, post, f"Missed {post['id']}")
            log(f"{post['id']}: MISSED")
            failures += 1
        elif when - now() <= LEAD:
            due.append((when, path, post))
    if not due:
        nxt = min((dt.datetime.fromisoformat(p["publish_at"]) for _, p in posts
                   if p.get("status") in ("queued", "uploaded")), default=None)
        log("nothing due" + (f"; next post {nxt.isoformat()}" if nxt else "; queue empty"))
    for when, path, post in sorted(due, key=lambda d: d[0]):
        try:
            handle(path, post, tokens)
        except AuthError as e:
            log(f"{post['id']}: {e}")
            return 1
        except Exception as e:  # record it, keep going with the next post
            post.update(status="failed", error=scrub(e)[:500])
            save(path, post, f"Failed {post['id']}")
            log(f"{post['id']}: FAILED: {scrub(e)}")
            failures += 1
    return 1 if failures else 0


def test_upload(post_id: str) -> int:
    """Upload a queued post to Instagram and wait for processing, without publishing (the container
    simply expires after 24 h). Proves the token, the public video link and Instagram's processing."""
    path = QUEUE / f"{post_id}.json"
    post = json.loads(path.read_text(encoding="utf-8"))
    token = load_tokens().get(post["token_secret"])
    if not token:
        raise SystemExit(f"secret {post['token_secret']} is not set")
    container = upload(post, token)
    log(f"TEST OK: {post_id} uploaded and processed by Instagram (container {container}); NOT published")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-upload", metavar="ID")
    a = ap.parse_args()
    sys.exit(test_upload(a.test_upload) if a.test_upload else run())
