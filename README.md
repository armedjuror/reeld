# ReelD

Automate your story reels. Upload voiceover → transcribe → assign visuals → export.

> **The suite.** `suite/` is the series → episode → publish pipeline described in [SPEC.md](SPEC.md): scripts with a critic loop, recording with automatic take selection, Claude-made visuals rendered with ffmpeg, automations, SuperProfile publishing, Instagram insights and self-improving skills, driven from a task-first UI at `/studio`. It shares this app's Postgres database and `users` table. Setup and testing: [LOCAL_TESTING.md](LOCAL_TESTING.md).

## The suite

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/reeld   # same as the editor

alembic upgrade head                                  # creates the suite_* tables
python3 -m suite.cli create-user --email you@example.com --name Ajwad
python3 -m pytest tests -q                            # SQLite, no Postgres needed
```

**reeld MCP.** `.mcp.json` registers the server for Claude Code sessions opened in this repo, so a session can read your voice profile and push a series package straight into the database. Elsewhere: `claude mcp add reeld -- python3 -m suite.mcp_server` (run from the repo, with `DATABASE_URL` set). Set `REELD_USER_EMAIL` if the database has more than one user. Tools: `get_user_profile`, `set_user_profile`, `validate_series_package`, `create_series`, `update_series`, `get_series`, `list_series`.

**Designing a series.** The `reeld-series-package` skill (`.claude/skills/`) walks a Claude session through the interview, the package, validation and saving. Upload the same SKILL.md to claude.ai to use it in chats.

**Inspecting context.** See exactly what a stage would send to Claude:

```bash
python3 -m suite.cli context --series ballot-math --episode 3 --stage script
python3 -m suite.cli context --series ballot-math --episode 3 --stage script --manifest
```

Layout:

| Module | Job |
| --- | --- |
| `suite/models.py`, `migrations/` | Tables (`suite_*`) and Alembic migrations |
| `suite/documents.py` | Versioned text records |
| `suite/context.py` | Per-stage context bundles (voice ← bible ← episode) |
| `suite/package.py`, `suite/mcp_server.py` | Series packages and the reeld MCP |
| `suite/runner.py` | `claude -p` runner, usage-limit detection |
| `suite/pipeline.py` | Episode stages, checkpoints, pause/resume, gates |
| `suite/audio.py`, `suite/video.py` | Take selection, cleanup, loudness; scene rendering |
| `suite/schedule.py` | Tasks, due dates, Today/Tomorrow, calendar |
| `suite/insights.py` | Meta stats, analysis, proposals, edit distillation |
| `suite/integrations.py` | SuperProfile, git publishing, settings |
| `suite/worker.py` | Celery worker and beat |
| `suite/api.py`, `suite/app.py`, `static/suite.html` | API and UI |

## Setup

```bash
# 1. Install Python deps
pip install -r requirements.txt

# 2. Install FFmpeg (required for rendering)
# macOS:
brew install ffmpeg
# Ubuntu:
sudo apt install ffmpeg

# 3. Set OpenAI API key (for Whisper transcription)
export OPENAI_API_KEY=sk-...

# 4. Run
python main.py
# → open http://localhost:8000
```

## Using local Whisper (no API key)
```bash
pip install openai-whisper
```
Then in `transcribe.py`, change `transcribe_audio` to call `transcribe_audio_local`.

## Project Structure
```
ReelD/
  main.py          # FastAPI app + all endpoints
  presets.py       # Preset CRUD (presets.json)
  transcribe.py    # Whisper integration
  render.py        # FFmpeg render pipeline
  static/
    index.html     # Single page UI
    app.js         # All frontend logic
  fonts/           # Uploaded fonts (persistent)
  bgm/             # BGM files (persistent, per preset)
  frames/          # Frame assets (persistent, per preset)
  backgrounds/     # Background files (persistent, per preset)
  temp/            # Session files (ephemeral, per export)
  presets.json     # All preset configs
```

## Caption Highlighting
Wrap words in `*asterisks*` in the caption editor to highlight them.

Example: `"One *decision* changed *everything* forever."`

Highlight mode (text color or pill background) is set per preset.

## Workflow
1. **Presets** tab → create preset (background, BGM, caption style, visual layout, frame, transitions)
2. **Generate** tab → pick preset → upload voiceover → Transcribe
3. Review segments → edit captions, add visuals, pin intro
4. Export → download MP4
