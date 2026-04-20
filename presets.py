import json
import os

PRESETS_FILE = "presets.json"

def load_presets() -> list:
    if not os.path.exists(PRESETS_FILE):
        return []
    with open(PRESETS_FILE, "r") as f:
        return json.load(f)

def _write_presets(presets: list):
    with open(PRESETS_FILE, "w") as f:
        json.dump(presets, f, indent=2)

def get_preset(preset_id: str) -> dict | None:
    return next((p for p in load_presets() if p["id"] == preset_id), None)

def save_preset(preset: dict):
    presets = load_presets()
    idx = next((i for i, p in enumerate(presets) if p["id"] == preset["id"]), None)
    if idx is not None:
        presets[idx] = preset
    else:
        presets.append(preset)
    _write_presets(presets)

def delete_preset(preset_id: str):
    presets = [p for p in load_presets() if p["id"] != preset_id]
    _write_presets(presets)

def default_preset(name: str) -> dict:
    """Returns a sensible default preset structure."""
    return {
        "id": "",
        "name": name,
        "resolution": {"w": 1080, "h": 1920},

        "background": {
            "type": "color",       # color | image | video
            "color": "#0a0a0a",
            "file": None
        },

        "bgm": {
            "file": None,
            "bgm_volume": 0.15,
            "voice_volume": 1.0,
            "fade_out": 2.0
        },

        "caption": {
            "font": None,           # path to font file
            "text_color": "#ffffff",
            "highlight": {
                "mode": "text",     # text | pill
                "color": "#f5a623",
                "pill_padding": 8
            },
            "without_visual": {
                "font_size": 72,
                "position": {"x": "center", "y": "center"},
                "max_chars_per_line": 22,
                "animation": "typing",   # typing | fade | none
                "animation_speed": 30    # chars/sec for typing
            },
            "with_visual": {
                "font_size": 52,
                "position": {"x": "center", "y": 1500},
                "max_chars_per_line": 28,
                "animation": "typing",
                "animation_speed": 30
            }
        },

        "frame": {
            "enabled": False,
            "file": None,            # image or video
            "container": {"x": 40, "y": 300, "w": 1000, "h": 860},
            "crop_anchor": "center"  # center | top | bottom
        },

        "visual": {
            "container": {"x": 60, "y": 320, "w": 960, "h": 820},
            "crop_anchor": "center",
            "zoom": 1.0,
            "animation": "fade_in",  # fade_in | slide_up | none
            "animation_duration": 0.5
        },

        "transitions": {
            "type": "fade",          # fade | cut
            "duration": 0.3
        }
    }
