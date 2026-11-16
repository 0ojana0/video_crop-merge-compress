#!/usr/bin/env python3
"""
Automated Multi-Video Unified Crop & Lossless/Visually-Lossless x265 Merge Tool
===============================================================================
Pipeline:
  1. Auto-installs missing Python (opencv-python, numpy) and system (ffmpeg, ffprobe)
     dependencies across Linux, macOS, and Windows.
  2. Selects multiple videos of any format (.mp4, .mkv, .mov, .avi, .webm, .ts, etc.)
     via a native multi-file GUI dialog or CLI arguments.
  3. Probes all inputs via ffprobe and selects the "best" reference input based on
     resolution (pixels), frame rate, bit depth, video bitrate, and audio quality.
  4. Launches an interactive OpenCV visual crop selector normalized to the best
     input's canvas (with composite blend mode, per-video preview cycling, and
     timeline scrubbing) to choose a single unified cropping zone.
  5. Merges and crops all videos into a faststart MP4 using libx265 10-bit
     (yuv420p10le, CRF 15, preset slow, custom x265 tuning, hvc1 tag, audio copy
     when stream-compatible or high-fidelity best-input-matched encoding when
     heterogeneous, mov_text subtitles, and metadata mapped from the best input).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from fractions import Fraction
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# 1. Dependency Management
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv",
    ".m4v", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".ogv"
)


def install_python_package(pkg_Import_name: str, pip_spec: str) -> None:
    """Import a Python module or install it automatically via pip."""
    try:
        __import__(pkg_Import_name)
    except ImportError:
        print(f"[deps] Installing missing Python package '{pip_spec}'...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--upgrade", pip_spec]
        )


def ensure_ffmpeg_installed() -> None:
    """Ensure both ffmpeg and ffprobe are available on PATH, attempting OS install if missing."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return

    print("[deps] 'ffmpeg' / 'ffprobe' not found on PATH. Attempting automatic installation...")
    system = platform.system().lower()
    install_cmds: List[List[str]] = []

    if system == "linux":
        sudo = ["sudo"] if os.geteuid() != 0 and shutil.which("sudo") else []
        if shutil.which("apt-get"):
            install_cmds.append(sudo + ["apt-get", "update", "-y"])
            install_cmds.append(sudo + ["apt-get", "install", "-y", "ffmpeg"])
        elif shutil.which("dnf"):
            install_cmds.append(sudo + ["dnf", "install", "-y", "ffmpeg"])
        elif shutil.which("pacman"):
            install_cmds.append(sudo + ["pacman", "-Sy", "--noconfirm", "ffmpeg"])
        elif shutil.which("zypper"):
            install_cmds.append(sudo + ["zypper", "--non-interactive", "install", "ffmpeg"])
    elif system == "darwin":
        if shutil.which("brew"):
            install_cmds.append(["brew", "install", "ffmpeg"])
    elif system == "windows":
        if shutil.which("winget"):
            install_cmds.append([
                "winget", "install", "--id=Gyan.FFmpeg", "-e",
                "--accept-package-agreements", "--accept-source-agreements"
            ])
        elif shutil.which("choco"):
            install_cmds.append(["choco", "install", "ffmpeg", "-y"])

    for cmd in install_cmds:
        try:
            print(f"[deps] Running: {' '.join(cmd)}")
            subprocess.check_call(cmd)
        except subprocess.CalledProcessError as exc:
            print(f"[deps] Warning: command failed ({exc}).")

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        raise RuntimeError(
            "ffmpeg and/or ffprobe could not be installed automatically. "
            "Please install FFmpeg manually and ensure it is on your PATH."
        )


def ensure_dependencies(skip_install: bool = False) -> None:
    """Verify and automatically install required system and Python packages."""
    if not skip_install:
        ensure_ffmpeg_installed()
        install_python_package("numpy", "numpy")
        install_python_package("cv2", "opencv-python")
    else:
        if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            raise RuntimeError("ffmpeg and ffprobe are required on PATH.")


# ---------------------------------------------------------------------------
# 2. Video Selection (GUI + CLI)
# ---------------------------------------------------------------------------

