# ReelD

Automate your story reels. Upload voiceover → transcribe → assign visuals → export.

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
