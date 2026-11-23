# Running and testing the suite on your Mac

Everything below runs from the repo root on the `feat/suite-foundation` branch.

## 1. One-time setup

```bash
brew install ffmpeg librsvg redis postgresql@16    # skip what you already have
brew services start redis
brew services start postgresql@16

pip install -r requirements.txt
pip install -r requirements-ml.txt                 # optional: speech-model take selection (~1 GB)

export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/reeld   # your existing reeld DB is fine
alembic upgrade head                               # adds the suite_* tables; leaves your tables alone
python3 -m suite.cli create-user --email you@example.com --name Ajwad
```

If your database already has your Google-login user, skip `create-user` and set
`REELD_USER_EMAIL` to that email instead.

`claude` must be installed and signed in (`claude -p "hi"` should answer).

## 2. Run the tests

```bash
python3 -m pytest tests -q                         # 49 unit/integration tests, no Claude calls
REELD_LOCAL_TESTS=1 python3 -m pytest tests/test_local.py -v -s
```

The local tier, and what each test proves:

| Test | Needs | Proves |
| --- | --- | --- |
| `test_real_claude_dummy_episode` | `claude` signed in | One tiny episode through every Claude step with real `claude -p`: research, script, critic, scene SVGs, render, automation, caption, continuity. ~1–2 min, a few cents. Prints the video path. |
| `test_reeld_mcp_from_claude` | `claude` | Claude Code can load the reeld MCP server and call its tools. |
| `test_celery_worker_over_redis` | `redis-server` | A real Celery worker takes a job from Redis and advances an episode. |
| `test_mms_alignment_drops_fumbled_take` | `requirements-ml.txt`, macOS `say` | The speech model keeps the retake and drops a fumbled first take, on real speech. |
| `test_superprofile_tools_visible` | SuperProfile set up (below), `REELD_SUPERPROFILE=1` | Claude Code can see the SuperProfile tools. |
| `test_meta_token_live` | `REELD_META_TOKEN` | Your Instagram token can list reels and read insights. |

Already verified in the build sandbox: the first three (real Claude, real MCP, real Redis + Celery),
plus everything on Postgres 16. Not verifiable there: the speech model (no model download),
SuperProfile (your account) and Meta (your token).

## 3. Try the UI without spending Claude

```bash
python3 -m suite.cli demo                          # a paused demo series with an episode at every gate
uvicorn suite.app:app --port 8100                  # or run main.py and open /studio
```

Open http://localhost:8100. The demo uses generated tones instead of a voice, so the audio
and video are placeholders.

## 4. Run it for real

Three processes:

```bash
uvicorn suite.app:app --port 8100                  # UI + API (or main.py → /studio)
celery -A suite.worker worker --beat --concurrency 2 --loglevel info
# Postgres and Redis via brew services
```

Then:

1. Design a series in a Claude chat or Claude Code in this repo. `.mcp.json` gives Claude
   Code the reeld tools; the `reeld-series-package` skill drives the conversation.
2. In the UI, Series → **Start the pipeline**. The worker writes scripts for the next 3
   episodes (`REELD_SCRIPTS_AHEAD`).
3. Work through **Tasks**: approve scripts, record (teleprompter in the browser, or upload a
   file), check audio, watch the video. Approving the video runs the automations and publishes.

One-off foreground run without Celery: `python3 -m suite.cli advance --episode 12` or
`python3 -m suite.cli tick`.

## 5. Integrations

**SuperProfile publishing** (optional; without it you get a "Post" task with the caption,
DM text and a video download):

```bash
claude mcp add --scope user --transport http superprofile <SuperProfile MCP URL>
claude          # then run /mcp, pick superprofile, sign in once
```

Then Settings → Publishing → tick "Publish through SuperProfile". SuperProfile needs to fetch
the video, so the video must have a public URL: either `STORAGE=s3` (presigned URLs) or a
tunnel to the Mac with `REELD_PUBLIC_BASE_URL=https://<your-tunnel>` (e.g. `cloudflared
tunnel --url http://localhost:8100`).

**Instagram stats:** create a Meta developer app with the Instagram API (Instagram Login),
add your professional account as a tester, generate a long-lived token, and paste it in
Settings → Instagram stats. Stats are pulled when you open the app (at most every 20 hours),
and the analysis runs once a series has 3 live reels with stats.

**Blog automation:** give the automation a git config in the series package, e.g.

```json
{"name": "blog", "order": 1, "config": {
  "handler": "git", "repo_path": "~/code/armedjuror.github.io",
  "path": "_posts/{date}-{series}-ep{number}.md", "url": "https://armedjuror.in/{series}/ep{number}/"}}
```

The case study is written by the `auto-blog` skill, committed and pushed, and its URL goes
into the caption and the DM.

## 6. Settings reference

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | local `reeld` DB | Same variable as the editor |
| `REELD_USER_EMAIL` | the only user | Which account the suite acts for |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker |
| `REELD_EAGER` | off | `1` runs jobs inline (no Redis) |
| `REELD_MEDIA_ROOT` | `suite_media/` | Local media storage |
| `STORAGE` | `local` | `s3` to use the bucket settings in `storage.py` |
| `REELD_PUBLIC_BASE_URL` | unset | Public URL of this app, for SuperProfile to fetch videos |
| `REELD_SCRIPTS_AHEAD` | 3 | Episodes in flight per series |
| `REELD_MAX_REVISIONS` | 3 | Critic → revise rounds |
| `REELD_DISTIL_EVERY` | 10 | Approved scripts between learnings proposals |
| `REELD_CLAUDE_MODEL` | Claude Code default | Model for `claude -p` steps |
| `REELD_LIMIT_BACKOFF_MIN` | 30 | Retry delay when a limit message has no reset time |
| `REELD_TZ` | `Asia/Kolkata` | Slots, due dates, reset times |
