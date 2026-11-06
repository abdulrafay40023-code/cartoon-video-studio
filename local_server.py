import os
import re
import sys
import json
import math
import time
import base64
import shutil
import hashlib
import threading
import subprocess
import socketserver
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import imageio_ffmpeg
from faster_whisper import WhisperModel

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

from api.tts import handler as TtsHandler

RENDER_JOBS_ROOT = os.path.join(BASE_DIR, "local_data", "render_jobs")
WHISPER_CACHE_DIR = os.path.join(BASE_DIR, "local_data", "whisper_cache")
DOWNLOADS_DIR = os.path.join(os.path.expanduser("~"), "Downloads")
os.makedirs(RENDER_JOBS_ROOT, exist_ok=True)
os.makedirs(WHISPER_CACHE_DIR, exist_ok=True)

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

FONT_PATH = "C:/Windows/Fonts/arialbd.ttf"
if not os.path.exists(FONT_PATH):
    FONT_PATH = "C:/Windows/Fonts/segoeuib.ttf"
SUBTITLE_FONT = ImageFont.truetype(FONT_PATH, 44)

BGM_MP3_MAP = {
    "calmant": "Calmant.mp3",
    "dark_suspense": "Dark_Mystery_Bgm.mp3",
    "warm_docu": "Warm_Documentary_Bgm.mp3",
    "lofi_focus": "Soft_Lofi_Bgm.mp3",
    "horror_nightmare": "Horror_Nightmare_Bgm.mp3",
    "paranormal_dread": "Paranormal_Dread_Bgm.mp3",
    "crime_thriller": "Crime_Thriller_Bgm.mp3",
    "ancient_history": "Ancient_History_Bgm.mp3",
    "emotional_piano": "Emotional_Piano_Bgm.mp3",
    "scifi_cyber": "SciFi_Cyber_Bgm.mp3",
}

RENDER_JOBS_LOCK = threading.Lock()
RENDER_JOBS = {}
WHISPER_MODEL_LOCK = threading.Lock()
_WHISPER_MODEL = None
SUB_CACHE = {}


def get_whisper_model():
    global _WHISPER_MODEL
    with WHISPER_MODEL_LOCK:
        if _WHISPER_MODEL is None:
            _WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8", cpu_threads=8)
        return _WHISPER_MODEL


def sanitize_user_key(raw_key: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_\-]+", "_", str(raw_key or "default").strip().lower()).strip("_")
    return clean or "default"


def get_user_job_dir(user_key: str) -> str:
    uk = sanitize_user_key(user_key)
    d = os.path.join(RENDER_JOBS_ROOT, uk)
    os.makedirs(d, exist_ok=True)
    return d