def select_videos_interactively(initial_paths: Optional[List[str]] = None) -> List[Path]:
    """Select multiple video files via Tkinter file dialog or terminal fallback."""
    if initial_paths:
        paths = [Path(p).expanduser().resolve() for p in initial_paths]
        missing = [str(p) for p in paths if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"Input file(s) not found: {', '.join(missing)}")
        return paths

    selected: List[Path] = []
    # Attempt native GUI multi-file picker first
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        ext_pattern = " ".join(f"*{ext}" for ext in VIDEO_EXTENSIONS)
        file_paths = filedialog.askopenfilenames(
            title="Select Videos to Crop & Merge (Order Preserved)",
            filetypes=[
                ("Video Files", ext_pattern),
                ("All Files", "*.*"),
            ],
        )
        root.destroy()
        selected = [Path(p).resolve() for p in file_paths if p]
    except Exception as exc:
        print(f"[ui] GUI file dialog unavailable ({exc}). Falling back to console input.")

    if not selected:
        print("\nEnter video file paths or glob patterns (one per line, blank line to finish):")
        while True:
            try:
                line = input("  > ").strip()
            except EOFError:
                break
            if not line:
                break
            expanded = Path(line).expanduser()
            if expanded.is_file():
                selected.append(expanded.resolve())
            else:
                matches = sorted(Path().glob(line))
                for m in matches:
                    if m.is_file() and m.suffix.lower() in VIDEO_EXTENSIONS:
                        selected.append(m.resolve())

    if not selected:
        raise ValueError("No video files were selected.")
    return selected


def select_output_path(default_dir: Path, explicit_output: Optional[str] = None) -> Path:
    """Choose the output MP4 path via CLI argument, GUI save dialog, or sensible default."""
    if explicit_output:
        out = Path(explicit_output).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        return out

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        save_path = filedialog.asksaveasfilename(
            title="Save Cropped & Merged Video As",
            initialdir=str(default_dir),
            initialfile="merged_cropped_output.mp4",
            defaultextension=".mp4",
            filetypes=[("MP4 Video", "*.mp4"), ("Matroska Video", "*.mkv")],
        )
        root.destroy()
        if save_path:
            return Path(save_path).resolve()
    except Exception:
        pass

    return (default_dir / "merged_cropped_output.mp4").resolve()


# ---------------------------------------------------------------------------
# 3. Probing & Selecting the Best Input Specification
# ---------------------------------------------------------------------------

@dataclass
class VideoSpecs:
    index: int
    path: Path
    width: int
    height: int
    fps_fraction: Fraction
    fps_float: float
    pix_fmt: str
    bit_depth: int
    video_codec: str
    video_bitrate: int
    duration: float
    has_audio: bool
    audio_codec: Optional[str]
    audio_sample_rate: int
    audio_channels: int
    audio_bitrate: int
    has_subtitles: bool
    subtitle_codec: Optional[str]

    @property
    def resolution_pixels(self) -> int:
        return self.width * self.height

    @property
    def quality_score(self) -> Tuple[int, float, int, int, int, int]:
        """Tuple used to rank inputs from highest to lowest specification quality."""
        return (
            self.resolution_pixels,
            round(self.fps_float, 3),
            self.bit_depth,
            self.video_bitrate,
            self.audio_sample_rate,
            self.audio_bitrate,
        )


def parse_fps(rate_str: str) -> Fraction:
    """Safely parse an ffprobe frame rate string like '60000/1001' or '30/1'."""
    if not rate_str or rate_str == "0/0":
        return Fraction(30, 1)
    try:
        frac = Fraction(rate_str)
        return frac if frac > 0 else Fraction(30, 1)
    except (ValueError, ZeroDivisionError):
        return Fraction(30, 1)


def infer_bit_depth(pix_fmt: str, bits_per_raw_sample: Optional[str]) -> int:
    """Determine bit depth from ffprobe stream metadata."""
    if bits_per_raw_sample and bits_per_raw_sample.isdigit():
        val = int(bits_per_raw_sample)
        if val >= 8:
            return val
    if "12" in pix_fmt:
        return 12
    if "10" in pix_fmt or "p010" in pix_fmt:
        return 10
    return 8


