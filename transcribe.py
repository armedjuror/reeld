import os
import json

def transcribe_audio(audio_path: str) -> list:
    """
    Transcribe audio using OpenAI Whisper API (word-level timestamps).
    Returns list of segments: [{start, end, text, words}]
    """
    try:
        import openai
        client = openai.OpenAI()

        with open(audio_path, "rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="verbose_json",
                timestamp_granularities=["segment", "word"]
            )

        segments = []
        for seg in result.segments:
            segments.append({
                "index": seg.id,
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "caption": seg.text.strip(),
                "type": "text_only",
                "visual_path": None,
                "is_intro_pin": False
            })

        return segments

    except ImportError:
        raise RuntimeError("openai package not installed. Run: pip install openai")
    except Exception as e:
        raise RuntimeError(f"Transcription failed: {e}")


def transcribe_audio_local(audio_path: str) -> list:
    """
    Fallback: local Whisper via whisper package.
    Slower but no API key needed.
    """
    try:
        import whisper
        model = whisper.load_model("base")
        result = model.transcribe(audio_path, word_timestamps=True)

        segments = []
        for i, seg in enumerate(result["segments"]):
            segments.append({
                "index": i,
                "start": round(seg["start"], 2),
                "end": round(seg["end"], 2),
                "caption": seg["text"].strip(),
                "type": "text_only",
                "visual_path": None,
                "is_intro_pin": False
            })

        return segments

    except ImportError:
        raise RuntimeError("whisper package not installed. Run: pip install openai-whisper")