def save_job_state(user_key: str, state: dict):
    uk = sanitize_user_key(user_key)
    with RENDER_JOBS_LOCK:
        RENDER_JOBS[uk] = state
        status_path = os.path.join(get_user_job_dir(uk), "status.json")
        try:
            with open(status_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except Exception:
            pass


def load_job_state(user_key: str) -> dict:
    uk = sanitize_user_key(user_key)
    with RENDER_JOBS_LOCK:
        if uk in RENDER_JOBS:
            return dict(RENDER_JOBS[uk])
        status_path = os.path.join(get_user_job_dir(uk), "status.json")
        if os.path.exists(status_path):
            try:
                with open(status_path, "r", encoding="utf-8") as f:
                    st = json.load(f)
                RENDER_JOBS[uk] = st
                return dict(st)
            except Exception:
                pass
    return {"status": "idle", "pct": 0, "stage": ""}


def update_job_progress(user_key: str, pct: float, stage: str, extra: dict = None):
    uk = sanitize_user_key(user_key)
    st = load_job_state(uk)
    st["pct"] = round(float(pct), 1)
    st["stage"] = str(stage)
    st["updated_at"] = time.time()
    if extra:
        st.update(extra)
    save_job_state(uk, st)


def norm_word(w: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(w or "").lower())


def word_sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    num_map = {
        "100000": "hundredthousand", "100k": "hundredthousand", "hundred": "100", "thousand": "000",
        "20": "twenty", "70000": "seventythousand", "70": "seventy",
        "140000": "hundredandfortythousand", "140": "forty", "forty": "40",
        "20000": "twentythousand", "11": "eleven", "90": "ninety", "847": "eightfortyseven",
        "12": "twelve", "83": "eightythree", "3": "three", "4000": "fourthousand",
        "4": "four", "10": "ten", "14": "fourteen", "2": "two", "5": "five",
        "6": "six", "7": "seven", "8": "eight", "9": "nine", "1": "one",
        "30": "thirty", "50": "fifty", "60": "sixty", "80": "eighty",
        "500": "fivehundred", "300": "threehundred", "200": "twohundred",
        "1500": "fifteenthousand", "2000": "twothousand", "10000": "tenthousand",
    }
    if num_map.get(a) == b or num_map.get(b) == a:
        return 0.92
    if len(a) >= 4 and len(b) >= 4 and (a[:4] == b[:4] or a in b or b in a):
        return 0.85
    if len(a) >= 3 and len(b) >= 3 and a[:3] == b[:3]:
        return 0.55
    return 0.0


def align_scenes_dp(scene_scripts: list, whisper_words: list, total_dur: float) -> list:
    n_scenes = len(scene_scripts)
    W = len(whisper_words)
    if W == 0 or n_scenes <= 0:
        return []

    # If we have only 1 scene
    if n_scenes == 1:
        txt = str(scene_scripts[0] or "").strip()
        sub_parts = []
        for gi in range(0, W, 4):
            grp = whisper_words[gi:gi+4]
            sub_parts.append({
                "start": grp[0]["start"],
                "end": grp[-1]["end"],
                "text": " ".join(x["word"] for x in grp).strip()
            })
        return [{
            "scene_num": 1,
            "start_sec": 0.0,
            "end_sec": total_dur,
            "sub_start": whisper_words[0]["start"],
            "sub_end": whisper_words[-1]["end"],
            "text": txt,
            "sub_parts": sub_parts
        }]

    w_norms = [norm_word(x["word"]) for x in whisper_words]

    # Pre-tokenize all scene scripts
    sc_tokens = []
    for sc in scene_scripts:
        toks = [norm_word(t) for t in str(sc or "").split() if norm_word(t)]
        sc_tokens.append(toks if toks else ["scene"])

    # Sequential search for scene boundaries with lookahead
    # Each scene i matches its tokens to whisper_words starting from cur_w
    bounds = [0] * (n_scenes + 1)
    bounds[0] = 0
    bounds[n_scenes] = W

    cur_w = 0
    for i in range(n_scenes - 1):
        toks = sc_tokens[i]
        rem_scenes = n_scenes - i
        rem_words = W - cur_w
        expected_len = max(1, round(rem_words / float(rem_scenes)))
        
        last_tok = toks[-1] if toks else ""
        second_last_tok = toks[-2] if len(toks) >= 2 else ""

        # Search window for end of scene i
        search_start = max(cur_w, cur_w + len(toks) - 5)
        search_end = min(W - (n_scenes - 1 - i), cur_w + max(len(toks) * 2, expected_len * 2) + 8)

        best_j = min(W - (n_scenes - 1 - i), max(cur_w, cur_w + len(toks) - 1))
        best_score = -1e9

        for j in range(search_start, search_end + 1):
            if j >= W:
                break
            score = 0.0
            
            # Match last token
            if last_tok and w_norms[j] == last_tok:
                score += 8.0
            elif last_tok and len(last_tok) >= 4 and (last_tok in w_norms[j] or w_norms[j] in last_tok):
                score += 5.0
                
            # Match second last token
            if j > 0 and second_last_tok and w_norms[j-1] == second_last_tok:
                score += 4.0

            # Pause bonus after punctuation or acoustic gap
            gap = max(0.0, whisper_words[j]["start"] - whisper_words[j - 1]["end"]) if j > 0 else 0.0
            score += min(3.0, gap * 4.0)
            prev_w = str(whisper_words[j]["word"] or "")
            if prev_w.endswith((".", "!", "?", '."', '!"', '?"')):
                score += 2.5
            elif prev_w.endswith((",", ";", ":", "—", "–")):
                score += 1.2

            # Distance penalty from expected length
            dist_pen = abs((j - cur_w + 1) - len(toks)) * 0.35
            score -= dist_pen

            if score > best_score:
                best_score = score
                best_j = j

        bounds[i + 1] = best_j + 1
        cur_w = best_j + 1

    timeline = []
    for i in range(n_scenes):
        w0 = bounds[i]
        w1 = max(w0 + 1, bounds[i + 1])
        slice_w = whisper_words[w0:w1]

        prev_end = 0.0 if i == 0 else timeline[i - 1]["end_sec"]
        next_start = total_dur if i == n_scenes - 1 else whisper_words[bounds[i + 1]]["start"]
        cur_end = slice_w[-1]["end"] if slice_w else next_start

        st_sec = round(prev_end, 3)
        en_sec = round(
            total_dur
            if i == n_scenes - 1
            else min(total_dur, max(st_sec + 0.30, 0.5 * (cur_end + next_start))),
            3,
        )

        # Build strict 4-word subtitles from the actual spoken words in this scene
        sub_parts = []
        if slice_w:
            for gi in range(0, len(slice_w), 4):
                grp = slice_w[gi:gi+4]
                sub_parts.append({
                    "start": round(grp[0]["start"], 3),
                    "end": round(grp[-1]["end"], 3),
                    "text": " ".join(x["word"] for x in grp).strip()
                })

        exact_script = str(scene_scripts[i] or "").strip()
        timeline.append({
            "scene_num": i + 1,
            "start_sec": st_sec,
            "end_sec": en_sec,
            "sub_start": sub_parts[0]["start"] if sub_parts else st_sec,
            "sub_end": sub_parts[-1]["end"] if sub_parts else en_sec,
            "text": exact_script if exact_script else (" ".join(x["word"] for x in slice_w)),
            "sub_parts": sub_parts,
        })

    return timeline


def build_pause_grouped_scripts(whisper_words: list, n_scenes: int) -> list:
    W = len(whisper_words)
    if W <= 0 or n_scenes <= 0:
        return [""] * max(1, n_scenes)
    if W <= n_scenes:
        out = []
        for i in range(n_scenes):
            w = whisper_words[min(W - 1, i)]["word"]
            out.append(w)
        return out

    break_bonus = [0.0] * W
    for wi in range(W - 1):
        w_str = str(whisper_words[wi]["word"] or "").strip()
        gap = max(0.0, whisper_words[wi + 1]["start"] - whisper_words[wi]["end"])
        b = gap * 4.0
        if w_str.endswith((".", "!", "?", '."', '!"', '?"')):
            b += 2.5
        elif w_str.endswith((",", ";", ":", "—", "–")):
            b += 1.4
        break_bonus[wi] = b

    bounds = [0] * (n_scenes + 1)
    bounds[0] = 0
    bounds[n_scenes] = W
    for k in range(1, n_scenes):
        prev_w = bounds[k - 1]
        rem_scenes = n_scenes - k + 1
        rem_words = W - prev_w
        ideal_w = prev_w + max(1, round(rem_words / float(rem_scenes)))
        s_min = max(prev_w + 1, ideal_w - 5)
        s_max = min(W - (n_scenes - k), ideal_w + 5)
        best_w = min(W - (n_scenes - k), max(prev_w + 1, ideal_w))
        best_val = -1e9
        for cand in range(s_min, s_max + 1):
            score = break_bonus[cand - 1] - abs(cand - ideal_w) * 0.42
            if score > best_val:
                best_val = score
                best_w = cand
        bounds[k] = best_w

    scripts = []
    for i in range(n_scenes):
        w0 = bounds[i]
        w1 = max(w0 + 1, bounds[i + 1])
        scripts.append(" ".join(x["word"] for x in whisper_words[w0:w1]).strip())
    return scripts


def calculate_zoom(t: float, T: float) -> float:
    # High-Definition Anti-Aliased Zoom (Max 1.06 to preserve 100% native image pixels without blurring/stretching)
    z_in_dur = min(1.2, T * 0.32)
    hold_dur = max(0.5, T - 2 * z_in_dur)
    z_out_dur = z_in_dur

    t_in_end = z_in_dur
    t_hold_end = t_in_end + hold_dur
    t_out_end = t_hold_end + z_out_dur

    if z_in_dur <= 0.05:
        return 1.00
    if t <= t_in_end:
        p = t / z_in_dur
        ease = 0.5 * (1.0 - math.cos(math.pi * p))
        return 1.00 + 0.06 * ease
    elif t <= t_hold_end:
        return 1.06
    elif t <= t_out_end:
        p = (t - t_hold_end) / z_out_dur
        ease = 0.5 * (1.0 - math.cos(math.pi * p))
        return 1.06 - 0.06 * ease
    else:
        return 1.00


def get_subtitle_data(text: str):
    if not text or not text.strip():
        return None
    raw_words = text.strip().split()
    # Strictly enforce max 4 words per subtitle card burst
    clean_text = " ".join(raw_words[:4])
    if clean_text in SUB_CACHE:
        return SUB_CACHE[clean_text]

    overlay = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    words = clean_text.split()
    bbox = draw.textbbox((0, 0), clean_text, font=SUBTITLE_FONT)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    pad_x, pad_y = 32, 16
    box_w = min(1800, text_w + pad_x * 2)
    box_h = text_h + pad_y * 2
    box_x0 = max(20, int((1920 - box_w) / 2))
    box_y0 = max(20, int(1080 - box_h - 75))
    box_x1 = min(1900, box_x0 + box_w)
    box_y1 = min(1060, box_y0 + box_h)

    # Modern dark rounded pill (85% opacity)
    draw.rounded_rectangle([box_x0, box_y0, box_x1, box_y1], radius=16, fill=(0, 0, 0, 210))

    cur_x = (1920 - text_w) / 2
    cur_y = box_y0 + pad_y - 2
    for wi, w in enumerate(words):
        w_str = w + (" " if wi < len(words) - 1 else "")
        wb = draw.textbbox((0, 0), w_str, font=SUBTITLE_FONT)
        w_len = wb[2] - wb[0]
        # First word / punch in vibrant yellow (#FACC15), rest pure white (#FFFFFF)
        text_color = (250, 204, 21, 255) if wi == 0 or len(words) <= 2 else (255, 255, 255, 255)
        for dx, dy in ((-2,0), (2,0), (0,-2), (0,2), (1,1), (-1,-1)):
            draw.text((cur_x + dx, cur_y + dy), w_str, font=SUBTITLE_FONT, fill=(0, 0, 0, 255))
        draw.text((cur_x, cur_y), w_str, font=SUBTITLE_FONT, fill=text_color)
        cur_x += w_len

    rgba = np.array(overlay)
    roi_rgba = rgba[box_y0:box_y1, box_x0:box_x1]
    roi_bgr = cv2.cvtColor(roi_rgba, cv2.COLOR_RGBA2BGR).astype(np.float32)
    roi_alpha = (roi_rgba[:, :, 3:4].astype(np.float32) / 255.0)

    res = {
        "box": (box_x0, box_y0, box_x1, box_y1),
        "bgr": roi_bgr,
        "alpha": roi_alpha,
        "inv_alpha": (1.0 - roi_alpha),
    }
    SUB_CACHE[clean_text] = res
    return res


def imread_unicode(img_path: str):
    try:
        stream = np.fromfile(img_path, dtype=np.uint8)
        img = cv2.imdecode(stream, cv2.IMREAD_COLOR)
        if img is not None:
            return img
    except Exception:
        pass
    pil_img = Image.open(img_path).convert("RGB")
    return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)