def probe_video(path: Path, index: int) -> VideoSpecs:
    """Inspect a video file using ffprobe and return structured specifications."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    info = json.loads(proc.stdout)

    streams = info.get("streams", [])
    fmt = info.get("format", {})

    v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v_stream is None:
        raise ValueError(f"No video stream found in '{path}'.")

    a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    s_stream = next((s for s in streams if s.get("codec_type") == "subtitle"), None)

    width = int(v_stream.get("width", 1920))
    height = int(v_stream.get("height", 1080))

    avg_fps = parse_fps(v_stream.get("avg_frame_rate", "0/0"))
    r_fps = parse_fps(v_stream.get("r_frame_rate", "30/1"))
    fps_frac = avg_fps if float(avg_fps) > 1.0 else r_fps

    pix_fmt = v_stream.get("pix_fmt", "yuv420p")
    bit_depth = infer_bit_depth(pix_fmt, v_stream.get("bits_per_raw_sample"))
    video_codec = v_stream.get("codec_name", "unknown")

    v_bitrate = int(v_stream.get("bit_rate") or fmt.get("bit_rate") or 0)
    duration = float(v_stream.get("duration") or fmt.get("duration") or 0.0)

    has_audio = a_stream is not None
    audio_codec = a_stream.get("codec_name") if a_stream else None
    audio_sr = int(a_stream.get("sample_rate") or 48000) if a_stream else 0
    audio_ch = int(a_stream.get("channels") or 2) if a_stream else 0
    audio_br = int(a_stream.get("bit_rate") or 0) if a_stream else 0

    has_subs = s_stream is not None
    sub_codec = s_stream.get("codec_name") if s_stream else None

    return VideoSpecs(
        index=index,
        path=path,
        width=width,
        height=height,
        fps_fraction=fps_frac,
        fps_float=float(fps_frac),
        pix_fmt=pix_fmt,
        bit_depth=bit_depth,
        video_codec=video_codec,
        video_bitrate=v_bitrate,
        duration=duration,
        has_audio=has_audio,
        audio_codec=audio_codec,
        audio_sample_rate=audio_sr,
        audio_channels=audio_ch,
        audio_bitrate=audio_br,
        has_subtitles=has_subs,
        subtitle_codec=sub_codec,
    )


def choose_best_input(specs_list: List[VideoSpecs]) -> VideoSpecs:
    """Return the highest-specification input video."""
    best = max(specs_list, key=lambda s: s.quality_score)
    return best


# ---------------------------------------------------------------------------
# 4. Unified Visual Crop Selector (OpenCV Interactive ROI)
# ---------------------------------------------------------------------------

def extract_normalized_sample_frames(
    specs_list: List[VideoSpecs],
    target_w: int,
    target_h: int,
    samples_per_video: int = 3,
):
    """
    Sample frames from each video and letterbox/pillarbox them to (target_w, target_h)
    matching the exact aspect-preserving normalization used by FFmpeg during merging.
    """
    import cv2
    import numpy as np

    frames_by_video = []
    labels = []

    for spec in specs_list:
        cap = cv2.VideoCapture(str(spec.path))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        sample_indices = (
            [int(total_frames * r) for r in (0.15, 0.5, 0.85)][:samples_per_video]
            if total_frames > 10
            else [0]
        )

        collected = []
        for f_idx in sample_indices:
            if total_frames > 10:
                cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue

            h, w = frame.shape[:2]
            scale = min(target_w / w, target_h / h)
            new_w = max(1, int(round(w * scale)))
            new_h = max(1, int(round(h * scale)))
            resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)

            canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
            off_x = (target_w - new_w) // 2
            off_y = (target_h - new_h) // 2
            canvas[off_y : off_y + new_h, off_x : off_x + new_w] = resized
            collected.append(canvas)

        cap.release()
        if collected:
            frames_by_video.append(collected)
            labels.append(f"#{spec.index + 1}: {spec.path.name} ({spec.width}x{spec.height})")

    if not frames_by_video:
        raise RuntimeError("Could not decode preview frames from any input video.")

    return frames_by_video, labels


def sanitize_crop_box(
    x: int, y: int, w: int, h: int, canvas_w: int, canvas_h: int
) -> Tuple[int, int, int, int]:
    """Clamp crop rectangle to canvas bounds and enforce even width/height for 4:2:0."""
    x = max(0, min(int(x), canvas_w - 2))
    y = max(0, min(int(y), canvas_h - 2))
    w = max(2, min(int(w), canvas_w - x))
    h = max(2, min(int(h), canvas_h - y))

    # Ensure even dimensions for yuv420p10le compatibility
    w -= w % 2
    h -= h % 2
    if w < 2:
        w = 2
    if h < 2:
        h = 2
    return x, y, w, h


def select_unified_crop_visually(
    specs_list: List[VideoSpecs],
    best_spec: VideoSpecs,
    explicit_crop: Optional[str] = None,
) -> Tuple[int, int, int, int]:
    """
    Visually select a single unified crop box (x, y, w, h) in the coordinate space
    of best_spec (best_spec.width x best_spec.height).
    """
    target_w, target_h = best_spec.width, best_spec.height

    if explicit_crop:
        parts = [int(v.strip()) for v in explicit_crop.split(",")]
        if len(parts) != 4:
            raise ValueError("--crop must be formatted as 'x,y,w,h' (4 integers).")
        return sanitize_crop_box(parts[0], parts[1], parts[2], parts[3], target_w, target_h)

    import cv2
    import numpy as np

    frames_by_video, labels = extract_normalized_sample_frames(specs_list, target_w, target_h)
    all_frames = [f for group in frames_by_video for f in group]

    # Build composite views so the user can see where all videos overlap
    mean_blend = np.mean(np.stack(all_frames, axis=0), axis=0).astype(np.uint8)
    max_blend = np.max(np.stack(all_frames, axis=0), axis=0).astype(np.uint8)

    preview_modes = [
        ("Composite Mean Blend (All Videos)", mean_blend),
        ("Composite Max Projection (All Videos)", max_blend),
    ]
    for group, label in zip(frames_by_video, labels):
        mid_frame = group[len(group) // 2]
        preview_modes.append((label, mid_frame))

    # Scale preview window to fit comfortable desktop dimensions while preserving coordinates
    max_display_w, max_display_h = 1280, 760
    disp_scale = min(1.0, max_display_w / target_w, max_display_h / target_h)
    disp_w = int(round(target_w * disp_scale))
    disp_h = int(round(target_h * disp_scale))

    state = {
        "mode_idx": 0,
        "dragging": False,
        "start_pt": (0, 0),
        "rect": (0, 0, disp_w, disp_h),  # (x, y, w, h) in display coordinates
    }

    win_name = "Unified Crop Selector | Drag Mouse | [TAB/N] Switch View | [R] Full | [ENTER/SPACE] Confirm"

    def on_mouse(event, mx, my, flags, param):
        mx = max(0, min(mx, disp_w - 1))
        my = max(0, min(my, disp_h - 1))
        if event == cv2.EVENT_LBUTTONDOWN:
            state["dragging"] = True
            state["start_pt"] = (mx, my)
            state["rect"] = (mx, my, 1, 1)
        elif event == cv2.EVENT_MOUSEMOVE and state["dragging"]:
            sx, sy = state["start_pt"]
            rx, ry = min(sx, mx), min(sy, my)
            rw, rh = max(2, abs(mx - sx)), max(2, abs(my - sy))
            state["rect"] = (rx, ry, rw, rh)
        elif event == cv2.EVENT_LBUTTONUP:
            state["dragging"] = False
            sx, sy = state["start_pt"]
            rx, ry = min(sx, mx), min(sy, my)
            rw, rh = max(2, abs(mx - sx)), max(2, abs(my - sy))
            if rw < 6 or rh < 6:
                state["rect"] = (0, 0, disp_w, disp_h)
            else:
                state["rect"] = (rx, ry, rw, rh)

    try:
        cv2.namedWindow(win_name, cv2.WINDOW_AUTOSIZE)
        cv2.setMouseCallback(win_name, on_mouse)

        print("\n[crop] Interactive Unified Crop Selector opened:")
        print("  - Click and drag with the mouse to draw the unified crop zone.")
        print("  - Press TAB or 'n' to cycle between Composite Blend and individual videos.")
        print("  - Press 'r' to reset to the full frame.")
        print("  - Press ENTER or SPACE to confirm your crop selection.")

        while True:
            mode_label, base_img = preview_modes[state["mode_idx"]]
            disp_img = cv2.resize(base_img, (disp_w, disp_h), interpolation=cv2.INTER_AREA)

            rx, ry, rw, rh = state["rect"]
            # Dim outside crop region
            overlay = (disp_img * 0.35).astype(np.uint8)
            overlay[ry : ry + rh, rx : rx + rw] = disp_img[ry : ry + rh, rx : rx + rw]

            # Draw crop border and rule-of-thirds guides
            cv2.rectangle(overlay, (rx, ry), (rx + rw, ry + rh), (0, 255, 180), 2)
            for third in (1, 2):
                gx = rx + (rw * third) // 3
                gy = ry + (rh * third) // 3
                cv2.line(overlay, (gx, ry), (gx, ry + rh), (0, 180, 120), 1)
                cv2.line(overlay, (rx, gy), (rx + rw, gy), (0, 180, 120), 1)

            real_x = int(round(rx / disp_scale))
            real_y = int(round(ry / disp_scale))
            real_w = int(round(rw / disp_scale))
            real_h = int(round(rh / disp_scale))
            real_x, real_y, real_w, real_h = sanitize_crop_box(
                real_x, real_y, real_w, real_h, target_w, target_h
            )

            hud_text = (
                f"View: {mode_label} | Crop: x={real_x}, y={real_y}, "
                f"w={real_w}, h={real_h} (Canvas {target_w}x{target_h})"
            )
            cv2.rectangle(overlay, (0, 0), (disp_w, 34), (20, 20, 20), -1)
            cv2.putText(
                overlay,
                hud_text,
                (10, 23),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (240, 240, 240),
                1,
                cv2.LINE_AA,
            )

            cv2.imshow(win_name, overlay)
            key = cv2.waitKey(20) & 0xFF

            if key in (13, 32):  # ENTER or SPACE
                break
            elif key in (9, ord("n"), ord("N")):  # TAB or N
                state["mode_idx"] = (state["mode_idx"] + 1) % len(preview_modes)
            elif key in (ord("p"), ord("P")):
                state["mode_idx"] = (state["mode_idx"] - 1) % len(preview_modes)
            elif key in (ord("r"), ord("R")):
                state["rect"] = (0, 0, disp_w, disp_h)
            elif key == 27:  # ESC
                cv2.destroyAllWindows()
                raise KeyboardInterrupt("Crop selection cancelled by user.")

        cv2.destroyAllWindows()
        rx, ry, rw, rh = state["rect"]
        real_x = int(round(rx / disp_scale))
        real_y = int(round(ry / disp_scale))
        real_w = int(round(rw / disp_scale))
        real_h = int(round(rh / disp_scale))
        return sanitize_crop_box(real_x, real_y, real_w, real_h, target_w, target_h)

    except cv2.error as exc:
        print(f"[crop] OpenCV GUI unavailable ({exc}). Using full canvas {target_w}x{target_h}.")
        return sanitize_crop_box(0, 0, target_w, target_h, target_w, target_h)


# ---------------------------------------------------------------------------
# 5. FFmpeg Lossless / Visually-Lossless Re-Encoding & Merging
# ---------------------------------------------------------------------------

def inputs_are_uniform_for_concat_demuxer(specs_list: List[VideoSpecs]) -> bool:
    """
    Check if all inputs share the exact same resolution, frame rate, video codec,
    and audio stream parameters so the concat demuxer can stream-copy audio (`-c:a copy`)
    and subtitles (`-c:s mov_text`) directly.
    """
    if len(specs_list) <= 1:
        return True
    first = specs_list[0]
    for s in specs_list[1:]:
        if (s.width, s.height) != (first.width, first.height):
            return False
        if abs(s.fps_float - first.fps_float) > 0.05:
            return False
        if s.video_codec != first.video_codec:
            return False
        if s.has_audio != first.has_audio:
            return False
        if s.has_audio and (
            s.audio_codec != first.audio_codec
            or s.audio_sample_rate != first.audio_sample_rate
            or s.audio_channels != first.audio_channels
        ):
            return False
    return True


def build_and_run_ffmpeg(
    specs_list: List[VideoSpecs],
    best_spec: VideoSpecs,
    crop_box: Tuple[int, int, int, int],
    output_path: Path,
    crf: int = 15,
    preset: str = "slow",
    true_lossless: bool = False,
) -> None:
    """
    Build and execute the FFmpeg command to crop and merge all inputs while
    preferring the specifications and metadata of `best_spec`.
    """
    cx, cy, cw, ch = crop_box
    crop_filter = f"crop={cw}:{ch}:{cx}:{cy}"

    x265_params = "no-sao=1:deblock=-1,-1:aq-mode=3:strong-intra-smoothing=0"
    if true_lossless:
        x265_params = f"lossless=1:{x265_params}"

    video_encode_args = [
        "-c:v", "libx265",
        "-crf", str(crf),
        "-preset", preset,
        "-pix_fmt", "yuv420p10le",
        "-x265-params", x265_params,
        "-tag:v", "hvc1",
    ]

    # Case 1: Single input file — matches the exact reference command structure
    if len(specs_list) == 1:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(specs_list[0].path),
            "-vf", crop_filter,
            "-map", "0:v:0",
            "-map", "0:a?",
            "-map", "0:s?",
            *video_encode_args,
            "-c:a", "copy",
            "-c:s", "mov_text",
            "-map_metadata", "0",
            "-movflags", "+faststart",
            str(output_path),
        ]
        print("\n[ffmpeg] Executing single-input crop & encode:")
        print("  " + " ".join(cmd))
        subprocess.run(cmd, check=True)
        return

    # Case 2: Multiple uniform inputs — concat demuxer preserves `-c:a copy` and `-c:s mov_text`
    if inputs_are_uniform_for_concat_demuxer(specs_list):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tmp_concat:
            concat_list_path = Path(tmp_concat.name)
            for s in specs_list:
                escaped = str(s.path).replace("'", "'\\''")
                tmp_concat.write(f"file '{escaped}'\n")

        try:
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list_path),
                "-i", str(best_spec.path),  # Secondary input to map best input's metadata
                "-vf", crop_filter,
                "-map", "0:v:0",
                "-map", "0:a?",
                "-map", "0:s?",
                *video_encode_args,
                "-c:a", "copy",
                "-c:s", "mov_text",
                "-map_metadata", "1",
                "-movflags", "+faststart",
                str(output_path),
            ]
            print("\n[ffmpeg] Executing uniform concat-demuxer merge & crop:")
            print("  " + " ".join(cmd))
            subprocess.run(cmd, check=True)
        finally:
            concat_list_path.unlink(missing_ok=True)
        return

    # Case 3: Heterogeneous multi-type inputs — normalize all streams to best_spec's canvas & FPS
    input_args: List[str] = []
    for s in specs_list:
        input_args.extend(["-i", str(s.path)])

    n = len(specs_list)
    any_audio = any(s.has_audio for s in specs_list)
    best_w, best_h = best_spec.width, best_spec.height
    best_fps = f"{best_spec.fps_fraction.numerator}/{best_spec.fps_fraction.denominator}"
    best_sr = best_spec.audio_sample_rate if best_spec.audio_sample_rate > 0 else 48000
    best_ch = best_spec.audio_channels if best_spec.audio_channels > 0 else 2
    ch_layout = "mono" if best_ch == 1 else ("5.1" if best_ch == 6 else "stereo")

    filter_parts: List[str] = []
    concat_inputs: List[str] = []

    for i, s in enumerate(specs_list):
        filter_parts.append(
            f"[{i}:v:0]scale={best_w}:{best_h}:force_original_aspect_ratio=decrease,"
            f"pad={best_w}:{best_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={best_fps},format=yuv420p10le[v{i}]"
        )
        concat_inputs.append(f"[v{i}]")

        if any_audio:
            if s.has_audio:
                filter_parts.append(
                    f"[{i}:a:0]aresample={best_sr},"
                    f"aformat=sample_fmts=fltp:channel_layouts={ch_layout}[a{i}]"
                )
            else:
                # Generate silent audio matching the video's duration so concat stays synced
                dur = max(0.1, s.duration)
                filter_parts.append(
                    f"anullsrc=r={best_sr}:cl={ch_layout}:d={dur}[a{i}]"
                )
            concat_inputs.append(f"[a{i}]")

    if any_audio:
        filter_parts.append(
            f"{''.join(concat_inputs)}concat=n={n}:v=1:a=1[vcat][aout]"
        )
    else:
        filter_parts.append(
            f"{''.join(concat_inputs)}concat=n={n}:v=1:a=0[vcat]"
        )

    filter_parts.append(f"[vcat]{crop_filter}[vout]")
    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *input_args,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
    ]

    if any_audio:
        cmd.extend(["-map", "[aout]"])

    # Map subtitles from the best input if present
    if best_spec.has_subtitles:
        cmd.extend(["-map", f"{best_spec.index}:s?"])

    cmd.extend(video_encode_args)

    if any_audio:
        # Use ALAC (lossless in MP4) or high-bitrate AAC matching best input's sample rate
        target_br = max(320_000, best_spec.audio_bitrate)
        cmd.extend([
            "-c:a", "aac",
            "-b:a", str(min(target_br, 512_000)),
            "-ar", str(best_sr),
        ])

    cmd.extend([
        "-c:s", "mov_text",
        "-map_metadata", str(best_spec.index),
        "-movflags", "+faststart",
        str(output_path),
    ])

    print("\n[ffmpeg] Executing heterogeneous normalized merge & crop:")
    print("  " + " ".join(cmd))
    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------
# 6. CLI Entrypoint
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Automatically install dependencies, select multiple videos, visually "
            "choose a unified crop zone, and merge/re-encode losslessly with libx265 "
            "preferring the best input's specifications."
        )
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="Optional input video file paths. Opens a GUI file selector if omitted.",
    )
    parser.add_argument(
        "-o", "--output",
        help="Output video path (default: GUI save dialog or merged_cropped_output.mp4).",
    )
    parser.add_argument(
        "--crop",
        help="Optional non-interactive crop rectangle 'x,y,w,h' in best-input coordinates.",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=15,
        help="x265 CRF value (default: 15 per visually-lossless specification).",
    )
    parser.add_argument(
        "--preset",
        default="slow",
        help="x265 encoding preset (default: slow).",
    )
    parser.add_argument(
        "--lossless",
        action="store_true",
        help="Enable bit-exact x265 mathematical lossless mode (lossless=1).",
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Skip automatic dependency installation checks.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    # Step 1: Ensure dependencies are installed
    ensure_dependencies(skip_install=args.skip_install)

    # Step 2: Select input videos
    video_paths = select_videos_interactively(args.inputs)
    print(f"\n[info] Selected {len(video_paths)} video(s):")
    for idx, p in enumerate(video_paths, 1):
        print(f"  {idx}. {p}")

    # Step 3: Probe all inputs & determine the highest-spec reference input
    specs_list = [probe_video(p, idx) for idx, p in enumerate(video_paths)]
    best_spec = choose_best_input(specs_list)

    print("\n[specs] Input Specifications Summary:")
    for s in specs_list:
        marker = "★ BEST" if s.index == best_spec.index else "      "
        print(
            f"  [{marker}] #{s.index + 1} {s.path.name}: "
            f"{s.width}x{s.height} @ {s.fps_float:.2f}fps, "
            f"{s.bit_depth}-bit ({s.pix_fmt}), codec={s.video_codec}, "
            f"v_bitrate={s.video_bitrate // 1000}kbps, "
            f"audio={s.audio_codec or 'none'} ({s.audio_sample_rate}Hz, {s.audio_channels}ch)"
        )

    # Step 4: Visually select unified crop zone on the best input's canvas
    crop_box = select_unified_crop_visually(
        specs_list=specs_list,
        best_spec=best_spec,
        explicit_crop=args.crop,
    )
    cx, cy, cw, ch = crop_box
    print(f"\n[crop] Confirmed unified crop zone: x={cx}, y={cy}, w={cw}, h={ch}")

    # Step 5: Choose output destination and run FFmpeg merge + crop
    output_path = select_output_path(best_spec.path.parent, args.output)
    build_and_run_ffmpeg(
        specs_list=specs_list,
        best_spec=best_spec,
        crop_box=crop_box,
        output_path=output_path,
        crf=args.crf,
        preset=args.preset,
        true_lossless=args.lossless,
    )

    print(f"\n[done] Successfully created merged & cropped video: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())