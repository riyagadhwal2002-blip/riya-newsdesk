"""Turns a post card into a short 9:16 Reel video (card centred on its colour, gentle zoom, silent audio track)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .render import render_story


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg  # bundled static ffmpeg, installed via requirements.txt
    return imageio_ffmpeg.get_ffmpeg_exe()


def render_reel(card_path: Path, out_path: Path, brand: dict, seconds: int = 8, fps: int = 30) -> Path:
    out_path = Path(out_path)
    frame = render_story(card_path, out_path.with_suffix(".frame.jpg"), brand)   # 1080x1920 card on soft colour
    frames = seconds * fps
    zoom = f"zoompan=z='1+0.04*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1080x1920:fps={fps}"
    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error",
           "-loop", "1", "-i", str(frame),
           "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
           "-filter_complex", f"[0:v]scale=2160:3840,{zoom},format=yuv420p[v]",
           "-map", "[v]", "-map", "1:a", "-t", str(seconds),
           "-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-crf", "20", "-g", str(fps),
           "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-shortest", "-movflags", "+faststart", str(out_path)]
    subprocess.run(cmd, check=True, timeout=240)
    return out_path
