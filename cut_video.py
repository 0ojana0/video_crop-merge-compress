import os
import shutil
import subprocess
import tempfile
import tkinter as tk
from tkinter import filedialog


def parse_time(time_str: str) -> float:
    """সেকেন্ড (যেমন: 120) অথবা MM:SS / HH:MM:SS ফরম্যাটকে সেকেন্ডে রূপান্তর করে।"""
    parts = [float(p) for p in time_str.strip().split(":")]
    if len(parts) == 1:
        return parts[0]
    elif len(parts) == 2:
        return parts[0] * 60 + parts[1]
    elif len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"ভুল সময় ফরম্যাট: {time_str}")


def get_video_duration(file_path: str) -> float | None:
    """ffprobe ব্যবহার করে ভিডিওর মোট দৈর্ঘ্য (সেকেন্ডে) বের করে।"""
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            file_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception:
        return None


def parse_cut_intervals(raw_input: str) -> list[tuple[float, float]]:
    """'10-25, 60-95' ইনপুট থেকে সর্ট ও মার্জ করা বাদ দেওয়ার রেঞ্জ তৈরি করে।"""
    intervals = []
    for chunk in raw_input.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" not in chunk:
            raise ValueError(f"রেঞ্জের মাঝে '-' চিহ্ন নেই: '{chunk}'")
        start_str, end_str = chunk.split("-", 1)
        start, end = parse_time(start_str), parse_time(end_str)
        if end <= start:
            raise ValueError(f"শেষ সময় শুরুর চেয়ে বড় হতে হবে: '{chunk}'")
        intervals.append((start, end))

    # এলোমেলো বা ওভারল্যাপিং রেঞ্জগুলো সাজিয়ে একত্রিত করা
    intervals.sort(key=lambda x: x[0])
    merged = []
    for current in intervals:
        if not merged or current[0] > merged[-1][1]:
            merged.append(list(current))
        else:
            merged[-1][1] = max(merged[-1][1], current[1])
    return [(s, e) for s, e in merged]


def main():
    if not shutil.which("ffmpeg"):
        print("ত্রুটি: সিস্টেমে FFmpeg ইনস্টল করা নেই বা PATH-এ যুক্ত নেই।")
        return

    # ১. ফাইল সিলেক্টর ওপেন করা
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    input_path = filedialog.askopenfilename(
        title="ভিডিও ফাইল নির্বাচন করুন",
        filetypes=[
            ("Video Files", "*.mp4 *.mkv *.mov *.avi *.webm *.flv"),
            ("All Files", "*.*"),
        ],
    )
    root.destroy()

    if not input_path:
        print("কোনো ফাইল নির্বাচন করা হয়নি। প্রোগ্রাম বন্ধ হচ্ছে।")
        return

    print(f"নির্বাচিত ফাইল: {input_path}")

    # ২. বাদ দেওয়ার সেকেন্ড ইনপুট নেওয়া
    raw_cuts = input(
        "বাদ দেওয়ার সেকেন্ড লিখুন (যেমন: 15-30, 90-145 অথবা 1:30-2:25): "
    ).strip()
    if not raw_cuts:
        print("কোনো সময় দেওয়া হয়নি।")
        return

    try:
        cut_ranges = parse_cut_intervals(raw_cuts)
    except ValueError as e:
        print(f"ইনপুট ত্রুটি: {e}")
        return

    total_duration = get_video_duration(input_path)

    # ৩. যে অংশগুলো রাখতে হবে (keep_ranges) তা বের করা
    keep_ranges = []
    cursor = 0.0
    for cut_start, cut_end in cut_ranges:
        if cut_start > cursor:
            keep_ranges.append((cursor, cut_start))
        cursor = max(cursor, cut_end)

    if total_duration is None or cursor < total_duration - 0.1:
        keep_ranges.append((cursor, total_duration))

    if not keep_ranges:
        print("সম্পূর্ণ ভিডিওই বাদ পড়ে যাচ্ছে! কোনো অংশ অবশিষ্ট নেই।")
        return

    # ৪. সাময়িক ফোল্ডার তৈরি, ট্রিম, লিস্ট তৈরি ও মার্জ করা
    base_dir = os.path.dirname(input_path)
    name, ext = os.path.splitext(os.path.basename(input_path))
    output_path = os.path.join(base_dir, f"{name}_edited{ext}")

    temp_dir = tempfile.mkdtemp(prefix="ffmpeg_trim_")
    try:
        part_files = []
        for idx, (start, end) in enumerate(keep_ranges, start=1):
            part_path = os.path.join(temp_dir, f"part_{idx:03d}{ext}")
            cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", input_path]
            if end is not None:
                cmd.extend(["-t", str(end - start)])
            cmd.extend(["-c", "copy", part_path])

            print(f"অংশ {idx}/{len(keep_ranges)} কাটা হচ্ছে...")
            subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True
            )
            part_files.append(part_path)

        # list.txt তৈরি
        list_file_path = os.path.join(temp_dir, "list.txt")
        with open(list_file_path, "w", encoding="utf-8") as f:
            for p in part_files:
                safe_path = p.replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")

        # মার্জ করা
        print("অংশগুলো একসাথে মার্জ করা হচ্ছে...")
        merge_cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_file_path,
            "-c",
            "copy",
            output_path,
        ]
        subprocess.run(
            merge_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True
        )
        print(f"\nসম্পন্ন! নতুন ফাইল সেভ হয়েছে:\n{output_path}")

    except subprocess.CalledProcessError as e:
        print("\nFFmpeg চালানোর সময় ত্রুটি হয়েছে:")
        print(e.stderr.decode("utf-8", errors="ignore"))
    finally:
        # ৫. কাজ শেষে বা ত্রুটি হলেও সাময়িক ফোল্ডার ডিলিট করা
        shutil.rmtree(temp_dir, ignore_errors=True)
        print("সাময়িক ফোল্ডার ডিলিট করা হয়েছে।")


if __name__ == "__main__":
    main()