def blend_sub(frame, sub_data):
    if sub_data is None:
        return frame
    out = frame.copy()
    bx0, by0, bx1, by1 = sub_data["box"]
    roi = out[by0:by1, bx0:bx1].astype(np.float32)
    out[by0:by1, bx0:bx1] = (roi * sub_data["inv_alpha"] + sub_data["bgr"] * sub_data["alpha"]).astype(np.uint8)
    return out


def render_single_scene_clip(task: dict):
    idx = task["idx"]
    user_key = task["user_key"]
    job_id = task["job_id"]
    st = load_job_state(user_key)
    if st.get("cancelled") or st.get("job_id") != job_id:
        return idx

    out_clip = task["out_clip"]
    img_path = task["img_path"]
    st_sec = task["start_sec"]
    num_frames = task["num_frames"]
    has_zoom = task["has_zoom"]
    sub_parts = task["sub_parts"]
    fps = 25
    duration = num_frames / float(fps)

    base_img = imread_unicode(img_path)
    if base_img.shape[:2] != (1080, 1920):
        base_img = cv2.resize(base_img, (1920, 1080), interpolation=cv2.INTER_LANCZOS4)

    cmd = [
        FFMPEG_EXE, "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", "1920x1080",
        "-pix_fmt", "bgr24",
        "-r", str(fps),
        "-i", "-",
        "-frames:v", str(num_frames),
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        out_clip,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    last_key = None
    last_bytes = None

    for f_idx in range(num_frames):
        local_t = f_idx / float(fps)
        abs_t = st_sec + local_t
        zoom = round(calculate_zoom(local_t, duration), 5) if has_zoom else 1.00000

        active_sub = ""
        for sp in sub_parts:
            if sp["start"] - 0.02 <= abs_t <= sp["end"] + 0.02:
                active_sub = sp["text"]
                break

        key = (zoom, active_sub)
        if key == last_key and last_bytes is not None:
            frame_bytes = last_bytes
        else:
            if abs(zoom - 1.00) < 0.0001:
                zoomed = base_img
            else:
                M = np.float32([
                    [zoom, 0.0, 960.0 * (1.0 - zoom)],
                    [0.0, zoom, 540.0 * (1.0 - zoom)],
                ])
                zoomed = cv2.warpAffine(base_img, M, (1920, 1080), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)

            sub_data = get_subtitle_data(active_sub) if active_sub else None
            final_frame = blend_sub(zoomed, sub_data)
            frame_bytes = final_frame.tobytes()
            last_key = key
            last_bytes = frame_bytes

        try:
            proc.stdin.write(frame_bytes)
        except Exception:
            break

    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait()
    return idx


def run_background_video_render(user_key: str, job_id: str):
    uk = sanitize_user_key(user_key)
    job_dir = get_user_job_dir(uk)
    images_dir = os.path.join(job_dir, "images")
    clips_dir = os.path.join(job_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    try:
        st = load_job_state(uk)
        if st.get("job_id") != job_id:
            return

        vo_path = st.get("vo_path", "")
        if not vo_path or not os.path.exists(vo_path):
            update_job_progress(uk, 0, "❌ Error: Voiceover audio file missing.", {"status": "error", "error": "Voiceover audio file missing."})
            return

        # Gather ordered scene images
        scene_files = sorted(
            [f for f in os.listdir(images_dir) if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))],
            key=lambda x: int(re.search(r"(\d+)", x).group(1)) if re.search(r"(\d+)", x) else 999999
        )
        n_scenes = len(scene_files)
        if n_scenes == 0:
            update_job_progress(uk, 0, "❌ Error: No scene images uploaded.", {"status": "error", "error": "No scene images uploaded."})
            return

        # 1. Probe exact voiceover duration
        update_job_progress(uk, 8, f"🎧 Probing Voiceover Audio & Preparing {n_scenes} Scenes...", {"status": "rendering"})
        r_probe = subprocess.run([FFMPEG_EXE, "-i", vo_path], capture_output=True, text=True)
        dur_m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r_probe.stderr)
        total_dur = 60.0
        if dur_m:
            total_dur = int(dur_m.group(1)) * 3600 + int(dur_m.group(2)) * 60 + float(dur_m.group(3))

        # 2. Run or load cached faster-whisper word-level transcription
        with open(vo_path, "rb") as vf:
            vo_sha = hashlib.sha256(vf.read()).hexdigest()[:24]
        whisper_cache_path = os.path.join(WHISPER_CACHE_DIR, f"whisper_{vo_sha}.json")

        if os.path.exists(whisper_cache_path):
            with open(whisper_cache_path, "r", encoding="utf-8") as wf:
                whisper_words = json.load(wf)
            update_job_progress(uk, 16, f"⚡ Loaded {len(whisper_words)} Exact Word Timestamps from Cache...")
        else:
            update_job_progress(uk, 11, "🎧 AI Faster-Whisper Listening to Voiceover & Extracting Exact Word Timestamps...")
            model = get_whisper_model()
            segments, _ = model.transcribe(vo_path, word_timestamps=True, vad_filter=False)
            whisper_words = []
            for seg in segments:
                if seg.words:
                    for w in seg.words:
                        wt = str(w.word or "").strip()
                        if wt:
                            whisper_words.append({
                                "word": wt,
                                "start": round(float(w.start), 3),
                                "end": round(float(w.end), 3),
                            })
            with open(whisper_cache_path, "w", encoding="utf-8") as wf:
                json.dump(whisper_words, wf, ensure_ascii=False)
            update_job_progress(uk, 17, f"✅ Transcribed {len(whisper_words)} Spoken Words ({total_dur / 60.0:.1f} mins)!")

        st = load_job_state(uk)
        if st.get("cancelled") or st.get("job_id") != job_id:
            update_job_progress(uk, 0, "🛑 Video rendering cancelled by user.", {"status": "cancelled"})
            return

        # 3. Align scenes via Viterbi DP
        scene_scripts = st.get("scene_scripts") or []
        non_empty_cnt = sum(1 for s in scene_scripts if str(s or "").strip())
        if len(scene_scripts) != n_scenes or non_empty_cnt < max(1, n_scenes // 2):
            scene_scripts = build_pause_grouped_scripts(whisper_words, n_scenes)

        update_job_progress(uk, 20, f"🔗 Locking {n_scenes} Images & 4-Word Subtitles to Spoken Voiceover (Viterbi DP)...")
        timeline = align_scenes_dp(scene_scripts, whisper_words, total_dur)

        # Pre-populate subtitle image cache
        for item in timeline:
            for sp in item["sub_parts"]:
                get_subtitle_data(sp["text"])

        # 4. Prepare 25fps scene clip tasks
        fps = 25
        tasks = []
        time_since_last_zoom = 10.0
        cum_frames = 0
        for i, item in enumerate(timeline):
            s_num = i + 1
            img_p = os.path.join(images_dir, scene_files[i])
            target_end_frames = round(item["end_sec"] * fps)
            n_frames = max(5, target_end_frames - cum_frames)
            cum_frames += n_frames
            dur = n_frames / float(fps)

            has_zoom = False
            if time_since_last_zoom >= 10.0:
                has_zoom = True
                time_since_last_zoom = 0.0
            else:
                time_since_last_zoom += dur

            out_clip = os.path.join(clips_dir, f"clip_{s_num:04d}.mp4")
            if os.path.exists(out_clip):
                try:
                    os.remove(out_clip)
                except Exception:
                    pass
            tasks.append({
                "idx": s_num,
                "user_key": uk,
                "job_id": job_id,
                "img_path": img_p,
                "start_sec": item["start_sec"],
                "num_frames": n_frames,
                "has_zoom": has_zoom,
                "sub_parts": item["sub_parts"],
                "out_clip": out_clip,
            })

        update_job_progress(uk, 22, f"🎬 Rendering HD Scene 1 / {n_scenes} (25fps • 4-Word Synced Subtitles • Smooth Zoom)...")
        done_cnt = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            for _ in ex.map(render_single_scene_clip, tasks):
                st_now = load_job_state(uk)
                if st_now.get("cancelled") or st_now.get("job_id") != job_id:
                    update_job_progress(uk, 0, "🛑 Video rendering cancelled by user.", {"status": "cancelled"})
                    return
                done_cnt += 1
                pct = 22.0 + (done_cnt / float(n_scenes)) * 70.0
                update_job_progress(
                    uk,
                    pct,
                    f"🎬 Rendering HD Scenes with 4-Word Synced Subtitles & Smooth Zoom ({done_cnt} / {n_scenes})..."
                )

        # 5. Concatenate clips + Mux Voiceover + Selected BGM
        update_job_progress(uk, 94, "🎵 Mixing Voiceover Audio + Background Music & Finalizing 1080p MP4...")
        concat_txt = os.path.join(clips_dir, "concat_list.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            for t in tasks:
                p_clean = t["out_clip"].replace("\\", "/")
                f.write(f"file '{p_clean}'\n")

        raw_out_name = str(st.get("out_file_name") or "Finance_Story_Video.mp4").strip()
        clean_out_name = re.sub(r"[^a-zA-Z0-9_\-\.]+", "_", raw_out_name)
        if not clean_out_name.lower().endswith(".mp4"):
            clean_out_name = os.path.splitext(clean_out_name)[0] + ".mp4"

        final_mp4_path = os.path.join(job_dir, clean_out_name)
        bgm_track = str(st.get("bgm_track") or "calmant").strip().lower()
        bgm_file = BGM_MP3_MAP.get(bgm_track, "Calmant.mp3")
        bgm_path = os.path.join(BASE_DIR, bgm_file) if bgm_track != "none" else ""

        if bgm_path and os.path.exists(bgm_path):
            fade_out_st = max(4.0, total_dur - 4.0)
            af_filter = (
                f"[1:a]volume=1.0[vo];"
                f"[2:a]volume=0.09,afade=t=in:st=0:d=3,afade=t=out:st={fade_out_st:.2f}:d=4[bgm];"
                f"[vo][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            )
            mux_cmd = [
                FFMPEG_EXE, "-y",
                "-f", "concat", "-safe", "0", "-i", concat_txt,
                "-i", vo_path,
                "-stream_loop", "-1", "-i", bgm_path,
                "-filter_complex", af_filter,
                "-map", "0:v:0", "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                "-shortest",
                "-movflags", "+faststart",
                final_mp4_path,
            ]
        else:
            mux_cmd = [
                FFMPEG_EXE, "-y",
                "-f", "concat", "-safe", "0", "-i", concat_txt,
                "-i", vo_path,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                "-shortest",
                "-movflags", "+faststart",
                final_mp4_path,
            ]

        subprocess.run(mux_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Also copy final MP4 directly to user's Downloads folder on host PC
        downloads_copy_path = os.path.join(DOWNLOADS_DIR, clean_out_name)
        try:
            shutil.copy2(final_mp4_path, downloads_copy_path)
        except Exception:
            pass

        size_mb = f"{os.path.getsize(final_mp4_path) / (1024 * 1024):.2f}"
        dur_mins = f"{total_dur / 60.0:.2f}"
        video_url = f"/local_data/render_jobs/{uk}/{clean_out_name}?t={int(time.time())}"
        full_transcribed = " ".join(x["word"] for x in whisper_words).strip()

        update_job_progress(
            uk,
            100.0,
            f"✅ Completed! Saved to Downloads/{clean_out_name} & Ready to Watch/Download.",
            {
                "status": "completed",
                "video_url": video_url,
                "file_name": clean_out_name,
                "size_mb": size_mb,
                "duration_mins": dur_mins,
                "scenes_count": n_scenes,
                "downloads_path": downloads_copy_path,
                "transcribed_script": full_transcribed,
            },
        )
    except Exception as e:
        update_job_progress(
            uk,
            0,
            f"❌ Render Error: {e}",
            {"status": "error", "error": str(e)},
        )


class StudioLocalHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/local_render_status":
            qs = parse_qs(parsed.query)
            user_key = (qs.get("user_key") or qs.get("user") or ["default"])[0]
            st = load_job_state(user_key)
            # Exclude large internal arrays from status poll response
            resp = {k: v for k, v in st.items() if k not in ("scene_scripts",)}
            resp["ok"] = True
            self._send_json(200, resp)
            return
        super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/tts"):
            TtsHandler.do_POST(self)
            return

        if self.path.startswith("/api/local_render_"):
            content_len = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(content_len) if content_len > 0 else b"{}"
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except Exception as e:
                self._send_json(400, {"ok": False, "error": f"Invalid JSON: {e}"})
                return

            user_key = sanitize_user_key(body.get("user_key") or body.get("user") or "default")
            job_dir = get_user_job_dir(user_key)
            images_dir = os.path.join(job_dir, "images")
            clips_dir = os.path.join(job_dir, "clips")

            if self.path.startswith("/api/local_render_init"):
                job_id = str(body.get("job_id") or f"render_{int(time.time() * 1000)}")
                # Clean previous images/clips for fresh job
                for d in (images_dir, clips_dir):
                    if os.path.exists(d):
                        shutil.rmtree(d, ignore_errors=True)
                    os.makedirs(d, exist_ok=True)

                state = {
                    "ok": True,
                    "job_id": job_id,
                    "user_key": user_key,
                    "status": "uploading",
                    "cancelled": False,
                    "pct": 2.0,
                    "stage": "📦 Preparing Scene Images & Voiceover for Localhost HD Render...",
                    "folder_name": str(body.get("folder_name") or "Finance_Story"),
                    "out_file_name": str(body.get("out_file_name") or "Finance_Story_Video.mp4"),
                    "bgm_track": str(body.get("bgm_track") or "calmant"),
                    "title": str(body.get("title") or body.get("folder_name") or "Finance Story Video"),
                    "total_scenes": int(body.get("total_scenes") or 0),
                    "scene_scripts": list(body.get("scene_scripts") or []),
                    "vo_path": "",
                    "video_url": "",
                    "started_at": time.time(),
                    "updated_at": time.time(),
                }
                save_job_state(user_key, state)
                self._send_json(200, {"ok": True, "job_id": job_id, "status": "uploading"})
                return

            if self.path.startswith("/api/local_render_upload"):
                st = load_job_state(user_key)
                os.makedirs(images_dir, exist_ok=True)
                asset_type = str(body.get("asset_type") or "")
                if asset_type == "voiceover":
                    vo_b64 = str(body.get("voiceover_b64") or "")
                    if "," in vo_b64:
                        vo_b64 = vo_b64.split(",", 1)[1]
                    ext = str(body.get("voiceover_ext") or ".wav")
                    if not ext.startswith("."):
                        ext = "." + ext
                    vo_path = os.path.join(job_dir, f"voiceover_input{ext}")
                    with open(vo_path, "wb") as vf:
                        vf.write(base64.b64decode(vo_b64))
                    st["vo_path"] = vo_path
                    save_job_state(user_key, st)
                    self._send_json(200, {"ok": True})
                    return

                if asset_type == "scenes":
                    scenes = body.get("scenes") or []
                    for sc in scenes:
                        s_num = int(sc.get("scene_num") or 1)
                        ext = str(sc.get("ext") or ".png").lower()
                        if not ext.startswith("."):
                            ext = "." + ext
                        b64_str = str(sc.get("b64") or "")
                        if "," in b64_str:
                            b64_str = b64_str.split(",", 1)[1]
                        img_out = os.path.join(images_dir, f"scene_{s_num:04d}{ext}")
                        with open(img_out, "wb") as imf:
                            imf.write(base64.b64decode(b64_str))
                    uploaded_cnt = int(body.get("uploaded_so_far") or len(os.listdir(images_dir)))
                    tot_cnt = max(1, int(st.get("total_scenes") or uploaded_cnt))
                    pct = 2.0 + (uploaded_cnt / float(tot_cnt)) * 5.0
                    update_job_progress(user_key, pct, f"📦 Loading Scene Images ({uploaded_cnt} / {tot_cnt})...")
                    self._send_json(200, {"ok": True, "uploaded": uploaded_cnt})
                    return

                self._send_json(400, {"ok": False, "error": "Unknown asset_type"})
                return

            if self.path.startswith("/api/local_render_start"):
                st = load_job_state(user_key)
                job_id = str(body.get("job_id") or st.get("job_id") or "")
                st["status"] = "rendering"
                st["cancelled"] = False
                save_job_state(user_key, st)
                t = threading.Thread(target=run_background_video_render, args=(user_key, job_id), daemon=True)
                t.start()
                self._send_json(200, {"ok": True, "job_id": job_id, "status": "rendering"})
                return

            if self.path.startswith("/api/local_render_status"):
                st = load_job_state(user_key)
                resp = {k: v for k, v in st.items() if k not in ("scene_scripts",)}
                resp["ok"] = True
                self._send_json(200, resp)
                return

            if self.path.startswith("/api/local_render_cancel"):
                st = load_job_state(user_key)
                st["cancelled"] = True
                st["status"] = "cancelled"
                st["stage"] = "🛑 Video rendering cancelled by user."
                st["pct"] = 0
                save_job_state(user_key, st)
                self._send_json(200, {"ok": True, "status": "cancelled"})
                return

        self.send_error(404, "Endpoint not found")

    def _send_json(self, status_code: int, payload: dict):
        TtsHandler._send_json(self, status_code, payload)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"Video Studio Localhost running at: http://localhost:{port} (and Office LAN http://10.0.0.34:{port})")
    with ThreadedHTTPServer(("0.0.0.0", port), StudioLocalHandler) as httpd:
        httpd.serve_forever()
