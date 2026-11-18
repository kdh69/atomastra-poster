# atomastra-poster

Publishes Atomastra's approved Instagram Reels at their scheduled times, from the cloud: no computer or phone needs to be on. It's public so its GitHub Actions minutes are free. The main repo stays private.

- **Queue:** `queue/<id>.json`, one file per post: time, caption, status, and once posted the Instagram link. Covers are in `covers/`. Videos are assets of the [videos release](../../releases/tag/videos).
- **Adding a post:** only after Kyle approves it, from the main repo: `python scripts/ig_queue.py add --slot <slot> --yes`. Use `list` to see the queue and `cancel <id>` to stop a post.
- **Posting:** `.github/workflows/post.yml` runs every 10 minutes. It picks a post up 40 minutes before its time, uploads it to Instagram, and publishes at the exact minute. A post more than 3 hours late is marked missed, never posted late.
- **Failures:** a failed post fails the run, and GitHub emails Kyle.
- **Business Suite:** posts appear there once published. Instagram's API can't put them in the Planner beforehand.
- **Token:** the Page token is the repository secret `IG_PAGE_TOKEN_ATOMASTRA`. It is never committed or printed.
