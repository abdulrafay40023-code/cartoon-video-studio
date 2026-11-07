import asyncio
import base64
import concurrent.futures
from http.server import BaseHTTPRequestHandler
import io
import json
import os
import random
import re
import time
import urllib.request
import urllib.error
import wave
try:
    import edge_tts
except ImportError:
    edge_tts = None

_DEFAULT_GEMINI_KEYS_B64 = "QVEuQWI4Uk42TERWRXVHd1FEY2twOUlzbkVHN09LYVpTcUhMLWE2TndfTDViVFI1d3BtakEKQVEuQWI4Uk42TE03eXVxZFpmaUFLVGlUTEY3VlFNMGd6MjR5YXgzb3JacC14NWJOSGtVVHcKQVEuQWI4Uk42SlNSSUs1T0ozUHR6SkVreF9oV21YUmlvUExEYXdJSFV4NjU1LTQ1SHZLclEKQVEuQWI4Uk42SWNWaU1BcTNXalNqWFhlX0ZhLTk0U0ItVzVuRWpJRGJUWWVKTFMwQkhGWHcKQVEuQWI4Uk42S1RIWG5jakc1dlB2WmxVVldZWTJJd1hiUWc0ZU8yQVY5ZzRkWFBqMFNuOGcKQVEuQWI4Uk42SXNLMUY2Yk1raG1xWkZKUTE0Zi1zcE9CU3pDME4zbW5fdmZDaUxmTld4c0EKQVEuQWI4Uk42TDJsdmdlSlprYjluc0pWQldZVnp6Wk14eW9yNFRLTWlBc0hHMVdWQmVaX0EKQVEuQWI4Uk42Si1mZTlCU1VQSC1ST0RMRzM3Zjl4azFoRS04LVZKZzJxeGozem5LQ2lqaHcKQVEuQWI4Uk42TFpPekVydzhoclA5a1lCeFZZZ1llNklvSFRuMzZIVk10ZzA5SmdGTHk4Q3c="

def _load_gemini_keys():
    env_k = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY")
    if env_k:
        keys = [k.strip() for k in env_k.replace(",", "\n").split() if k.strip()]
        if keys:
            return keys
    try:
        return [k.strip() for k in base64.b64decode(_DEFAULT_GEMINI_KEYS_B64).decode("utf-8").split() if k.strip()]
    except Exception:
        return []

GEMINI_API_KEYS = _load_gemini_keys()

GEMINI_TTS_MODELS = [
    "gemini-3.8-flash-tts",
    "gemini-3.8-flash-lite-tts",
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-preview-tts",
]

GEMINI_MODELS = [
    "gemini-3.1-flash-lite-preview",
    "gemini-3-flash-preview",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]

REGISTRY_FILE = "/tmp/video_studio_users_registry.json"

MASTER_PROMPT_HEADER = """# Full Cinematic Story Image Prompt — (Dynamic 1–2s, 3–4s & 5–6s Spoken-to-Visual Scene Breakdown — 16:9 LANDSCAPE)

IMPORTANT: Use the uploaded reference images as permanent visual references throughout the entire project.
REFERENCE 1, 2, 3 = MAIN CHARACTER REFERENCE (same person, different outfits).
REFERENCE 4 = YOUTUBE CHANNEL / VISUAL STYLE REFERENCE.
Generate every image in **16:9 landscape orientation** (standard long-form YouTube frame, NOT vertical/shorts).

## MAIN CHARACTER CONSISTENCY — EXTREMELY IMPORTANT
The main protagonist ("you") must remain EXACTLY the same character throughout the entire story. Always preserve:
- Same white/pale skin tone
- Same face shape & simple cartoon facial construction
- Same large simple black button-like oval/dot eyes (NEVER realistic, anime-detailed, colored, or glowing)
- Same thin natural eyebrows, simple nose, and friendly subtle smile style
- Same dark brown wavy/curly hair, medium volume, same hairline
- Same body proportions and height
- Same clean black ink outline; same 2D illustrated character over detailed semi-realistic painted backgrounds, matching the uploaded character references.
DO NOT redesign the protagonist from scene to scene. DO NOT change his facial structure, eyes, skin, or hairstyle randomly.

## OTHER CHARACTERS — keep each visually identical every time they appear:
All secondary characters (family members, coworkers, mentors, managers, friends, clients) must use the same simple 2D cartoon face style and clean black ink outlines as the protagonist, with natural normal skin tones and consistent hair/clothing every time they reappear.

## VISUAL STYLE
Polished 2D digital illustration, flat cartoon character faces over detailed semi-realistic painted backgrounds, clean black/ink outlines, warm cinematic lighting, soft cel shading, storytelling-focused composition with foreground/midground/background separation, premium illustrated documentary feel, warm natural color palette, matching REFERENCE 4.

## COMPOSITION (16:9)
Wide 16:9 landscape framing, horizontal breathing room, strong foreground/background separation for slow pans/push-ins, faces and key objects clearly visible, no text in-frame unless a specific number/sign is mentioned in the script.

## CLOTHING CONTINUITY
Same day/scene = same outfit. Face/hair/eyes/skin/body always identical:
- Early / Starting Period: brown sweater, black pants, white sneakers (REFERENCE 2).
- Work / Growth Period: navy sweater, tan chinos, white sneakers (REFERENCE 1) or navy collared polo, khaki pants.
- Present-Day / Mature Period: denim jacket over a gray t-shirt, dark jeans, white sneakers (REFERENCE 3).

## NEGATIVE RULES
No character redesign, no different face/hairstyle/eyes, no realistic/colored eyes, no random clothing changes mid-scene, no photorealism, no 3D CGI, no distorted hands/extra fingers, no duplicated people, no deformed faces, no watermark, no unrequested text.
"""


def get_beat_duration_info(beat_text: str) -> tuple:
    """
    Returns (est_seconds, duration_bucket_label) strictly calibrated to 140 words/min (2.333 words/sec):
    - 1-2s for short punchy visual beats (2-5 words)
    - 3-4s for standard visual beats (6-9 words -> ~2.6s to 3.9s)
    - 4-5s for slightly longer descriptive beats (10+ words)
    """
    words = [w for w in beat_text.strip().split() if w]
    w_count = len(words)
    est_sec = round(max(1.5, min(5.0, w_count / 2.333)), 1)
    if w_count <= 5 or est_sec <= 2.2:
        bucket = "1–2s"
    elif w_count <= 9 or est_sec <= 4.0:
        bucket = "3–4s"
    else:
        bucket = "4–5s"
    return est_sec, bucket


def _split_long_clause_into_3_4s_chunks(text: str) -> list:
    """
    Splits any clause > 14 words into crisp 9-to-13 word visual beats (~3.5-4.0 seconds each at normal ~180 WPM voiceover pace)
    so a 10-minute voiceover produces ~155-165 images (1 image every 3.5-4.0 seconds):
    """
    words = text.strip().split()
    w_len = len(words)
    if w_len <= 14:
        return [text.strip()] if text.strip() else []

    # Try splitting on natural phrase connectors first
    sub_parts = re.split(
        r"(?<=[,;—–:])\s+|\s+(?=(?:and|but|while|when|because|so|until|where|as|that|which|who|before|after|without|instead|through|inside|outside|across|under|over|into|from|with|for|to|at|in|on|by|लेकिन|पर|मगर|क्योंकि|जब|तब|और|फिर|तभी|कि|जो|जहाँ|ताकि|बल्कि)\b)",
        text.strip(),
        flags=re.I,
    )
    sub_parts = [p.strip() for p in sub_parts if p and p.strip()]

    merged = []
    buf = ""
    for sp in sub_parts:
        sp_w = len(sp.split())
        buf_w = len(buf.split()) if buf else 0
        if not buf:
            buf = sp
        elif buf_w + sp_w <= 13:
            buf = f"{buf} {sp}"
        elif buf_w < 6 and buf_w + sp_w <= 15:
            buf = f"{buf} {sp}"
        else:
            merged.append(buf)
            buf = sp
    if buf:
        if merged and len(buf.split()) < 6 and len(merged[-1].split()) + len(buf.split()) <= 15:
            merged[-1] = f"{merged[-1]} {buf}"
        else:
            merged.append(buf)

    # Final pass: if any chunk is still > 14 words, split cleanly into 10-to-12 word visual beats (~3.8s each)
    final_chunks = []
    for ch in merged:
        ch_words = ch.split()
        while len(ch_words) > 14:
            rem = len(ch_words)
            if rem <= 22:
                cut = rem // 2
            else:
                cut = 11
            final_chunks.append(" ".join(ch_words[:cut]))
            ch_words = ch_words[cut:]
        if ch_words:
            if final_chunks and len(ch_words) < 6 and len(final_chunks[-1].split()) + len(ch_words) <= 15:
                final_chunks[-1] = final_chunks[-1] + " " + " ".join(ch_words)
            else:
                final_chunks.append(" ".join(ch_words))
    return final_chunks


def split_script_into_3_4_sec_beats(script_text: str, known_duration_sec: float = 0.0) -> list:
    """
    Dynamically splits any story script (English or Hindi) into crisp 3.5–4.0s visual beats
    matched to the actual voiceover duration (~180 WPM normal speaking speed = ~160 images for a 10m 11s voiceover).
    Every single visual moment on screen strictly matches what is being spoken.
    """
    cleaned = re.sub(r"\r\n?", "\n", script_text.strip())
    raw_paragraphs = [p.strip() for p in cleaned.split("\n") if p.strip()]

    sentences = []
    for para in raw_paragraphs:
        para_clean = re.sub(r"^(Scene\s*\d+[:.\-–—]*|Image\s*\d+[:.\-–—]*|\d+[.):\-–—]\s*)", "", para, flags=re.I).strip()
        if not para_clean:
            continue
        parts = re.split(
            r'(?<!\d)(?<!\ba\.m)(?<!\bp\.m)(?<!\bmr)(?<!\bdr)(?<=[.!?।|])["\']?\s+(?=[A-Z0-9"\'\u0900-\u097F])',
            para_clean,
            flags=re.I,
        )
        for pt in parts:
            s = pt.strip()
            if s:
                sentences.append(s)

    raw_beats = []
    for s in sentences:
        words = s.split()
        w_len = len(words)
        if w_len == 0:
            continue

        if w_len <= 6 and raw_beats and len(raw_beats[-1].split()) + w_len <= 14:
            raw_beats[-1] = raw_beats[-1] + " " + s
            continue

        if w_len <= 14:
            raw_beats.append(s)
            continue

        raw_beats.extend(_split_long_clause_into_3_4s_chunks(s))

    beats = []
    for b in raw_beats:
        bw = len(b.split())
        if bw == 0:
            continue
        if bw <= 5 and beats and len(beats[-1].split()) + bw <= 15:
            beats[-1] = f"{beats[-1]} {b}".strip()
        else:
            beats.append(b)

    total_words = len(cleaned.split())
    est_dur_sec = float(known_duration_sec) if (known_duration_sec and float(known_duration_sec) > 15.0) else ((total_words / 182.0) * 60.0)
    target_count = max(8, round(est_dur_sec / 3.8))

    # Rebalance by merging shortest adjacent pairs if beats exceed target_count
    while len(beats) > target_count and len(beats) > 8:
        best_idx = 0
        best_sum = 999999
        for i in range(len(beats) - 1):
            w_sum = len(beats[i].split()) + len(beats[i + 1].split())
            if w_sum < best_sum:
                best_sum = w_sum
                best_idx = i
        beats[best_idx] = f"{beats[best_idx]} {beats[best_idx + 1]}".strip()
        del beats[best_idx + 1]

    return beats



SCENE_LOCATIONS = {
    "corporate_office": {
        "name": "Modern Corporate Office Interior",
        "setting": "sleek modern glass-walled corporate office interior with ergonomic chair, workstation monitor, and city skyline visible through floor-to-ceiling windows",
        "outfit": "tailored navy-blue business suit jacket over a crisp white collared dress shirt, dark dress trousers, polished leather dress shoes (formal office attire)",
        "valid_actions": [
            "standing by the floor-to-ceiling office window looking out at the city skyline in thought",
            "seated at the sleek corporate workstation reviewing reports on the monitor",
            "walking through the modern glass office corridor holding a leather folder",
        ],
        "broll": [
            "wide establishing shot of a glowing modern corporate office floor with empty glass cubicles, wall clock, and city skyline outside the windows",
            "close-up of a sleek corporate office monitor displaying financial spreadsheets beside a cup of black coffee and a fountain pen",
        ],
    },
    "boardroom_meeting": {
        "name": "Executive Conference Room",
        "setting": "bright executive boardroom with a long polished table, presentation screen on the wall, and glass doors",
        "outfit": "tailored charcoal-grey business suit over a crisp light-blue dress shirt and dark tie (sharp executive attire)",
        "valid_actions": [
            "standing confidently near the presentation screen addressing the room",
            "seated at the polished boardroom table listening intently with hands folded",
        ],
        "broll": [
            "wide shot of an executive conference table with printed financial charts, notebooks, and a glowing presentation screen on the wall",
        ],
    },
    "upscale_restaurant": {
        "name": "Upscale Restaurant / Lounge",
        "setting": "warm upscale restaurant interior with leather booth seating, soft ambient pendant lamps, white tablecloth, and elegant glassware",
        "outfit": "sharp charcoal blazer over a crisp white button-down Oxford shirt, tailored dark chinos, leather loafers (upscale smart-casual attire)",
        "valid_actions": [
            "seated comfortably in the restaurant booth across from a companion, speaking calmly over dinner",
            "looking thoughtfully at the check folder on the white tablecloth under warm pendant lighting",
        ],
        "broll": [
            "cinematic close-up of a leather restaurant bill folder with a credit card on a warmly lit dining table beside two water glasses",
        ],
    },
    "coffee_shop": {
        "name": "Cozy Urban Coffee Shop",
        "setting": "warm neighborhood café interior with a small round wooden bistro table by a large glass window, ceramic espresso cup, brick accent wall, and soft pendant lights",
        "outfit": "clean olive-green bomber jacket over a crisp white crew-neck t-shirt, dark blue jeans, clean white sneakers (smart daytime casual)",
        "valid_actions": [
            "sitting at the small round café window table holding a warm ceramic coffee mug and looking outside",
            "sitting at the café bistro table jotting notes into a small leather notebook beside an espresso cup",
        ],
        "broll": [
            "cinematic close-up of a steaming ceramic coffee cup beside a leather notebook and smartphone on a small wooden café table by a rainy window",
        ],
    },
    "outdoor_park": {
        "name": "Sunlit Public Park Pathway",
        "setting": "wide outdoor public park with a paved walking trail, lush green trees, sunlit grass lawn, and a classic wooden park bench (strictly outdoor park setting — NO desks, NO computers, NO indoor furniture)",
        "outfit": "modern slate-blue athletic zip-up jacket over a grey crew-neck t-shirt, fitted dark joggers, clean running sneakers (outdoor walk attire)",
        "valid_actions": [
            "walking calmly along the tree-lined park pathway with hands in jacket pockets under warm sunlight",
            "sitting naturally on a wooden park bench under the shade of green trees, looking ahead in quiet reflection",
        ],
        "broll": [
            "wide scenic 16:9 shot of a sunlit tree-lined park pathway with empty wooden benches, golden sunlight filtering through green leaves, and birds in the distance",
        ],
    },
    "city_street": {
        "name": "Downtown City Sidewalk",
        "setting": "wide downtown city street and sidewalk with brick storefronts, streetlamps, crosswalk, and warm late-afternoon sunlight (strictly outdoor street — NO indoor furniture or desks)",
        "outfit": "well-fitted navy Harrington jacket over a light grey t-shirt, dark denim jeans, clean white sneakers (urban casual attire)",
        "valid_actions": [
            "walking along the bustling city sidewalk past glass storefronts with a focused expression",
            "standing at a street corner crosswalk waiting for the light while city traffic passes by",
        ],
        "broll": [
            "wide cinematic street-level shot of downtown city buildings, glowing traffic lights, and commuter cars moving through an intersection at dusk",
        ],
    },
    "car_interior": {
        "name": "Inside Car Cabin / Highway Commute",
        "setting": "interior car cabin view from the passenger-side angle showing the driver's seat, steering wheel, dashboard glow, and highway traffic visible through the windshield (strictly inside a car — NO desks or room furniture)",
        "outfit": "clean charcoal-grey collared polo shirt and dark jeans",
        "valid_actions": [
            "sitting in the driver's seat with hands on the steering wheel, looking focused at the road ahead through the windshield",
            "sitting in the parked car looking thoughtfully at the dashboard before turning the key",
        ],
        "broll": [
            "close-up POV shot of a car dashboard speedometer and glowing fuel gauge with highway tail lights blurred through the windshield ahead",
            "wide exterior shot of a sedan parked in a quiet suburban driveway under early morning sky",
        ],
    },
    "grocery_store": {
        "name": "Supermarket / Retail Store Aisle",
        "setting": "brightly lit supermarket aisle lined with neatly stocked shelves, price tags, and a metal shopping cart (strictly inside a retail store)",
        "outfit": "clean burgundy crew-neck sweatshirt over a white tee, dark jeans, white sneakers",
        "valid_actions": [
            "standing in the bright supermarket aisle holding two product boxes and comparing the price labels",
            "pushing a shopping cart slowly down the grocery aisle while checking a list",
        ],
        "broll": [
            "close-up of a supermarket checkout register screen showing rising dollar totals beside a shopping basket of everyday groceries",
        ],
    },
    "bank_lobby": {
        "name": "Financial Bank / Advisor Office",
        "setting": "clean marble bank lobby and glass-partitioned financial advisor office with polished counters and warm lighting",
        "outfit": "sharp navy blazer over a crisp light-blue collared shirt, khaki dress trousers, leather shoes",
        "valid_actions": [
            "standing at the marble bank counter reviewing a printed statement",
            "sitting across from a financial advisor in a glass-walled office discussing documents",
        ],
        "broll": [
            "wide architectural shot of an upscale marble bank lobby with brass signage, glass offices, and warm overhead lighting",
        ],
    },
    "home_kitchen": {
        "name": "Home Kitchen & Dining Table",
        "setting": "warm modern home kitchen with marble countertop, wooden dining table, refrigerator, coffee maker, and soft pendant light",
        "outfit": "comfortable navy-blue knitted crew-neck sweater and tan chinos (clean home attire)",
        "valid_actions": [
            "standing by the kitchen counter pouring morning coffee from a carafe into a ceramic mug",
            "seated at the wooden kitchen dining table looking over monthly household bills and a notepad",
        ],
        "broll": [
            "close-up of a wooden kitchen table with opened paper envelopes, a red 'PAST DUE' notice, a calculator, and a half-empty coffee mug",
        ],
    },
    "home_living_room": {
        "name": "Warm Home Living Room",
        "setting": "cozy apartment living room with a fabric sofa, warm floor lamp, wooden coffee table, indoor plant, and bookshelf",
        "outfit": "clean heather-grey cardigan sweater over a white t-shirt and dark slacks",
        "valid_actions": [
            "sitting on the living room sofa leaning forward in deep thought under the warm floor lamp",
            "standing near the living room window looking out at the evening neighborhood lights",
        ],
        "broll": [
            "wide shot of a quiet, dimly lit living room at evening with a warm floor lamp beside a sofa and a ticking wall clock",
        ],
    },
    "bedroom_night": {
        "name": "Quiet Bedroom at Late Night",
        "setting": "quiet apartment bedroom at night with a bed, pillows, nightstand digital clock, and cool blue moonlight coming through window blinds (NO office desk in the bedroom)",
        "outfit": "simple soft charcoal-grey crew-neck sleep t-shirt and dark lounge pants (late-night homewear)",
        "valid_actions": [
            "sitting on the edge of the bed late at night rubbing his forehead in quiet exhaustion",
            "lying awake in bed staring up at the dark ceiling while moonlight filters through the blinds",
        ],
        "broll": [
            "close-up of a glowing red digital alarm clock reading 3:12 AM on a wooden nightstand beside a glass of water in a dark bedroom",
        ],
    },
    "home_study_desk": {
        "name": "Home Study Desk",
        "setting": "focused home study corner with a clean wooden desk, open laptop, warm brass desk lamp, and bookshelf",
        "outfit": "crisp light-blue roll-sleeve button-down shirt and dark chinos",
        "valid_actions": [
            "sitting at the wooden study desk typing a plan on the laptop under the warm brass lamp",
            "writing structured financial goals in a hardcover journal beside the open laptop",
        ],
        "broll": [
            "close-up over-the-shoulder shot of an open laptop screen showing a clean rising investment chart and budget tracker beside a handwritten notebook",
        ],
    },
    "workshop_garage": {
        "name": "Hands-On Workshop / Garage",
        "setting": "well-organized workshop garage with a sturdy wooden workbench, red metal tool chest, pegboard tools, and overhead shop light",
        "outfit": "durable dark canvas work jacket over a blue chambray work shirt, rugged work pants, leather boots",
        "valid_actions": [
            "standing at the workbench organizing tools with a calm, focused expression",
            "examining a blueprint and measuring tape on the workshop bench",
        ],
        "broll": [
            "detailed close-up of a wooden workbench with neatly arranged wrenches, blueprints, and a warm overhead shop lamp",
        ],
    },
    "horror_corridor": {
        "name": "Eerie Dark Hallway at 3:00 AM",
        "setting": "dimly lit, unsettling long residential hallway at 3:00 AM with flickering overhead bulb, deep shadows stretching across old wooden floorboards, and a slightly open door at the end",
        "outfit": "dark charcoal hoodie over a simple t-shirt and dark pants (late-night indoor attire)",
        "valid_actions": [
            "standing frozen in the dim hallway holding a phone flashlight toward the shadowy open door",
            "turning slowly with wide uneasy eyes as a long shadow stretches across the hallway wall",
        ],
        "broll": [
            "chilling wide 16:9 shot of an empty dimly lit corridor at night with a slightly ajar wooden door and cold moonlight pooling on the floor",
        ],
    },
    "foggy_night_forest": {
        "name": "Foggy Midnight Road & Woods",
        "setting": "eerie fog-covered road bordered by tall dark pine trees under pale moonlight with thick mist rolling across the asphalt",
        "outfit": "dark weatherproof jacket over a sweater and dark jeans",
        "valid_actions": [
            "standing cautiously beside car headlights cutting through the thick midnight fog on the empty road",
            "looking toward the dark tree line where an indistinct silhouette stands in the mist",
        ],
        "broll": [
            "atmospheric 16:9 wide shot of a lonely fog-draped road at midnight with a single flickering streetlamp and dark forest silhouettes",
        ],
    },
    "creepy_old_house": {
        "name": "Abandoned Old House Interior",
        "setting": "creepy old house interior with creaking wooden stairs, dusty vintage furniture, rain-streaked windows, and eerie moonlight shadows",
        "outfit": "dark jacket over a casual shirt and dark trousers",
        "valid_actions": [
            "walking slowly up the creaking wooden staircase while looking up into the dark landing",
            "standing near the rain-streaked window looking out into the stormy night",
        ],
        "broll": [
            "cinematic horror 16:9 shot of an old house living room during a thunderstorm with lightning casting window-frame shadows across the floor",
        ],
    },
    "crime_investigation_room": {
        "name": "Noir Detective Investigation Room",
        "setting": "moody investigation room with a corkboard of case photos connected by red string, case files on a desk, and venetian blind shadows",
        "outfit": "dark trench coat or charcoal blazer over a button-down shirt",
        "valid_actions": [
            "standing before the investigation board studying the red-string timeline of clues",
            "examining a case file folder and photographs under a green banker's desk lamp",
        ],
        "broll": [
            "close-up 16:9 shot of a detective investigation board with pinned photographs, map markers, and red string under moody lighting",
        ],
    },
    "ancient_historical_hall": {
        "name": "Ancient Historical Courtyard & Hall",
        "setting": "grand historical stone courtyard and torch-lit hall with carved pillars, ancient scrolls on a wooden table, and dramatic golden-hour shafts of light",
        "outfit": "classic period-appropriate tunic and dark cloak or timeless linen attire",
        "valid_actions": [
            "standing among the grand stone pillars observing the historical setting in solemn thought",
            "studying an ancient parchment scroll and map on a carved wooden table by torchlight",
        ],
        "broll": [
            "epic wide 16:9 establishing shot of ancient stone architecture, glowing torches, and dramatic clouds at sunset",
        ],
    },
    "futuristic_tech_hub": {
        "name": "Futuristic AI & Space Command Center",
        "setting": "sleek futuristic command center with panoramic holographic data displays, glowing neon blue circuitry accents, and a view of Earth or a cyberpunk skyline",
        "outfit": "sleek modern minimalist dark tech jacket and fitted trousers",
        "valid_actions": [
            "standing before a glowing 3D holographic projection analyzing futuristic data streams",
            "looking out through the panoramic observation glass at the futuristic skyline",
        ],
        "broll": [
            "wide 16:9 sci-fi shot of a glowing holographic neural network and futuristic cityscape at night",
        ],
    },
}

ROTATING_LOCATION_ORDER = [
    "home_kitchen",
    "city_street",
    "corporate_office",
    "coffee_shop",
    "outdoor_park",
    "bank_lobby",
    "car_interior",
    "upscale_restaurant",
    "grocery_store",
    "home_living_room",
    "home_study_desk",
    "boardroom_meeting",
]

HORROR_LOCATION_ORDER = [
    "bedroom_night",
    "horror_corridor",
    "creepy_old_house",
    "foggy_night_forest",
    "car_interior",
    "home_living_room",
]

CRIME_LOCATION_ORDER = [
    "crime_investigation_room",
    "city_street",
    "car_interior",
    "bedroom_night",
    "corporate_office",
    "horror_corridor",
]

HISTORY_LOCATION_ORDER = [
    "ancient_historical_hall",
    "outdoor_park",
    "home_study_desk",
]

SCIFI_LOCATION_ORDER = [
    "futuristic_tech_hub",
    "corporate_office",
    "city_street",
    "home_study_desk",
]


def idx_hash(s: str) -> int:
    return sum(ord(c) for c in (s or ""))


ANIMATION_STYLE_LABELS = {
    "2d_illustrated": "2D Illustrated Animation (Bold Ink Outlines & Cel Shading)",
    "sketch": "Hand-Drawn Ink & Charcoal Sketch Animation",
    "flat_vector": "Clean 2D Flat Vector Animation",
    "comic_noir": "Dark Graphic Novel / Comic Noir Animation",
    "docu_cinematic": "Cinematic Documentary Digital Painting",
    "anime_style": "2D Anime / Webtoon Animation Style",
    "3d_pixar": "3D Stylized Pixar / 3D Animated Render Style",
    "charcoal_horror": "Dark Gritty Charcoal & Eerie Horror Art Style",
}

ANIMATION_STYLE_DESCRIPTIONS = {
    "2d_illustrated": "polished 2D digital cartoon/narrative illustration with clean bold black ink outlines, warm cinematic lighting, soft cel shading, and detailed semi-realistic painted 2D background (no photorealism)",
    "sketch": "expressive hand-drawn ink and charcoal sketch animation style with textured cross-hatching, visible pen & pencil linework, and editorial sketchbook shading (strictly hand-drawn sketch look, no photorealism)",
    "comic_noir": "dark cinematic graphic novel / comic noir illustration with heavy black ink shadows, dramatic chiaroscuro lighting, high-contrast atmosphere, and graphic novel panel framing",
    "flat_vector": "modern minimalist 2D flat vector animation style with clean geometric shapes, crisp vector silhouettes, smooth gradients, and high-contrast vector color palette",
    "docu_cinematic": "cinematic atmospheric documentary digital matte painting with dramatic volumetric lighting, deep depth-of-field, rich painterly realism, and moody film color grading",
    "anime_style": "high-quality 2D anime / webtoon animation style with expressive anime character design, crisp line art, dramatic anime lighting, and richly painted anime background art",
    "3d_pixar": "stylized 3D animated movie render (Pixar / DreamWorks 3D animation style) with expressive 3D character modeling, soft subsurface scattering, and cinematic 3D volumetric lighting",
    "charcoal_horror": "gritty dark charcoal and eerie ink horror illustration with unsettling shadow textures, foggy vignette, cold moonlit contrast, and spine-chilling atmospheric dread",
}

NICHE_VISUAL_OVERRIDES = {
    "horror_dark": "Eerie dark horror & paranormal atmosphere with chilling shadows, volumetric midnight fog, cold moonlight vs flickering dim warm light, and spine-tingling suspense",
    "documentary": "Authentic cinematic documentary visual storytelling with rich real-world environments, detailed subject focus, natural volumetric lighting, and deep visual realism",
    "crime_detective": "Dark noir true-crime & detective investigation atmosphere with dramatic shadows, rain-slicked streets, forensic evidence details, and high-tension thriller framing",
    "history_ancient": "Epic historical & ancient civilization atmosphere with period-accurate architecture, flickering torchlight, authentic historical textures, and grand cinematic depth",
    "luxury_wealth": "High-contrast quiet wealth & billionaire empire aesthetic with sleek architectural spaces, warm golden-hour lighting, and refined financial storytelling details",
    "psychology_stoic": "Moody symbolic dark-psychology & stoic philosophical atmosphere with dramatic chiaroscuro spotlighting, expressive human emotion, and deep psychological symbolism",
    "scifi_future": "Futuristic sci-fi, space & advanced AI atmosphere with glowing holographic interfaces, neon atmospheric depth, and expansive futuristic environments",
    "comedy_funny": "Lively, expressive, comedic & humorous storytelling atmosphere with hilarious facial expressions, playful visual gags, vibrant warm lighting, and entertaining situational comedy",
}

# Rich Niche-Specific Environments & World Elements (so non-finance niches NEVER get random office/finance rooms)
NICHE_WORLD_PROFILES = {
    "horror_dark": {
        "settings": [
            "dimly lit eerie hallway at 3:00 AM with peeling wallpaper, flickering overhead bulb, and a half-open wooden door leading into pitch darkness",
            "abandoned old two-story house surrounded by dead twisted trees, thick rolling midnight fog, and cold pale moonlight",
            "creepy shadowy bedroom late at night with moonlight filtering through window blinds, an old mirror on the wall, and long unsettling shadows",
            "fog-covered lonely road cutting through a dark pine forest at midnight with a single flickering streetlamp",
            "damp shadowy basement staircase with cracked stone walls, a swinging bare bulb, and eerie mist pooling on the floor",
            "dusty old attic lit by a single beam of moonlight revealing vintage furniture, old portraits, and a rocking chair casting a long shadow",
        ],
        "object_shots": [
            "chilling close-up & environment shot of an antique wall clock frozen at 3:00 AM, flickering candle wax, and an eerie tall shadow stretching across the wall",
            "unsettling wide shot of an empty fog-draped midnight corridor where a shadowy silhouette stands barely visible behind a frosted glass door",
            "eerie close-up of a rain-streaked window at night with cold condensation and a mysterious dark figure standing outside under a flickering streetlamp",
            "creepy detail shot of an old wooden door handle slowly turning by itself with scratch marks on the doorframe and deep darkness beyond",
        ],
        "human_shots": [
            "a tense person holding a dim flashlight whose beam cuts through the dark room toward an eerie shadow on the wall",
            "a startled figure looking back over their shoulder in the shadowy corridor as a door slowly creaks open behind them",
            "two people standing frozen in the misty midnight yard looking up at a dimly lit top-floor window of the old house",
        ],
    },
    "documentary": {
        "settings": [
            "expansive real-world documentary location with dramatic natural lighting, deep horizon, and authentic environmental textures",
            "cinematic aerial and ground-level documentary perspective showing the real-world geography, architecture, and human activity of the subject",
            "detailed inside look at the real-world facility, historical site, ecosystem, or urban center being explored in the narration",
            "atmospheric wide-angle documentary vista with rich foreground details and dramatic sky",
        ],
        "object_shots": [
            "detailed documentary close-up & macro showcase of the exact physical objects, mechanisms, natural elements, archival documents, or maps described in the narration",
            "wide cinematic establishing shot of the environment, architecture, nature, or infrastructure illustrating the real-world scale of the topic",
            "visual documentary comparison frame showing the physical evidence, real-world process, and environmental details in sharp focus",
        ],
        "human_shots": [
            "real-world people, workers, researchers, or citizens naturally engaged in the exact activity and environment described in the narration",
            "a documentary subject observing the surrounding environment with authentic props and period/location-accurate details",
        ],
    },
    "crime_detective": {
        "settings": [
            "moody detective investigation room with a red-string evidence board, pinned case photographs, maps, and a warm green banker's lamp",
            "rain-soaked city street at night cordoned off with yellow crime-scene tape under flashing red-and-blue patrol lights",
            "dimly lit interrogation room with a metal table, case dossier folders, a tape recorder, and dramatic overhead spotlighting",
            "shadowy urban alleyway and wet asphalt reflecting neon signs and streetlamps at midnight",
        ],
        "object_shots": [
            "forensic close-up of numbered yellow evidence markers on wet pavement beside footprints, a case file folder, and red-and-blue light reflections",
            "detailed shot of a detective corkboard connected with red string, newspaper clippings, timeline notes, and suspect photographs",
            "high-contrast noir shot of a classified manila dossier stamped 'CONFIDENTIAL', a magnifying glass, and a glowing phone call log",
        ],
        "human_shots": [
            "a focused investigator in a dark coat examining physical clues and case files under moody chiaroscuro lighting",
            "a shadowy suspect silhouette seen through frosted glass and venetian blind shadows in a tense thriller composition",
        ],
    },
    "history_ancient": {
        "settings": [
            "grand ancient stone temple courtyard with towering carved pillars, blazing bronze torches, and golden sunset rays piercing the dust",
            "majestic historical royal throne room and war-council hall with ancient maps unrolled on a carved oak table",
            "bustling ancient civilization marketplace and monumental stone architecture overlooking a river valley",
            "epic historical battlefield and fortress ramparts under dramatic storm clouds with banners waving in the wind",
        ],
        "object_shots": [
            "detailed historical close-up of ancient parchment scrolls,wax seals, antique gold coins, carved stone inscriptions, and bronze artifacts lit by torchlight",
            "epic wide establishing shot of monumental ancient architecture, towering walls, and dramatic skies from history",
            "cinematic showcase of historical weapons, royal crowns, ancient ships, and period-accurate cultural relics",
        ],
        "human_shots": [
            "historical figures, rulers, scholars, or warriors in authentic period attire acting out the exact historical moment in a grand setting",
            "a crowd of ancient citizens and soldiers gathered in the monumental courtyard witnessing the historical event",
        ],
    },
    "luxury_wealth": {
        "settings": [
            "floor-to-ceiling billionaire penthouse overlooking a glittering metropolitan skyline at golden hour",
            "sleek private aviation tarmac with an executive jet and a black luxury sedan waiting on the runway",
            "refined oak-paneled private family-office library and high-stakes executive boardroom",
            "sunlit Mediterranean harbor with a modern superyacht and classic stone villa architecture",
        ],
        "object_shots": [
            "refined close-up of high-value assets, architectural blueprints, stock market ticker screens, and classic wealth symbols in warm cinematic light",
            "wide establishing shot contrasting quiet compounding assets and timeless real estate against flashy depreciating luxury",
        ],
        "human_shots": [
            "a composed, impeccably dressed strategist or founder in tailored attire reviewing global operations and interacting with associates",
        ],
    },
    "psychology_stoic": {
        "settings": [
            "dramatic chiaroscuro study room with a single warm spotlight, classical marble bust of Marcus Aurelius, and leather-bound philosophy books",
            "high windy mountain cliff overlooking a vast turbulent ocean under moody clouds",
            "busy blurred city crowd in motion contrasting against a calm, still focal point in the center of the frame",
        ],
        "object_shots": [
            "symbolic psychological composition showing a wooden chessboard mid-game, a glowing hourglass, and dramatic shadow interplay",
            "powerful visual metaphor of mental clarity and self-mastery — a calm lantern flame burning steady amidst swirling wind and shadows",
        ],
        "human_shots": [
            "a calm, deeply focused individual standing composed and unshaken while chaotic silhouettes rush past in the background",
            "two contrasting figures illustrating the psychological dynamic, body language, and emotional tension described in the narration",
        ],
    },
    "scifi_future": {
        "settings": [
            "futuristic orbital space station observation deck overlooking a glowing planet and deep-space nebula",
            "advanced AI quantum laboratory with floating 3D holographic neural networks and sleek neon-lit architecture",
            "expansive cyberpunk megacity skyline at night with flying transports, towering holographic billboards, and rain-slicked skybridges",
        ],
        "object_shots": [
            "macro sci-fi close-up of a glowing quantum processor, bionic robotics, and holographic data streams floating in mid-air",
            "epic wide 16:9 shot of deep space galaxies, futuristic spacecraft, and planetary megastructures",
        ],
        "human_shots": [
            "futuristic researchers, astronauts, or humanoid AI figures interacting with glowing holographic projections in a high-tech environment",
        ],
    },
    "comedy_funny": {
        "settings": [
            "colorful, lively everyday setting with humorous visual details and playful comedic timing",
            "expressive animated situational comedy backdrop matching the exact funny moment spoken in the narration",
        ],
        "object_shots": [
            "humorous close-up of the exact funny objects, visual gags, and situational comedy props described in the narration",
        ],
        "human_shots": [
            "expressive characters with hilarious facial reactions, comical body language, and witty timing acting out the exact funny moment spoken",
        ],
    },
}

CAMERA_ANGLE_VARIETY = [
    "Wide 16:9 cinematic establishing shot with deep foreground-to-background perspective",
    "Detailed 16:9 medium storytelling shot focusing on the exact action and surrounding props",
    "Dramatic 16:9 close-up / macro focus shot highlighting the key physical objects and atmosphere",
    "Over-the-shoulder / POV 16:9 composition drawing the viewer directly into the moment",
    "Atmospheric 16:9 low-angle cinematic shot emphasizing scale, tension, and lighting",
    "Dynamic 16:9 multi-layered scene showing both foreground objects and background environment activity",
]


def _has_any_word(text_low: str, keywords: list) -> bool:
    """
    Matches keywords only as whole words/phrases (never as substrings inside other words like
    'lock' in 'clock', 'truck' in 'struck', 'king' in 'looking', 'bed' in 'grabbed', 'car' in 'scared', 'ai' in 'wait').
    """
    for kw in keywords:
        kw_clean = kw.strip().lower()
        if not kw_clean:
            continue
        if kw_clean == "$":
            if "$" in text_low:
                return True
            continue
        pattern = r"(?<![a-zA-Z0-9\u0900-\u097F])" + re.escape(kw_clean) + r"(?:s|es)?(?![a-zA-Z0-9\u0900-\u097F])"
        if re.search(pattern, text_low):
            return True
    return False


def extract_beat_visual_clues(beat_text: str) -> dict:
    """
    Analyzes a single spoken script beat (in English OR Hindi Devanagari) using strict whole-word matching and extracts:
    - specific physical objects / props / phenomena explicitly mentioned
    - whether secondary people / crowds / entities are mentioned
    - whether the beat is primarily about an object/place/fact/environment
    """
    low = beat_text.lower()
    props_and_visuals = []
    has_other_people = False
    is_environment_or_object_heavy = False

    # 1. Horror & Paranormal keywords (English + Hindi)
    if _has_any_word(low, ["shadow", "silhouette", "परछाई", "साया", "आकृति"]):
        props_and_visuals.append("an eerie dark shadow silhouette stretching across the wall and floor")
        has_other_people = True
    if _has_any_word(low, ["ghost", "spirit", "entity", "paranormal", "demon", "haunted", "apparition", "भूत", "आत्मा", "प्रेत", "रूह"]):
        props_and_visuals.append("a chilling ghostly apparition partially visible in the dim mist and shadows")
        has_other_people = True
    if _has_any_word(low, ["door", "doorknob", "knock", "knocking", "creak", "creaking", "padlock", "locked", "दरवाजा", "दरवाज़ा", "दस्तक", "कुंडी", "ताला"]):
        props_and_visuals.append("an old wooden door slowly creaking ajar with deep shadows behind it")
        is_environment_or_object_heavy = True
    if _has_any_word(low, ["window", "curtain", "खिड़की", "पर्दा"]):
        props_and_visuals.append("a rain-streaked glass window at night with cold moonlight and swaying curtains")
        is_environment_or_object_heavy = True
    if _has_any_word(low, ["mirror", "reflection", "आइना", "शीशा", "अक्स"]):
        props_and_visuals.append("an antique mirror showing an unsettling reflection in dim candlelight")
        is_environment_or_object_heavy = True
    if _has_any_word(low, ["footstep", "stairs", "staircase", "hallway", "corridor", "basement", "floorboards", "सीढ़ियां", "सीढ़ी", "कदम", "आहट", "तहखाना", "गलियारा"]):
        props_and_visuals.append("creaking wooden stairs and a long dimly lit corridor with deep shadows")
        is_environment_or_object_heavy = True
    if _has_any_word(low, ["forest", "woods", "tree", "fog", "mist", "midnight road", "lonely road", "जंगल", "पेड़", "कोहरा"]):
        props_and_visuals.append("thick rolling midnight fog among tall dark trees along a lonely road")
        is_environment_or_object_heavy = True
    if _has_any_word(low, ["flashlight", "torch", "candle", "lantern", "bulb", "flicker", "flickering", "darkness", "टॉर्च", "मोमबत्ती", "लालटेन", "बल्ब", "अंधेरा"]):
        props_and_visuals.append("a flickering light beam cutting through pitch-black darkness")
    if _has_any_word(low, ["whisper", "scream", "screaming", "फुसफुसाहट", "चीख", "सन्नाटा"]):
        props_and_visuals.append("tense visual atmosphere capturing the sudden eerie sound in the dead of night")

    # 2. Crime, Mystery & Investigation keywords (English + Hindi)
    if _has_any_word(low, ["police", "cop", "officer", "detective", "investigation", "investigator", "crime scene", "पुलिस", "जासूस", "तहकीकात", "जांच", "अफसर"]):
        props_and_visuals.append("police crime-scene tape, flashing red-and-blue patrol lights, and investigation files")
        has_other_people = True
    if _has_any_word(low, ["clue", "evidence", "fingerprint", "blood", "weapon", "cctv", "footage", "सबूत", "सुराग", "निशान", "खून"]):
        props_and_visuals.append("forensic evidence markers, security camera monitor view, and physical clues on the ground")
        is_environment_or_object_heavy = True
    if _has_any_word(low, ["suspect", "killer", "murder", "victim", "witness", "कातिल", "अपराधी", "गवाह", "लापता"]):
        props_and_visuals.append("a shadowy suspect photograph pinned to an investigation board with red string and timeline notes")
        has_other_people = True

    # 3. History, Ancient & War keywords (English + Hindi)
    if _has_any_word(low, ["king", "emperor", "queen", "ruler", "pharaoh", "sultan", "राजा", "सम्राट", "सुल्तान", "शासक", "महारानी"]):
        props_and_visuals.append("a royal ruler in authentic historical attire inside a torch-lit palace hall")
        has_other_people = True
    if _has_any_word(low, ["empire", "kingdom", "ancient", "temple", "palace", "fortress", "castle", "साम्राज्य", "प्राचीन", "मंदिर", "महल", "किला"]):
        props_and_visuals.append("monumental ancient stone architecture, carved pillars, ancient scrolls, and blazing torches")
        is_environment_or_object_heavy = True
    if _has_any_word(low, ["war", "battle", "battlefield", "army", "soldier", "warrior", "sword", "invasion", "युद्ध", "लड़ाई", "सेना", "सैनिक", "योद्धा", "तलवार"]):
        props_and_visuals.append("an epic historical battlefield with soldiers, armor, banners, and dramatic smoke")
        has_other_people = True
        is_environment_or_object_heavy = True

    # 4. Documentary, Nature, Science & World keywords (English + Hindi)
    if _has_any_word(low, ["ocean", "sea", "river", "wave", "ship", "boat", "समुद्र", "महासागर", "नदी", "जहाज", "लहर"]):
        props_and_visuals.append("vast ocean waves, deep water currents, and maritime vessels under dramatic skies")
        is_environment_or_object_heavy = True
    if _has_any_word(low, ["mountain", "snow", "desert", "planet", "climate", "storm", "पहाड़", "बर्फ", "रेगिस्तान", "तूफान"]):
        props_and_visuals.append("sweeping natural landscape with dramatic weather, mountains, and atmospheric sky")
        is_environment_or_object_heavy = True
    if _has_any_word(low, ["science", "scientist", "laboratory", "experiment", "वैज्ञानिक", "प्रयोगशाला", "शोध"]):
        props_and_visuals.append("detailed scientific visual breakdown, laboratory equipment, and glowing analytical diagrams")
        is_environment_or_object_heavy = True

    # 5. Sci-Fi, Space & Future Tech keywords (English + Hindi)
    if _has_any_word(low, ["outer space", "galaxy", "universe", "nasa", "rocket", "mars", "spacecraft", "अंतरिक्ष", "ब्रह्मांड", "रॉकेट"]):
        props_and_visuals.append("deep space cosmos, glowing nebula stars, spacecraft, and planetary horizon")
        is_environment_or_object_heavy = True
    if _has_any_word(low, ["ai", "robot", "cyber", "algorithm", "रोबोट", "टेक्नोलॉजी"]):
        props_and_visuals.append("futuristic AI hardware, glowing holographic screens, and robotic technology")
        is_environment_or_object_heavy = True

    # 6. Everyday Life, People, Vehicles, Home & Wealth keywords (English + Hindi)
    if _has_any_word(low, ["car", "driving", "truck", "vehicle", "traffic", "highway", "steering", "गाड़ी", "कार", "ड्राइव", "ट्रैफिक"]):
        props_and_visuals.append("a detailed automobile on the road with dashboard glow and street environment")
        is_environment_or_object_heavy = True
    if _has_any_word(low, ["house", "apartment", "bedroom", "living room", "roof", "घर", "मकान", "कमरा"]):
        props_and_visuals.append("detailed residential home architecture and interior setting")
    if _has_any_word(low, ["phone", "smartphone", "notification", "फोन", "मोबाइल", "मैसेज"]):
        props_and_visuals.append("a glowing smartphone screen showing notifications/messages in close focus")
    if _has_any_word(low, ["clock", "watch", "3:12", "3 a.m", "2 a.m", "घड़ी"]):
        props_and_visuals.append("a prominent clock / timepiece showing the exact time")
    if _has_any_word(low, ["money", "cash", "dollar", "$", "debt", "loan", "bank", "investment", "stock", "wealth", "salary", "पैसा", "रुपये", "कर्ज", "बैंक", "निवेश", "सैलरी"]):
        props_and_visuals.append("clear physical financial props — currency bills, bank statements, or investment charts")
        is_environment_or_object_heavy = True
    if _has_any_word(low, ["friend", "family", "mother", "father", "parent", "wife", "husband", "child", "crowd", "neighbor", "boss", "coworker", "दोस्त", "परिवार", "माँ", "पिता", "पत्नी", "बच्चे", "भीड़", "पड़ोसी", "बॉस"]):
        props_and_visuals.append("secondary characters / other people naturally present and interacting in the scene")
        has_other_people = True

    return {
        "props_str": ", ".join(props_and_visuals[:2]) if props_and_visuals else "",
        "has_other_people": has_other_people,
        "is_environment_or_object_heavy": is_environment_or_object_heavy,
    }


def detect_explicit_location_key(beat_text: str):
    """
    Returns a SCENE_LOCATIONS key ONLY if the spoken beat explicitly mentions whole words for that place.
    NEVER returns a random fallback room (no fake kitchen, coffee shop, or corporate office when not spoken!).
    """
    low = beat_text.lower()
    if _has_any_word(low, ["shadow", "ghost", "whisper", "knock", "footstep", "hallway", "corridor", "creak", "darkness", "scream", "floorboards", "basement", "आहट", "परछाई", "अंधेरा", "भूत", "चीख", "तहखाना", "गलियारा"]):
        return "horror_corridor" if idx_hash(beat_text) % 2 == 0 else "creepy_old_house"
    if _has_any_word(low, ["forest", "woods", "fog", "mist", "lonely road", "midnight road", "जंगल", "कोहरा"]):
        return "foggy_night_forest"
    if _has_any_word(low, ["detective", "police", "clue", "evidence", "murder", "suspect", "crime scene", "investigation", "पुलिस", "सुराग", "सबूत", "जांच"]):
        return "crime_investigation_room"
    if _has_any_word(low, ["ancient", "empire", "king", "temple", "kingdom", "scroll", "pharaoh", "राजा", "साम्राज्य", "मंदिर", "प्राचीन"]):
        return "ancient_historical_hall"
    if _has_any_word(low, ["robot", "outer space", "planet", "galaxy", "cyber", "अंतरिक्ष", "रोबोट"]):
        return "futuristic_tech_hub"
    if _has_any_word(low, ["bed", "bedroom", "asleep", "awake", "pillow", "mattress", "blanket", "नींद", "बिस्तर", "तकिया"]):
        return "bedroom_night"
    if _has_any_word(low, ["park", "bench", "trail", "jog", "पार्क", "बगीचा"]):
        return "outdoor_park"
    if _has_any_word(low, ["coffee shop", "cafe", "café", "espresso", "barista", "latte", "कैफे"]):
        return "coffee_shop"
    if _has_any_word(low, ["restaurant", "dinner", "waiter", "menu", "steak", "रेस्तरां", "डिनर"]):
        return "upscale_restaurant"
    if _has_any_word(low, ["grocery", "supermarket", "aisle", "shopping cart", "checkout", "mall", "सुपरमार्केट"]):
        return "grocery_store"
    if _has_any_word(low, ["car", "driving", "highway", "traffic", "steering", "commute", "sedan", "truck", "गाड़ी", "कार", "ड्राइव", "ट्रैफिक"]):
        return "car_interior"
    if _has_any_word(low, ["boardroom", "presentation", "conference", "ceo", "मीटिंग रूम"]):
        return "boardroom_meeting"
    if _has_any_word(low, ["office", "cubicle", "coworker", "manager", "boss", "corporate", "workplace", "interview", "ऑफिस", "दफ्तर", "बॉस"]):
        return "corporate_office"
    if _has_any_word(low, ["bank lobby", "teller", "vault", "mortgage", "बैंक काउंटर", "तिजोरी"]):
        return "bank_lobby"
    if _has_any_word(low, ["garage", "workshop", "toolbox", "wrench", "plumbing", "electrician", "गैरेज", "वर्कशॉप"]):
        return "workshop_garage"
    if _has_any_word(low, ["kitchen", "breakfast", "fridge", "stove", "dining table", "रसोई", "किचन", "नाश्ता"]):
        return "home_kitchen"
    if _has_any_word(low, ["street", "sidewalk", "alley", "downtown", "crosswalk", "गली", "सड़क", "चौराहा"]):
        return "city_street"
    if _has_any_word(low, ["sofa", "couch", "living room", "television", "सोफा", "लिविंग रूम"]):
        return "home_living_room"
    if _has_any_word(low, ["laptop", "spreadsheet", "study desk", "notebook", "journal", "लैपटॉप", "डायरी"]):
        return "home_study_desk"
    return None



def _ensure_pure_english_prompt(text: str) -> str:
    """
    Strips any non-ASCII / Devanagari characters and Midjourney flags so diffusion CLIP tokenizers
    (Leonardo AI, SDXL, Flux, Stability, OpenAI) NEVER receive UTF-8 byte garbage that causes brown noise!
    """
    cleaned = re.sub(r"[^\x20-\x7E]+", " ", text or "")
    cleaned = re.sub(r"--ar\s+\d+:\d+", "", cleaned, flags=re.I)
    cleaned = cleaned.replace("|", " ")
    cleaned = re.sub(r'"\s*"', "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def _convert_beat_to_third_person_visual_action(beat_text: str, char_name: str = "") -> str:
    """
    Transforms a spoken English beat (e.g. 'You sit quietly at the corner table holding your old coffee mug')
    into a direct visual action phrase ('sitting quietly at the corner table holding an old coffee mug')
    so even deterministic fallbacks describe the EXACT spoken action and objects 1-to-1.
    """
    ascii_b = _ensure_pure_english_prompt(beat_text).strip().strip('"\'.!? ')
    if not ascii_b:
        return ""
    s = ascii_b
    if char_name:
        s = re.sub(r"\b" + re.escape(char_name) + r"\b", "the main character", s, flags=re.I)
    s = re.sub(r"^\s*(?:and|but|so|yet|meanwhile|suddenly|then)\s+", "", s, flags=re.I)
    s = re.sub(r"\byou're\b", "the main character is", s, flags=re.I)
    s = re.sub(r"\byou've\b", "the main character has", s, flags=re.I)
    s = re.sub(r"\byou'll\b", "the main character will", s, flags=re.I)
    s = re.sub(r"\byour\b", "his", s, flags=re.I)
    s = re.sub(r"\byourself\b", "himself", s, flags=re.I)
    s = re.sub(r"\byou\b", "the main character", s, flags=re.I)
    s = re.sub(r"\bi'm\b", "the main character is", s, flags=re.I)
    s = re.sub(r"\bmy\b", "his", s, flags=re.I)
    s = re.sub(r"\bi\b", "the main character", s, flags=re.I)
    return s.strip()


TIMELINE_OUTFIT_PALETTE = [
    "wearing a casual navy-blue hoodie and dark jeans",
    "wearing a crisp light-blue button-down collared shirt and chinos",
    "wearing an olive-green casual jacket over a clean white t-shirt",
    "wearing a warm charcoal-grey crewneck sweater",
    "wearing a burgundy zip-up jacket over a grey tee",
    "wearing a relaxed beige cardigan over a dark crewneck shirt",
    "wearing a smart dark-slate blazer over a casual white shirt",
]

WORLD_BROLL_TRIGGER_WORDS = re.compile(
    r"\b(chart|graph|stock|market|bank|building|skyline|clock|calendar|percentage|inflation|system|economy|factory|storefront|highway|traffic|spreadsheet|calculator|contract|billboard|vault|algorithm|server|data|network|newspaper|headline)\b",
    flags=re.I,
)


def build_strict_literal_scene_fallback(
    beat_text: str,
    idx: int,
    effective_niche: str,
    niche_type: str,
    char_name: str,
    char_name_mode: str,
    timeline_outfit: str = "",
) -> tuple:
    """
    Deterministic fallback that builds a 100% pure English literal visual scene description
    directly depicting the exact spoken words, actions, and objects in beat_text.
    Returns (scene_tag, literal_scene_str) where scene_tag is 'CHAR' or 'WORLD'.
    """
    clues = extract_beat_visual_clues(beat_text)
    explicit_loc_key = detect_explicit_location_key(beat_text)
    explicit_loc = SCENE_LOCATIONS.get(explicit_loc_key) if explicit_loc_key else None

    loc_phrase = f" inside {explicit_loc['name']} ({explicit_loc['setting']})" if explicit_loc else ""
    props_phrase = f", with clear focus on {clues['props_str']}" if clues["props_str"] else ""

    has_non_ascii = bool(re.search(r"[^\x00-\x7F]", beat_text))
    visual_action = "" if has_non_ascii else _convert_beat_to_third_person_visual_action(beat_text, char_name)

    # Decide if this beat works better as a WORLD / B-roll object-environment shot (~every 4th beat or strong object/world beat)
    has_first_person = bool(re.search(r"\b(I|my|me|we|he|him|his|Daniel)\b", beat_text, flags=re.I))
    is_world_candidate = (
        (bool(WORLD_BROLL_TRIGGER_WORDS.search(beat_text)) and not has_first_person)
        or (idx % 4 == 0 and bool(clues["props_str"]) and not has_first_person)
    )

    if is_world_candidate:
        scene_tag = "WORLD"
        focal_obj = clues["props_str"] or "the key environment and objects mentioned in the spoken line"
        literal_scene = f"detailed cinematic close-up and environmental view of {focal_obj}{loc_phrase}, illustrating the exact spoken moment without the main character"
        return scene_tag, _ensure_pure_english_prompt(literal_scene)

    scene_tag = "CHAR"
    outfit_clause = f" ({timeline_outfit})" if (timeline_outfit and niche_type != "stickman") else ""
    if niche_type == "stickman":
        subj = "the 2D stickman main character"
    else:
        subj = f"the main character{outfit_clause}"

    if visual_action:
        literal_scene = f"{subj} {visual_action}{loc_phrase}{props_phrase}"
    else:
        literal_scene = f"{subj} actively performing the exact spoken moment{loc_phrase}{props_phrase}"

    return scene_tag, _ensure_pure_english_prompt(literal_scene)


SCRIPT_TYPE_VISUAL_MOODS = {
    "wealth_story": "Financial & everyday life POV storytelling mood (MoneyLifePOV / QuietCapitalUSA style) with clear money/budget/lifestyle contrast visual details",
    "living_type": "Realistic day-to-day living & daily routine POV atmosphere with authentic home, workplace, and street details",
    "horror_story": "Eerie, spine-chilling late-night horror & paranormal atmosphere with dark shadows, tense lighting, and unsettling mood",
    "comedy_funny": "Hilarious, expressive comedic & humorous cartoon atmosphere with funny reactions, lively expressions, and playful visual timing",
    "documentary_type": "Cinematic documentary & investigative storytelling atmosphere with detailed visual context",
    "facts_type": "High-retention curiosity & hidden-truths visual storytelling with clear focal objects",
    "mystery_crime": "Suspenseful detective noir, mystery & true-crime investigation atmosphere with dramatic shadows and clues",
    "psychology_mindset": "Deep human psychology, mindset & behavioral contrast atmosphere with expressive body language",
    "history_mythology": "Immersive historical period, ancient civilization & mythological setting details",
    "biography_empire": "Chronological rise, ambition & empire-building storytelling atmosphere",
    "survival_disaster": "High-stakes extreme survival & dramatic urgency atmosphere",
    "motivation_stoic": "Calm, powerful stoic discipline, focus & self-mastery atmosphere",
    "explainer_guide": "Clear, engaging step-by-step visual breakdown with tangible real-world examples",
}


def _translate_beat_batch_via_gemini(
    batch_items: list,
    niche_label: str,
    niche_type: str,
    script_type: str,
    char_name: str,
    char_name_mode: str,
    custom_desc: str,
    batch_idx: int = 0,
) -> dict:
    """
    Converts a batch of (idx, beat_text) into literal, concise PURE ENGLISH visual scene descriptions
    using Gemini so every single image strictly shows the exact spoken words/actions/objects
    with a distinct camera angle on every scene, mixing CHAR scenes and WORLD (B-roll/object) scenes,
    and maintaining outfit continuity within each scene/timeline while changing outfits across different scenes/timelines.
    Returns dict {idx: (scene_tag, literal_visual_description)}.
    """
    numbered_lines = "\n".join(f"{idx}: {beat}" for idx, beat in batch_items)
    if niche_type == "stickman":
        char_rule = (
            "For CHAR scenes, refer to the protagonist simply as 'the stickman main character' performing or reacting to the exact spoken action."
        )
    else:
        char_rule = (
            "For CHAR scenes, refer to the protagonist simply as 'the main character' and ALWAYS include his current outfit (e.g., 'wearing a navy hoodie', 'wearing a light-blue button-down shirt', 'wearing an olive jacket over a white tee', 'wearing a charcoal sweater'). "
            "CRITICAL OUTFIT CONTINUITY RULE: Keep his outfit 100% IDENTICAL across all consecutive beats that take place in the SAME scene/location/timeline, and CHANGE his outfit whenever the story moves to a DIFFERENT scene, location, day, or timeline!"
        )

    prompt = f"""You are a Strict Literal Storyboard Visual Director for a 16:9 Widescreen YouTube video.
Your job is to convert each numbered spoken script line below into a CONCISE, NORMAL-LENGTH (14 to 24 words) 100% LITERAL ENGLISH VISUAL DESCRIPTION showing EXACTLY what is being spoken in that line, with a DIFFERENT camera angle and composition on every consecutive image.

STRICT MANDATORY RULES:
1. 100% PURE ENGLISH ONLY (ZERO HINDI / DEVANAGARI LETTERS IN OUTPUT).
2. LITERAL 1-TO-1 VISUAL MATCH ("JO BOLA JA RAHA HAI WOHI DIKHANA HAI"):
   - Directly depict the exact action, object, place, number, expression, or concept mentioned in that script line.
3. MIX OF MAIN CHARACTER (`CHAR`) AND OBJECT / ENVIRONMENT B-ROLL (`WORLD`) SCENES:
   - Not every image needs the main character!
   - Use `CHAR` (~65% to 75% of lines) when the main character is personally doing an action, reacting, working, or experiencing the moment.
   - Use `WORLD` (~25% to 35% of lines) when the spoken line focuses on an object, screen, chart, money, clock, receipt, building, city street, system, or other people — describe ONLY that object, environment, or B-roll scene WITHOUT the main character!
4. EVERY IMAGE MUST BE DIFFERENT & HAVE A DIFFERENT ANGLE ("HAR IMAGE DIFF HO, SAME NA HO"):
   - Never repeat the same camera angle, pose, or layout on consecutive lines! Rotate between wide shot, close-up on hands/object, over-the-shoulder POV, side profile, low angle, and three-quarter angle.
5. {char_rule}
6. OUTPUT FORMAT:
   - Output ONLY one line per item in this exact format:
     <number> | CHAR | <14-24 word concrete English visual description including outfit continuity & distinct angle>
     OR
     <number> | WORLD | <14-24 word concrete English B-roll / object / environment visual description without main character>

SPOKEN SCRIPT LINES:
{numbered_lines}"""

    payload = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.25,
            "maxOutputTokens": 6000,
        },
    }).encode("utf-8")

    num_keys = len(GEMINI_API_KEYS)
    ordered_keys = [GEMINI_API_KEYS[(batch_idx + k) % num_keys] for k in range(num_keys)]
    fast_models = [
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-3.1-flash-lite-preview",
        "gemini-3-flash-preview",
        "gemini-2.0-flash",
    ]

    raw_out = ""
    for model_name in fast_models:
        for api_key in ordered_keys[:4]:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            try:
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    candidates = res.get("candidates") or []
                    if candidates:
                        parts = (candidates[0].get("content") or {}).get("parts") or []
                        text_out = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                        if text_out:
                            raw_out = text_out
                            break
            except Exception:
                continue
        if raw_out:
            break

    parsed = {}
    if raw_out:
        for line in raw_out.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^(\d+)\s*\|\s*(CHAR|WORLD)\s*\|\s*(.+)$", line, flags=re.I)
            if m:
                b_idx = int(m.group(1))
                tag = m.group(2).strip().upper()
                desc = _ensure_pure_english_prompt(m.group(3).strip().rstrip("."))
                if desc:
                    parsed[b_idx] = (tag, desc)
            else:
                m2 = re.match(r"^(\d+)[\.:\)\-\s]+(.+)$", line)
                if m2:
                    b_idx = int(m2.group(1))
                    rest = m2.group(2).strip()
                    tag_m = re.search(r"\b(CHAR|WORLD)\b", rest, flags=re.I)
                    tag = tag_m.group(1).upper() if tag_m else "CHAR"
                    rest = re.sub(r"\[?(CHAR|WORLD)\]?\s*[:\-–—\|]*\s*", "", rest, flags=re.I).strip().rstrip(".")
                    rest = _ensure_pure_english_prompt(rest)
                    if rest:
                        parsed[b_idx] = (tag, rest)
    return parsed


def translate_beats_to_literal_visual_scenes(
    beats: list,
    effective_niche: str,
    niche_type: str,
    script_type: str,
    char_name: str,
    char_name_mode: str,
    custom_desc: str,
) -> list:
    """
    Translates all script beats into strict literal PURE ENGLISH visual scene descriptions in parallel batches,
    assigning each batch a distinct API key so all batches complete quickly in parallel.
    """
    total = len(beats)
    if total == 0:
        return []

    niche_label = NICHE_VISUAL_OVERRIDES.get(effective_niche, effective_niche)
    batch_size = 35
    batches = []
    for start_i in range(0, total, batch_size):
        chunk = [(i + 1, beats[i]) for i in range(start_i, min(total, start_i + batch_size))]
        batches.append(chunk)

    combined_map = {}
    try:
        max_workers = min(10, max(1, len(batches)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    _translate_beat_batch_via_gemini,
                    batch,
                    niche_label,
                    niche_type,
                    script_type,
                    char_name,
                    char_name_mode,
                    custom_desc,
                    b_idx,
                )
                for b_idx, batch in enumerate(batches)
            ]
            for fut in concurrent.futures.as_completed(futures, timeout=26):
                try:
                    batch_res = fut.result()
                    if batch_res:
                        combined_map.update(batch_res)
                except Exception:
                    pass
    except Exception:
        pass

    # Retry any missed beats once so no beat is left untranslated
    missing_items = [(idx, beat) for idx, beat in enumerate(beats, start=1) if idx not in combined_map or not combined_map[idx][1]]
    if missing_items:
        try:
            retry_res = _translate_beat_batch_via_gemini(
                missing_items[:40],
                niche_label,
                niche_type,
                script_type,
                char_name,
                char_name_mode,
                custom_desc,
                len(batches) + 1,
            )
            if retry_res:
                combined_map.update(retry_res)
        except Exception:
            pass

    # Track scene/timeline blocks so fallback or missing outfits stay consistent within a scene and change on new scenes
    results = []
    current_outfit_idx = 0
    last_loc_key = None
    beats_in_current_scene = 0

    for idx, beat in enumerate(beats, start=1):
        loc_key = detect_explicit_location_key(beat)
        time_shift = bool(re.search(r"\b(next day|years later|months later|that night|the next morning|later that evening|at work|at the office|back home|outside|at the bank|at the store)\b", beat, flags=re.I))
        if (loc_key and loc_key != last_loc_key and beats_in_current_scene >= 2) or time_shift or beats_in_current_scene >= 6:
            current_outfit_idx = (current_outfit_idx + 1) % len(TIMELINE_OUTFIT_PALETTE)
            beats_in_current_scene = 0
        if loc_key:
            last_loc_key = loc_key
        beats_in_current_scene += 1
        timeline_outfit = TIMELINE_OUTFIT_PALETTE[current_outfit_idx]

        if idx in combined_map and combined_map[idx][1]:
            tag, desc = combined_map[idx]
            if tag == "CHAR" and niche_type != "stickman":
                # Ensure outfit continuity is explicitly mentioned if Gemini omitted clothing words
                if not re.search(r"\b(wearing|shirt|hoodie|jacket|sweater|suit|t-shirt|tee|blazer|cardigan|coat|outfit)\b", desc, flags=re.I):
                    desc = f"{desc} ({timeline_outfit})"
            results.append((tag, desc))
        else:
            results.append(
                build_strict_literal_scene_fallback(
                    beat, idx, effective_niche, niche_type, char_name, char_name_mode, timeline_outfit
                )
            )
    return results


WHITE_CHAR_CONSISTENCY_HEADER = """**CHARACTER CONSISTENCY PROMPT**

Use the uploaded reference image as the permanent reference for the main character. Keep him EXACTLY the same in every image: same pale skin, face shape, simple black oval eyes, eyebrows, nose, subtle smile, dark brown wavy hair, hairstyle, body proportions, and clean black outlines.

**Style:** Polished 2D cartoon character with detailed semi-realistic painted backgrounds, cinematic lighting, soft cel shading, and warm natural colors.

**Continuity:** Change only the pose, facial expression, environment, and outfit when the story requires it. Maintain the same character identity across all scenes and ages, with only subtle natural aging. Keep the same outfit within the same timeline.

**Format:** 16:9 landscape, cinematic composition, clear foreground and background, suitable for YouTube storytelling.

**Negative rules:** No character redesign, different face, hairstyle, skin tone, or eyes. No realistic or colored eyes, photorealism, 3D, distorted hands, extra fingers, duplicated characters, watermarks, or unrequested text."""

WORLD_SCENE_STYLE_HEADER = """**Style:** Polished 2D illustration with detailed semi-realistic painted backgrounds, clean black outlines, cinematic lighting, soft cel shading, and warm natural colors.

**Format:** 16:9 landscape, cinematic composition, clear foreground and background, suitable for YouTube storytelling.

**Negative rules:** No photorealism, 3D, distorted hands, extra fingers, watermarks, or unrequested text."""


def generate_full_prompts_document(
    script_text: str,
    niche_config: dict = None,
    start_image_index: int = 1,
    pre_beats: list = None,
) -> dict:
    niche_config = niche_config or {}
    raw_niche = (niche_config.get("niche_type") or "white_char").strip().lower()
    niche_type = "stickman" if raw_niche == "stickman" else "white_char"
    custom_desc = ""
    char_name = (niche_config.get("fixed_char_name") or niche_config.get("char_name") or "").strip()
    char_name_mode = (niche_config.get("char_name_mode") or "fixed").strip().lower()
    script_type = (niche_config.get("script_type") or "wealth_story").strip().lower()

    effective_niche = niche_type

    if isinstance(pre_beats, list) and len(pre_beats) > 0:
        beats = [str(b).strip() for b in pre_beats if str(b).strip()]
    else:
        beats = split_script_into_3_4_sec_beats(script_text)

    total_scenes = len(beats)
    start_idx = max(1, int(start_image_index or 1))

    # Translate every spoken beat into a 100% literal PURE ENGLISH visual scene description
    literal_scenes = translate_beats_to_literal_visual_scenes(
        beats, effective_niche, niche_type, script_type, char_name, char_name_mode, custom_desc
    )

    lines = []
    total_est_sec = 0.0

    shot_cycle = [
        "wide-angle establishing shot",
        "close-up focal detail angle",
        "over-the-shoulder POV shot",
        "dramatic side-profile angle",
        "low-angle upward perspective",
        "high-angle top-down perspective",
        "three-quarter medium shot",
        "foreground object focus angle",
    ]

    for local_i, beat in enumerate(beats):
        img_num = start_idx + local_i
        est_sec, _dur_bucket = get_beat_duration_info(beat)
        total_est_sec += est_sec
        scene_tag, literal_scene = literal_scenes[local_i]
        scene_tag = (scene_tag or "CHAR").strip().upper()
        literal_scene = _ensure_pure_english_prompt(literal_scene)

        money_matches = re.findall(
            r"\$\d[\d,]*(?:\.\d+)?(?:\s*(?:k|m|million|thousand|billion))?|\b\d+(?:\.\d+)?%|\b\d{1,3}(?:,\d{3})+\b",
            beat,
            flags=re.I,
        )
        num_visual = ""
        if money_matches:
            num_visual = f" Clear in-frame visual readout showing '{money_matches[0]}'."

        shot_label = shot_cycle[(img_num - 1) % len(shot_cycle)]

        if scene_tag == "WORLD":
            action_prompt = _ensure_pure_english_prompt(
                f"Create the image in 16:9 widescreen 2D cartoon illustration ({shot_label}, completely different camera angle and scene from previous image, B-roll / object / environment focus without main character): {literal_scene}.{num_visual} "
                f"All other characters (if any) have normal human skin tones; strict normal anatomy only (1 head, 2 hands, 2 legs — NO 3 hands, NO 3 legs, NO 2 heads, and no anatomy glitches)."
            )
            lines.append(f"Image {img_num}")
            lines.append("")
            lines.append(WORLD_SCENE_STYLE_HEADER)
            lines.append("")
            lines.append("**SCENE:**")
            lines.append(f"Script: {beat}")
            lines.append(f"Prompt: {action_prompt}")
            lines.append("")
        elif niche_type == "stickman":
            action_prompt = _ensure_pure_english_prompt(
                f"Create the image in 16:9 widescreen 2D cartoon illustration ({shot_label}, completely different camera angle and scene from previous image): {literal_scene}.{num_visual} "
                f"Main character must stay 100% identical to the attached reference image with pure white stickman head (no character change); strict normal anatomy only (1 head, 2 hands, 2 legs — NO 3 hands, NO 3 legs, NO 2 heads, and no anatomy glitches)."
            )
            lines.append(f"Image {img_num}")
            lines.append("")
            lines.append("**SCENE:**")
            lines.append(f"Script: {beat}")
            lines.append(f"Prompt: {action_prompt}")
            lines.append("")
        else:
            action_prompt = _ensure_pure_english_prompt(
                f"Create the image in 16:9 widescreen 2D cartoon illustration ({shot_label}, completely different camera angle and scene from previous image): {literal_scene}.{num_visual} "
                f"Main character must stay 100% identical to the attached reference image with pure white skin tone (never change character, face, outfit, or skin tone), while all other characters have normal human skin tones; strict normal anatomy only (1 head, 2 hands, 2 legs — NO 3 hands, NO 3 legs, NO 2 heads, and no anatomy glitches)."
            )
            lines.append(f"Image {img_num}")
            lines.append("")
            lines.append(WHITE_CHAR_CONSISTENCY_HEADER)
            lines.append("")
            lines.append("**SCENE:**")
            lines.append(f"Script: {beat}")
            lines.append(f"Prompt: {action_prompt}")
            lines.append("")

    full_text = "\n".join(lines).strip()
    total_words_in_script = len(" ".join(beats).split()) if beats else len(script_text.split())
    est_mins = round(max(0.2, total_words_in_script / 140.0), 1)
    return {
        "ok": True,
        "total_images": total_scenes,
        "estimated_duration_mins": est_mins,
        "prompts_text": full_text,
    }


FREE_PROTAGONIST_NAMES = ["Ryan", "Ethan", "Marcus", "Lucas", "Noah", "Caleb", "Liam", "Owen", "Logan", "Julian", "Nathan", "Cole"]
HINDI_PROTAGONIST_NAMES = ["आरव", "रोहन", "कबीर", "विक्रम", "अर्जुन", "देव", "समीर", "आदित्य"]


def _get_rotated_gemini_keys() -> list:
    active_keys = list(GEMINI_API_KEYS[:9])
    random.shuffle(active_keys)
    if len(GEMINI_API_KEYS) > 9:
        active_keys.extend(GEMINI_API_KEYS[9:])
    return active_keys


def call_gemini_api(prompt_text: str, max_tokens: int = 4096) -> str:
    payload = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": 0.75,
            "maxOutputTokens": max(4096, int(max_tokens * 2)),
        }
    }).encode("utf-8")

    last_err = None
    rotated_keys = _get_rotated_gemini_keys()
    for model_name in GEMINI_MODELS:
        for api_key in rotated_keys:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            try:
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=28) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    candidates = res.get("candidates") or []
                    if candidates:
                        parts = (candidates[0].get("content") or {}).get("parts") or []
                        text_out = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                        if text_out:
                            return text_out
            except Exception as e:
                last_err = f"{model_name}: {str(e)}"
                continue
    raise RuntimeError(last_err or "All Gemini API keys/models failed")


def ai_vision_sync_scenes_batch(scenes_list: list, words_list: list) -> list:
    """
    Uses Gemini 2.5 Flash Multimodal Vision to inspect a batch of scene images (thumbnails)
    alongside a numbered window of spoken transcript words [W<idx>] <word>, and locks each
    image to the exact contiguous range of spoken word indices [w_start, w_end] where that
    visual scene is being narrated.
    """
    if not scenes_list or not words_list:
        return []

    parts = []
    min_w = int(words_list[0].get("w_idx", 0))
    max_w = int(words_list[-1].get("w_idx", len(words_list) - 1))
    n_scenes = len(scenes_list)

    intro_text = (
        "You are an expert AI Film & YouTube Documentary Video Editor with multimodal vision.\n"
        f"Below are {n_scenes} chronological visual scene images (Scene {scenes_list[0].get('scene_num')} to Scene {scenes_list[-1].get('scene_num')}) "
        f"and the spoken voiceover transcript words numbered from [W{min_w}] to [W{max_w}].\n\n"
        "YOUR TASK:\n"
        "1. Look closely at what is visually drawn in each image (the character's action, setting/location, objects, numbers/text on screen, emotion, and any Script Hint).\n"
        "2. Read the numbered spoken words and lock each image in strict chronological order to the exact contiguous word index range [w_start, w_end] where that image's visual content is being spoken.\n"
        f"3. Every word from W{min_w} to W{max_w} must be covered in contiguous order: Scene 1 starts at W{min_w}, each next scene starts at previous w_end + 1, and the last scene ends at W{max_w}.\n"
        "4. Place scene cuts at natural phrase/sentence boundaries or spoken pauses whenever possible, while ensuring every image stays on screen during the exact words that describe what is shown in that image.\n\n"
        "=== SCENE IMAGES IN CHRONOLOGICAL ORDER ===\n"
    )
    parts.append({"text": intro_text})

    for sc in scenes_list:
        s_num = int(sc.get("scene_num", 1))
        hint = (sc.get("script_hint") or "").strip()
        b64_raw = (sc.get("image_b64") or "").strip()
        if "," in b64_raw:
            b64_raw = b64_raw.split(",", 1)[1].strip()
        label = f"\n--- IMAGE FOR SCENE {s_num} ---"
        if hint:
            label += f"\nExpected Spoken Line / Hint for Scene {s_num}: \"{hint}\""
        label += "\n"
        parts.append({"text": label})
        if b64_raw:
            parts.append({
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": b64_raw
                }
            })

    words_lines = []
    cur_line = []
    for w_obj in words_list:
        wi = int(w_obj.get("w_idx", 0))
        wt = str(w_obj.get("word") or "").strip()
        st = float(w_obj.get("start") or 0.0)
        cur_line.append(f"[W{wi}@{st:.1f}s] {wt}")
        if len(cur_line) >= 12:
            words_lines.append(" ".join(cur_line))
            cur_line = []
    if cur_line:
        words_lines.append(" ".join(cur_line))

    outro_text = (
        "\n\n=== NUMBERED SPOKEN TRANSCRIPT WORDS ===\n"
        + "\n".join(words_lines)
        + "\n\n=== REQUIRED OUTPUT FORMAT ===\n"
        "Output ONLY one line per scene in this exact pipe-separated format (no markdown, no extra commentary):\n"
        f"SCENE {scenes_list[0].get('scene_num')} | W_START | W_END | <brief 4-word visual match note>\n"
    )
    parts.append({"text": outro_text})

    payload = json.dumps({
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.15,
            "maxOutputTokens": 2048,
        }
    }).encode("utf-8")

    rotated_keys = _get_rotated_gemini_keys()
    raw_out = ""
    vision_models = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]
    for model_name in vision_models:
        if raw_out:
            break
        for api_key in rotated_keys[:5]:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            try:
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=22) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    candidates = res.get("candidates") or []
                    if candidates:
                        c_parts = (candidates[0].get("content") or {}).get("parts") or []
                        txt = "".join(p.get("text", "") for p in c_parts if not p.get("thought")).strip()
                        if txt:
                            raw_out = txt
                            break
            except Exception:
                continue

    parsed_map = {}
    if raw_out:
        for line in raw_out.splitlines():
            m = re.search(r"SCENE\s*(\d+)\s*\|\s*W?(\d+)\s*\|\s*W?(\d+)", line, flags=re.I)
            if m:
                s_id = int(m.group(1))
                w0 = int(m.group(2))
                w1 = int(m.group(3))
                parsed_map[s_id] = (w0, w1)

    results = []
    for sc in scenes_list:
        s_num = int(sc.get("scene_num", 1))
        if s_num in parsed_map:
            w0, w1 = parsed_map[s_num]
            results.append({"scene_num": s_num, "w_start": w0, "w_end": max(w0, w1)})
    return results


_YT_REF_CACHE = {}

MONEYLIFEPOV_AND_QUIETCAPITAL_MASTER_BLUEPRINT = """
=== MASTER STORYTELLING ENGINE: @MoneyLifePOV & @QuietCapitalUSA IMMERSIVE POV STYLE ===
1. VIEWER IMMERSION ("JO USER VIDEO DEKHE USEY LAGEY WOH KHUD HAI"):
   - Write in an addictive, intimate, hyper-relatable POV storytelling voice modeled directly after YouTube channels @MoneyLifePOV ("Money Life POV") and @QuietCapitalUSA ("Quiet Capital").
   - Make the viewer feel like THEY are the quiet, underestimated protagonist living every single scene firsthand.
   - The opening scene, environment, characters, and props MUST be 100% custom-tailored to the SPECIFIC topic and premise!

2. ABSOLUTE BAN ON REPETITIVE CLICHÉS & COPY-PASTE TROPES:
   - FORBIDDEN: Do NOT mention "2012 Toyota Camry", "2014 Honda Civic", "faded paint job", "brother-in-law Mark", "coworker Dave", "gravel driveway", or "financed $900 watch". These are tired clichés and are strictly banned!
   - Every single video script MUST feature a completely unique, fresh opening scene, new specific characters, unique dialogue, and an environment directly matching the chosen topic.

3. DYNAMIC TOPIC-ANCHORED SCENE CRAFTING:
   - Carefully analyze every keyword in the topic title. If the topic mentions:
     * "Friends think you're unemployed": The scene MUST start with real friends (e.g. coffee shop, casual diner, group chat, living room hangout) reacting to your perceived lack of a traditional 9-to-5 job, pitying you, or offering unsolicited job leads.
     * "Trading / Market": The story MUST depict authentic trading details—pre-market prep, candlestick charts, liquidity sweeps, disciplined risk-to-reward ratio, execution, patience, closing the laptop before noon, and emotional control vs gambling.
     * "Investing / Real Estate / Tech / Business": The scenes must feature the real environments of that specific world—not generic office parking lots!

4. VISUAL MICRO-SCENES EVERY 3 TO 4 SECONDS (7–9 WORDS PER BEAT):
   - Every single sentence or clause (every 3 to 4 seconds of speech) MUST depict a concrete, visible physical action, real-world object, facial expression, setting, or exact dollar number that can be drawn on screen!
   - NEVER write boring textbook lectures, abstract advice, or generic filler. Show, don't tell!

5. SIGNATURE 4-ACT NARRATIVE ARC:
   - ACT 1 (Cold-Open Hook): Drop immediately into an ultra-specific, vivid everyday scene matching the topic title where external expectations clash with your quiet reality.
   - ACT 2 (The Hidden Reality & Daily Contrast): Show the day-to-day discipline, exact routines, numbers, and systems you quietly execute while outsiders have no idea how it works.
   - ACT 3 (The Turning Point / Undeniable Moment): Deliver a deeply satisfying, realistic moment of truth or realization where the outcome of your quiet discipline becomes crystal clear.
   - ACT 4 (The Psychological Truth): Close with grounded, memorable truths about freedom, calm conviction, and owning your time over seeking external validation.
"""


_DEFAULT_YT_KEYS_B64 = "QUl6YVN5RHVaLU1SdDMyd0pNOElxWXFUR29VLWN6T3REaW1KaVZnCkFJemFTeUJvdUxTcGRuMnVZZHBVSWktZ1RjY3Y4MmcwVF9Ic0pMNA=="

def _load_yt_keys():
    env_k = os.environ.get("YOUTUBE_DATA_API_KEY") or os.environ.get("YOUTUBE_DATA_API_KEYS")
    if env_k:
        keys = [k.strip() for k in env_k.replace(",", "\n").split() if k.strip()]
        if keys:
            return keys
    try:
        return [k.strip() for k in base64.b64decode(_DEFAULT_YT_KEYS_B64).decode("utf-8").split() if k.strip()]
    except Exception:
        return []

YOUTUBE_DATA_API_KEYS = _load_yt_keys()

YOUTUBE_DATA_API_KEY = YOUTUBE_DATA_API_KEYS[0]
_YT_API_RESEARCH_CACHE = {}
_yt_key_rr_idx = 0


def _call_youtube_data_api_json(base_url_without_key: str, timeout: int = 5) -> dict:
    """Calls YouTube Data API v3 with automatic round-robin rotation and instant failover across all YOUTUBE_DATA_API_KEYS."""
    global _yt_key_rr_idx
    if not YOUTUBE_DATA_API_KEYS:
        return {}
    n_keys = len(YOUTUBE_DATA_API_KEYS)
    start_idx = _yt_key_rr_idx % n_keys
    _yt_key_rr_idx += 1

    sep = "&" if "?" in base_url_without_key else "?"
    for offset in range(n_keys):
        k = YOUTUBE_DATA_API_KEYS[(start_idx + offset) % n_keys]
        full_url = f"{base_url_without_key}{sep}key={urllib.parse.quote(k)}"
        try:
            req = urllib.request.Request(full_url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                if isinstance(data, dict) and not data.get("error"):
                    return data
        except Exception:
            continue
    return {}


def fetch_youtube_api_viral_research(query_keywords: str = "", channel_or_video_ref: str = "") -> str:
    """Uses official YouTube Data API v3 (rotating across all YOUTUBE_DATA_API_KEYS) to fetch:
    1) Top viral videos, titles & descriptions from a Reference YouTube Channel or Video (if provided).
    2) Top performing viral YouTube videos & hooks matching the user's topic/keywords on YouTube right now.
    """
    q_clean = (query_keywords or "").strip()
    ref_clean = (channel_or_video_ref or "").strip()
    cache_key = f"{q_clean.lower()}||{ref_clean.lower()}"
    if cache_key in _YT_API_RESEARCH_CACHE:
        return _YT_API_RESEARCH_CACHE[cache_key]

    import html as html_mod

    report_lines = []
    resolved_channel_id = ""
    resolved_channel_title = ""

    # 1. If user provided a Reference YouTube Channel URL, @Handle, Channel Name, or Video URL
    if ref_clean and YOUTUBE_DATA_API_KEYS:
        try:
            # Check if it's a video link first
            vid_m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", ref_clean)
            if vid_m:
                vid_id = vid_m.group(1)
                v_url = (
                    f"https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics"
                    f"&id={urllib.parse.quote(vid_id)}"
                )
                v_data = _call_youtube_data_api_json(v_url, timeout=5)
                items = v_data.get("items") or []
                if items:
                    sn = items[0].get("snippet") or {}
                    st = items[0].get("statistics") or {}
                    resolved_channel_id = sn.get("channelId") or ""
                    resolved_channel_title = sn.get("channelTitle") or ""
                    v_title = sn.get("title") or ""
                    v_desc = re.sub(r"\s+", " ", (sn.get("description") or "")).strip()[:500]
                    v_views = st.get("viewCount") or ""
                    report_lines.append(
                        f'Reference YouTube Video: "{v_title}" by Channel "{resolved_channel_title}" '
                        f'(Views: {v_views}) — Hook/Description: {v_desc}'
                    )

            # Check if it's a channel URL/handle (e.g. youtube.com/@Handle or @Handle or youtube.com/channel/UC...)
            if not resolved_channel_id:
                ch_id_m = re.search(r"youtube\.com/channel/(UC[A-Za-z0-9_-]{16,})", ref_clean)
                handle_m = re.search(r"@([A-Za-z0-9_.-]+)", ref_clean)
                if ch_id_m:
                    resolved_channel_id = ch_id_m.group(1)
                elif handle_m:
                    handle_str = handle_m.group(1)
                    ch_url = (
                        f"https://www.googleapis.com/youtube/v3/channels?part=snippet,statistics"
                        f"&forHandle={urllib.parse.quote('@' + handle_str)}"
                    )
                    ch_data = _call_youtube_data_api_json(ch_url, timeout=5)
                    c_items = ch_data.get("items") or []
                    if c_items:
                        resolved_channel_id = c_items[0].get("id") or ""
                        sn = c_items[0].get("snippet") or {}
                        resolved_channel_title = sn.get("title") or handle_str
                        c_desc = re.sub(r"\s+", " ", (sn.get("description") or "")).strip()[:400]
                        report_lines.append(f'Reference YouTube Channel: "{resolved_channel_title}" (@{handle_str}) — Theme: {c_desc}')

                # Fallback: search for the channel by name/URL text via YouTube Data API
                if not resolved_channel_id:
                    q_ch = re.sub(r"https?://(?:www\.)?youtube\.com/?", "", ref_clean).strip("/ ")
                    if q_ch:
                        s_url = (
                            f"https://www.googleapis.com/youtube/v3/search?part=snippet&type=channel&maxResults=1"
                            f"&q={urllib.parse.quote(q_ch)}"
                        )
                        s_data = _call_youtube_data_api_json(s_url, timeout=5)
                        s_items = s_data.get("items") or []
                        if s_items:
                            sn = s_items[0].get("snippet") or {}
                            resolved_channel_id = sn.get("channelId") or (s_items[0].get("id") or {}).get("channelId") or ""
                            resolved_channel_title = sn.get("title") or q_ch
                            c_desc = re.sub(r"\s+", " ", (sn.get("description") or "")).strip()[:350]
                            report_lines.append(f'Reference YouTube Channel: "{resolved_channel_title}" — Theme: {c_desc}')

            # Fetch top-performing viral videos from this Reference Channel
            if resolved_channel_id:
                top_v_url = (
                    f"https://www.googleapis.com/youtube/v3/search?part=snippet&channelId={urllib.parse.quote(resolved_channel_id)}"
                    f"&type=video&order=viewCount&maxResults=12"
                )
                tv_data = _call_youtube_data_api_json(top_v_url, timeout=5)
                tv_items = tv_data.get("items") or []
                ch_titles = []
                for it in tv_items:
                    sn = it.get("snippet") or {}
                    t_str = html_mod.unescape(sn.get("title") or "").strip()
                    d_str = html_mod.unescape(re.sub(r"\s+", " ", (sn.get("description") or "")).strip())[:160]
                    if t_str:
                        ch_titles.append(f'"{t_str}"' + (f' ({d_str})' if d_str else ''))
                if ch_titles:
                    report_lines.append(
                        f'Reference Channel ({resolved_channel_title or resolved_channel_id}) Most-Viewed Viral Videos on YouTube: '
                        + " | ".join(ch_titles[:10])
                    )
        except Exception:
            pass

        # Also include scraped/oembed fallback info if any
        try:
            scraped = fetch_youtube_reference_context(ref_clean)
            if scraped:
                report_lines.append(scraped)
        except Exception:
            pass

    # 2. Search YouTube Data API v3 for high-performing viral videos matching the user's Topic / Keywords
    if YOUTUBE_DATA_API_KEYS:
        search_q = q_clean if q_clean else "POV quiet wealth financial freedom stocks investing storytelling"
        try:
            yt_search_url = (
                f"https://www.googleapis.com/youtube/v3/search?part=snippet&type=video&order=viewCount"
                f"&maxResults=10&q={urllib.parse.quote(search_q)}"
            )
            sr_data = _call_youtube_data_api_json(yt_search_url, timeout=5)
            sr_items = sr_data.get("items") or []
            viral_hits = []
            for it in sr_items:
                sn = it.get("snippet") or {}
                v_title = html_mod.unescape(sn.get("title") or "").strip()
                v_ch = html_mod.unescape(sn.get("channelTitle") or "").strip()
                v_desc = html_mod.unescape(re.sub(r"\s+", " ", (sn.get("description") or "")).strip())[:150]
                if v_title:
                    viral_hits.append(f'"{v_title}" [by {v_ch}]' + (f' — {v_desc}' if v_desc else ''))
            if viral_hits:
                report_lines.append(
                    f'Top-Performing Viral YouTube Videos Currently Trending for "{search_q}": '
                    + " || ".join(viral_hits[:10])
                )
        except Exception:
            pass

    final_report = "\n".join(report_lines).strip()
    if final_report:
        _YT_API_RESEARCH_CACHE[cache_key] = final_report
    return final_report


def fetch_youtube_reference_context(yt_url: str) -> str:
    if not yt_url:
        return ""
    yt_url = yt_url.strip()
    if yt_url in _YT_REF_CACHE:
        return _YT_REF_CACHE[yt_url]

    import html as html_mod

    low_url = yt_url.lower()
    info_parts = [f"Reference YouTube URL: {yt_url}"]

    # Built-in instant recognition for @MoneyLifePOV and @QuietCapitalUSA channel/video references
    if "moneylifepov" in low_url:
        info_parts.append('Reference Channel Name / Style: Money Life POV (@MoneyLifePOV)')
        info_parts.append(
            'Reference Channel Signature Titles & Style: "POV: You Look Broke — But You\'re Quietly Getting Rich", '
            '"POV: Your Coworkers Thought You Were Broke — Then You Retired First", '
            '"POV: Your Uncle Jokes About Your Van — You Have $572,886 and Nobody Knows", '
            '"POV: You Gave Up Looking Rich in Your 30s — Your 40s Looked Completely Different". '
            'Second-person immersive POV ("You") financial & life storytelling contrasting loud fake wealth with quiet stealth wealth, exact dollar figures, and vivid 3–4 second visual scenes.'
        )
    if "quietcapital" in low_url:
        info_parts.append('Reference Channel Name / Style: Quiet Capital (@QuietCapitalUSA)')
        info_parts.append(
            'Reference Channel Signature Titles & Style: "POV: You Lived Below Your Means — Then Quietly Retired Decades Earlier", '
            '"POV: Your Friends Kept Showing Off — You Quietly Became a Millionaire", '
            '"POV: Your Investment Income Passed Your Salary — Then Work Became Optional", '
            '"POV: You Fix Cars for a Living — Nobody Knows You Have $5 Million". '
            'Calm, cinematic second-person POV ("You") stealth-wealth storytelling where the viewer feels they are the underestimated protagonist.'
        )

    # Extract YouTube Video ID from watch?v=, youtu.be/, shorts/, embed/, live/
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", yt_url)
    vid_id = m.group(1) if m else ""
    canonical_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else yt_url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    got_title = False
    got_desc = False
    got_captions = False
    caption_urls = []

    # If it's a Channel URL (e.g. youtube.com/@ChannelName), scrape recent video titles & channel description
    if not vid_id and ("youtube.com/" in low_url or "youtu.be/" in low_url):
        try:
            ch_url = yt_url.rstrip("/")
            if "/@" in ch_url and not ch_url.endswith("/videos"):
                ch_url_videos = ch_url + "/videos"
            else:
                ch_url_videos = ch_url
            req = urllib.request.Request(ch_url_videos, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=6) as resp:
                ch_html = resp.read().decode("utf-8", errors="ignore")
                tm = re.search(r"<title>(.*?)</title>", ch_html, flags=re.I | re.S)
                if tm:
                    c_title = html_mod.unescape(tm.group(1)).replace("- YouTube", "").strip()
                    if c_title:
                        info_parts.append(f"Reference YouTube Channel: \"{c_title}\"")
                v_titles = re.findall(r'"title":\{"runs":\[\{"text":"((?:\\.|[^"\\]){12,110})"\}', ch_html)
                seen_vt = []
                for vt in v_titles:
                    clean_vt = html_mod.unescape(vt).strip()
                    if clean_vt and clean_vt not in seen_vt and not clean_vt.lower().startswith(("home", "videos", "shorts", "playlists")):
                        seen_vt.append(clean_vt)
                    if len(seen_vt) >= 10:
                        break
                if seen_vt:
                    info_parts.append("Reference Channel Popular Video Titles (match this exact POV framing & tone): " + " | ".join(f'"{t}"' for t in seen_vt))
        except Exception:
            pass

    # 1. Fetch oEmbed for exact Title & Channel Name
    if vid_id:
        try:
            oembed_url = f"https://www.youtube.com/oembed?url={urllib.parse.quote(canonical_url, safe='')}&format=json"
            req = urllib.request.Request(oembed_url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                odata = json.loads(resp.read().decode("utf-8", errors="ignore"))
                if odata.get("title"):
                    info_parts.append(f"Reference Video Title: \"{odata['title']}\"")
                    got_title = True
                if odata.get("author_name"):
                    info_parts.append(f"Reference Channel Name / Style: {odata['author_name']}")
        except Exception:
            pass

    # 2. Try YouTube Innertube Player API (ANDROID/WEB client) for reliable Title, Full Description, Keywords & Caption Tracks
    if vid_id:
        for client_cfg in [
            {"clientName": "WEB", "clientVersion": "2.20240726.00.00"},
            {"clientName": "IOS", "clientVersion": "19.29.1", "deviceMake": "Apple", "deviceModel": "iPhone16,2"},
        ]:
            try:
                ib_payload = json.dumps({
                    "videoId": vid_id,
                    "context": {"client": client_cfg},
                }).encode("utf-8")
                ib_req = urllib.request.Request(
                    "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                    data=ib_payload,
                    headers={"Content-Type": "application/json", "User-Agent": headers["User-Agent"]},
                    method="POST",
                )
                with urllib.request.urlopen(ib_req, timeout=5) as ib_resp:
                    ib_data = json.loads(ib_resp.read().decode("utf-8", errors="ignore"))
                    v_details = ib_data.get("videoDetails") or {}
                    if not got_title and v_details.get("title"):
                        info_parts.append(f"Reference Video Title: \"{v_details['title']}\"")
                        got_title = True
                    if v_details.get("author") and not any("Reference Channel" in p for p in info_parts):
                        info_parts.append(f"Reference Channel Name / Style: {v_details['author']}")
                    if not got_desc and v_details.get("shortDescription"):
                        clean_desc = re.sub(r"\s+", " ", v_details["shortDescription"]).strip()[:1100]
                        if clean_desc:
                            info_parts.append(f"Reference Video Description & Breakdown: {clean_desc}")
                            got_desc = True
                    if v_details.get("keywords") and isinstance(v_details["keywords"], list):
                        kw_str = ", ".join(v_details["keywords"][:18])
                        if kw_str:
                            info_parts.append(f"Reference Video Keywords / Topics: {kw_str}")
                    tracks = (((ib_data.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}).get("captionTracks")) or []
                    for tr in tracks:
                        if tr.get("baseUrl"):
                            caption_urls.append(tr["baseUrl"])
                    if got_desc and caption_urls:
                        break
            except Exception:
                pass

    # 3. Fallback / Supplement from Watch Page HTML
    if vid_id and (not got_desc or not caption_urls):
        try:
            req = urllib.request.Request(canonical_url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=6) as resp:
                html_text = resp.read().decode("utf-8", errors="ignore")
                if not got_title:
                    tm = re.search(r"<title>(.*?)</title>", html_text, flags=re.I | re.S)
                    if tm:
                        t_clean = html_mod.unescape(tm.group(1)).replace("- YouTube", "").strip()
                        if t_clean:
                            info_parts.append(f"Reference Video Title: \"{t_clean}\"")
                            got_title = True
                if not got_desc:
                    desc_m = re.search(r'"shortDescription":"((?:\\.|[^"\\])*)"', html_text)
                    if desc_m:
                        raw_desc = json.loads(f'"{desc_m.group(1)}"')
                        clean_desc = re.sub(r"\s+", " ", raw_desc).strip()[:1100]
                        if clean_desc:
                            info_parts.append(f"Reference Video Description & Breakdown: {clean_desc}")
                            got_desc = True
                if not caption_urls:
                    cap_m = re.search(r'"captionTracks":\[\{"baseUrl":"((?:\\.|[^"\\])*)"', html_text)
                    if cap_m:
                        caption_urls.append(json.loads(f'"{cap_m.group(1)}"'))
        except Exception:
            pass

    # 4. Fetch Caption / Transcript text from discovered caption URL(s)
    for cap_url in caption_urls[:2]:
        if got_captions:
            break
        try:
            c_req = urllib.request.Request(cap_url, headers=headers, method="GET")
            with urllib.request.urlopen(c_req, timeout=5) as c_resp:
                cap_xml = c_resp.read().decode("utf-8", errors="ignore")
                cap_lines = re.findall(r"<text[^>]*>(.*?)</text>", cap_xml, flags=re.S)
                if not cap_lines:
                    cap_lines = re.findall(r"<p[^>]*>(.*?)</p>", cap_xml, flags=re.S)
                if cap_lines:
                    joined_cap = " ".join(html_mod.unescape(re.sub(r"<[^>]+>", " ", c)) for c in cap_lines[:130])
                    joined_cap = re.sub(r"\s+", " ", joined_cap).strip()[:2200]
                    if len(joined_cap) > 40:
                        info_parts.append(
                            f"Reference Video Actual Spoken Transcript Sample (match this hook style, storytelling structure, pacing, and tone): \"{joined_cap}\""
                        )
                        got_captions = True
        except Exception:
            pass

    res_str = "\n".join(info_parts)
    _YT_REF_CACHE[yt_url] = res_str
    return res_str


def clean_narrator_script_text(raw_text: str, max_words: int = 1080) -> str:
    txt = raw_text.strip()
    cleaned_lines = []
    for line in txt.splitlines():
        ln = line.strip()
        if not ln:
            cleaned_lines.append("")
            continue
        if re.match(r"^(#+|\*\*?(?:part|act|scene|section|intro|outro|fact|chapter|भाग|अध्याय|दृश्य)\b|(?:part|act|scene|chapter|भाग|दृश्य)\s*\d+[:.\-–—])", ln, flags=re.I):
            continue
        ln = re.sub(r"\[[^\]]*\]", "", ln)
        ln = re.sub(r"^(?:narrator|speaker|voiceover|host|कथावाचक|वॉयसओवर)\s*:\s*", "", ln, flags=re.I)
        ln = ln.replace("**", "").replace("*", "")
        ln = re.sub(r"\s{2,}", " ", ln).strip()
        if ln:
            cleaned_lines.append(ln)

    joined = "\n\n".join([p.strip() for p in "\n".join(cleaned_lines).split("\n\n") if p.strip()])

    if max_words > 0 and len(joined.split()) > max_words:
        joined = _fit_script_to_word_window(joined, min_words=max(60, max_words - 12), max_words=max_words)

    return joined.strip()


def _strip_outro_sentences(text: str) -> str:
    if not text:
        return ""
    out_paras = []
    outro_pat = re.compile(
        r"(?:subscribe to |thank you for watching|thanks for watching|see you in the next|"
        r"चैनल को सब्सक्राइब|वीडियो देखने के लिए धन्यवाद|मिलते हैं अगली वीडियो में)",
        flags=re.I,
    )
    for para in text.split("\n\n"):
        p = para.strip()
        if not p:
            continue
        sents = [s.strip() for s in re.findall(r'[^.!?।|]+[.!?।|]+["\']?|\S[^.!?।|]*$', p) if s.strip()]
        kept = [s for s in sents if not outro_pat.search(s)]
        if kept:
            out_paras.append(" ".join(kept))
    return "\n\n".join(out_paras).strip()


def _fit_script_to_word_window(text: str, min_words: int, max_words: int, is_hindi: bool = False) -> str:
    if not text:
        return ""
    total_w = len(text.split())
    if total_w <= max_words:
        return text.strip()

    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    out_paras = []
    accum_words = 0
    stop_flag = False
    end_punct = "।" if is_hindi else "."

    for p in paras:
        if stop_flag:
            break
        sents = [s.strip() for s in re.findall(r'[^.!?।|]+[.!?।|]+["\']?|\S[^.!?।|]*$', p) if s.strip()]
        cur_p_sents = []
        for s in sents:
            sw = s.split()
            sw_len = len(sw)
            if accum_words + sw_len <= max_words:
                cur_p_sents.append(s)
                accum_words += sw_len
            else:
                if accum_words >= min_words:
                    stop_flag = True
                    break
                # We need more words from this sentence to reach min_words without exceeding max_words
                needed_max = max(1, max_words - accum_words)
                needed_min = max(1, min_words - accum_words)
                sub_words = sw[:needed_max]
                sub_str = " ".join(sub_words).rstrip(",;:-–— ")
                # Try to cut at a natural clause boundary between needed_min and needed_max if possible
                clause_matches = list(re.finditer(r"[,;—–]\s+", sub_str))
                if clause_matches:
                    last_clause = clause_matches[-1]
                    candidate = sub_str[:last_clause.start()].rstrip(",;:-–— ")
                    if len(candidate.split()) >= needed_min:
                        sub_str = candidate
                if not re.search(r'[.!?।]["\']?$', sub_str):
                    sub_str += end_punct
                cur_p_sents.append(sub_str)
                accum_words += len(sub_str.split())
                stop_flag = True
                break
        if cur_p_sents:
            out_paras.append(" ".join(cur_p_sents))

    return "\n\n".join(out_paras).strip()


SCRIPT_TYPE_INSTRUCTIONS = {
    "living_type": (
        "SCRIPT FORMAT — LIVING / LIFE EXPERIENCE POV TYPE (@MoneyLifePOV & @QuietCapitalUSA 'How You Live' Style): "
        "Put the viewer directly inside the story ('You wake up at 6:00 AM...', 'You drive your paid-off sedan...'). "
        "Focus deeply on realistic day-to-day living, daily routines from morning to night, housing/rent/food/work habits, "
        "quiet lifestyle choices, and the sharp contrast between how loud spenders appear to live on the surface "
        "versus how you quietly build real freedom behind closed doors. Use vivid 3–4 second visual scenes."
    ),
    "horror_story": (
        "SCRIPT FORMAT — CHILLING IMMERSIVE POV HORROR, SCARY & PARANORMAL STORY TYPE: "
        "Write as an intensely atmospheric, spine-chilling late-night horror story where the viewer feels they are right inside the room! "
        "Start with an eerie late-night setting (e.g., 2:47 AM, a quiet house, a lonely road, a strange knock, or an unsettling detail). "
        "Build creeping dread step by step with realistic sensory details every 3–4 seconds (footsteps, shadows, cold air, flickering lights), "
        "escalating into a terrifying climax and an unforgettable final twist."
    ),
    "documentary_type": (
        "SCRIPT FORMAT — CINEMATIC DOCUMENTARY & INVESTIGATION TYPE: "
        "Write in an authoritative, gripping documentary narrator voice with vivid 3–4 second visual scenes. "
        "Explore the deeper history, hidden systems, real-world mechanisms, timeline of events, key turning points, "
        "and investigative insights behind the topic with cinematic atmosphere."
    ),
    "facts_type": (
        "SCRIPT FORMAT — HIGH-RETENTION FACTS & HIDDEN TRUTHS TYPE: "
        "Structure the narration as a smooth, gripping series of mind-blowing facts, psychological secrets, and hidden truths "
        "(using natural spoken transitions like 'Fact number one...', 'That brings us to the second hidden truth...', "
        "'Number three...', etc. — NEVER use bullet symbols). Every fact must hook curiosity, explain the real-world mechanism "
        "with a vivid 3–4 second visual example, and keep the viewer glued until the final fact."
    ),
    "wealth_story": (
        "SCRIPT FORMAT — IMMERSIVE POV STORY & FINANCIAL REALISM TYPE: "
        "Write in the viral second-person POV storytelling style! "
        "Make the viewer feel like THEY are living the exact story described in the topic title. "
        "Every scene, setting, interaction, and conflict MUST be custom-built around the specific topic (no generic copy-paste tropes). "
        "Show exact dollar numbers, realistic daily routines, the quiet discipline behind real success, social contrasts with outsiders, "
        "and an undeniable turning point, with a concrete visual action in every 3–4 second sentence."
    ),
    "mystery_crime": (
        "SCRIPT FORMAT — TRUE CRIME, MYSTERY & UNSOLVED CASE TYPE: "
        "Write as a tense, atmospheric chronological mystery & investigation where clues, witness accounts, timeline events, "
        "and unanswered questions unfold step by step in vivid 3–4 second visual scenes, keeping suspense high to the final revelation."
    ),
    "psychology_mindset": (
        "SCRIPT FORMAT — DARK PSYCHOLOGY, HUMAN BEHAVIOR & MINDSET POV TYPE (@MoneyLifePOV / @QuietCapitalUSA Psychological Contrast): "
        "Focus on deep human psychology, why people chase status and approval, subtle social dynamics, emotional triggers, "
        "and the quiet power of walking away from the crowd, illustrated through hyper-relatable second-person ('You') real-life scenes."
    ),
    "history_mythology": (
        "SCRIPT FORMAT — HISTORY, ANCIENT SECRETS & MYTHOLOGY TYPE: "
        "Take the listener back in time with immersive historical or mythological storytelling — vivid ancient settings, empires, "
        "forgotten secrets, pivotal battles, and timeless mysteries with concrete visual scenes every 3–4 seconds."
    ),
    "biography_empire": (
        "SCRIPT FORMAT — BIOGRAPHY, RISE & FALL / EMPIRE STORY TYPE: "
        "Tell the chronological rise, struggle, pivotal decisions, peak, crises, and lasting lessons of the central subject, "
        "person, founder, or movement with vivid scenes and high stakes."
    ),
    "survival_disaster": (
        "SCRIPT FORMAT — EXTREME SURVIVAL & REAL-LIFE DISASTER STORY TYPE: "
        "Write a gripping, minute-by-minute survival narrative where the protagonist faces extreme odds, critical life-or-death decisions, "
        "and remarkable resilience."
    ),
    "motivation_stoic": (
        "SCRIPT FORMAT — STOIC PHILOSOPHY, SELF-DISCIPLINE & QUIET STRENGTH POV TYPE: "
        "Deliver a calm, deeply moving, immersive second-person ('You') narrative about self-mastery, mental toughness, "
        "outgrowing the need to impress anyone, and building an unshakable life."
    ),
    "explainer_guide": (
        "SCRIPT FORMAT — ENGAGING STEP-BY-STEP EXPLAINER & PRACTICAL GUIDE TYPE: "
        "Break down the topic clearly from the ground up using relatable real-life POV scenarios, exact numbers, common mistakes to avoid, "
        "and a clear step-by-step actionable blueprint."
    ),
    "comedy_funny": (
        "SCRIPT FORMAT — COMEDY, FUNNY & HUMOROUS POV STORY TYPE: "
        "Write an entertaining, hilarious, witty, and lighthearted comedy story or humorous POV commentary! Use sharp comedic timing, "
        "relatable everyday chaos, funny misunderstandings, ironic twists, playful sarcasm, and laugh-out-loud punchlines while keeping "
        "every 3–4 second beat visually expressive."
    ),
}

NICHE_SCRIPT_CONTEXT = {
    "white_char": "Visual Niche: 16:9 Widescreen 2D White-Skin Cartoon Main Character in a detailed, natural-colored 2D illustrated world (matching @MoneyLifePOV & @QuietCapitalUSA visual storytelling).",
    "stickman": "Visual Niche: 16:9 Widescreen 2D Stickman / Sticky Character video with expressive stick-figure actions and relatable everyday scenes.",
}


def compute_script_parts_plan(duration_mins: float) -> tuple:
    # Strictly calibrated to our mastered voiceover rate: 140 spoken words = 1.00 minute of audio (e.g. 3 mins = 420 words, 10 mins = 1,400 words, 15 mins = 2,100 words, 20 mins = 2,800 words)
    dur = max(1.0, min(45.0, float(duration_mins or 20.0)))
    target_total_words = max(140, int(round(dur * 140.0)))
    if target_total_words <= 560:
        total_parts = 1
    elif target_total_words <= 1260:
        total_parts = 2
    elif target_total_words <= 1820:
        total_parts = 3
    elif target_total_words <= 2520:
        total_parts = 4
    elif target_total_words <= 3220:
        total_parts = 5
    else:
        total_parts = 6
    return dur, target_total_words, total_parts


def generate_script_part(topic: str, part_num: int, mode: str, char_name: str = "", prev_tail: str = "", niche_config: dict = None) -> dict:
    niche_config = niche_config or {}
    raw_niche = (niche_config.get("niche_type") or "white_char").strip().lower()
    niche_type = "stickman" if raw_niche == "stickman" else "white_char"
    char_name_mode = (niche_config.get("char_name_mode") or "none").strip().lower()
    configured_name = (niche_config.get("fixed_char_name") or niche_config.get("char_name") or "").strip()
    script_type = (niche_config.get("script_type") or "wealth_story").strip().lower()
    language = (niche_config.get("language") or "english").strip().lower()
    is_hindi = (language == "hindi")

    raw_dur_mins = niche_config.get("script_duration_mins", 20)
    try:
        dur_mins = float(raw_dur_mins)
    except Exception:
        dur_mins = 20.0

    dur_mins, target_total_words, computed_total_parts = compute_script_parts_plan(dur_mins)
    total_parts = max(1, min(6, int(niche_config.get("total_parts") or computed_total_parts)))
    part_num = max(1, min(total_parts, int(part_num or 1)))
    is_final_part = (part_num == total_parts)

    words_written_so_far = 0
    try:
        words_written_so_far = max(0, int(niche_config.get("words_written_so_far") or 0))
    except Exception:
        words_written_so_far = 0

    if is_final_part and total_parts > 1 and words_written_so_far > 0:
        part_target_words = max(120, target_total_words - words_written_so_far)
    else:
        part_target_words = max(120, int(round(target_total_words / float(total_parts))))

    if is_hindi:
        closing_line = (
            "अगर आपको यह वीडियो पसंद आई हो, तो हमारे चैनल को सब्सक्राइब ज़रूर करें और इसे अपने दोस्तों के साथ शेयर करें। "
            "वीडियो देखने के लिए धन्यवाद, मिलते हैं अगली वीडियो में।"
        )
    else:
        closing_line = (
            "If this video helped you, subscribe to our channel and share it with a friend who needs to hear it. "
            "Thank you for watching, and I'll see you in the next one."
        )
    closing_words_count = len(closing_line.split())
    body_target_words = max(90, part_target_words - (closing_words_count if is_final_part else 0))
    body_min_words = body_target_words
    body_max_words = body_target_words + 8

    yt_ref_url = (niche_config.get("youtube_ref_url") or "").strip()
    yt_ref_notes = (niche_config.get("youtube_ref_notes") or "").strip()

    # Strictly respect the user's selected Character Name Mode ('fixed', 'random', or 'none')
    incoming_char = (char_name or "").strip()
    if incoming_char in ("NONE", "No Name (Narrator)"):
        incoming_char = ""

    if char_name_mode == "none":
        protagonist = ""
    elif char_name_mode == "fixed":
        protagonist = configured_name or incoming_char or ("आरव" if is_hindi else "Daniel")
    else:
        # char_name_mode == "random"
        if incoming_char:
            protagonist = incoming_char
        else:
            names_pool = HINDI_PROTAGONIST_NAMES if is_hindi else FREE_PROTAGONIST_NAMES
            idx = (sum(ord(c) for c in (topic or "video")) + int(time.time() // 60)) % len(names_pool)
            protagonist = names_pool[idx]

    if protagonist:
        char_rule_line = (
            f"STRICT MAIN CHARACTER & POV IMMERSION RULE: The main character's name is '{protagonist}'. "
            f"Feature '{protagonist}' consistently throughout the script AND write with intimate @MoneyLifePOV / @QuietCapitalUSA POV immersion "
            f"(so the viewer watching feels like they ARE '{protagonist}', experiencing every room, conversation, and quiet financial win firsthand)."
        )
        char_ref_label = f"{protagonist} (and the viewer in {protagonist}'s POV)"
    else:
        char_rule_line = (
            "STRICT SECOND-PERSON IMMERSIVE POV ('YOU' / 'आप') RULE (@MoneyLifePOV & @QuietCapitalUSA Style): "
            "Do NOT assign any third-person name (like Daniel, Ryan, or Aarav). "
            "Write the ENTIRE script directly in Second-Person POV ('You' / 'Your' in English, 'आप' / 'आपका' in Hindi) so the person watching the video feels 100% like THEY are the protagonist living the story right now!"
        )
        char_ref_label = "YOU (the viewer as the main character in second-person POV)"

    niche_context_line = NICHE_SCRIPT_CONTEXT.get(niche_type, NICHE_SCRIPT_CONTEXT["white_char"])
    script_type_block = SCRIPT_TYPE_INSTRUCTIONS.get(script_type, SCRIPT_TYPE_INSTRUCTIONS["wealth_story"])

    # Always enrich script generation with live YouTube Data API v3 viral research (both on the topic AND any reference channel/video!)
    yt_api_live_research = fetch_youtube_api_viral_research(query_keywords=topic, channel_or_video_ref=yt_ref_url)
    yt_ref_block = "\n=== LIVE YOUTUBE DATA API VIRAL RESEARCH & REFERENCE CHANNEL BLUEPRINT ===\n"
    if yt_api_live_research:
        yt_ref_block += f"{yt_api_live_research}\n"
    if yt_ref_url:
        fetched_yt = fetch_youtube_reference_context(yt_ref_url)
        if fetched_yt and fetched_yt not in yt_ref_block:
            yt_ref_block += f"{fetched_yt}\n"
    if yt_ref_notes:
        yt_ref_block += f"USER'S MANDATORY CUSTOMIZATION / CHANGE NOTES FOR THIS REFERENCE VIDEO: {yt_ref_notes}\n"
    if yt_ref_url:
        yt_ref_block += (
            f"CRITICAL TOPIC vs REFERENCE CHANNEL STYLE RULE:\n"
            f"1. CORE TOPIC & SUBJECT MATTER (100% FIXED TO USER'S TOPIC): The entire script MUST be 100% about the user's selected Topic & Niche (\"{topic}\").\n"
            f"2. REFERENCE CHANNEL STYLE ONLY: From the Reference YouTube Channel / Video research above, extract ONLY the storytelling style, cold-open hook technique, pacing, second-person/protagonist POV immersion, sentence rhythm, and audience retention structure — and apply that exact viral script style to write a masterpiece script on \"{topic}\" (even if the Reference Channel usually covers a different sub-niche)!\n"
        )
    else:
        yt_ref_block += (
            f"CRITICAL NO-REFERENCE MASTER VIRAL SCRIPT RULE (MAXIMUM ENGAGEMENT, TWISTS & RETENTION):\n"
            f"1. Since no specific Reference Channel was given, craft an original, ultra-addictive, binge-worthy YouTube script 100% focused on \"{topic}\".\n"
            f"2. COLD-OPEN HOOK + OPEN LOOPS: Grab the viewer by the collar in the very first sentence with a shocking contrast, high-stakes moment, or curiosity gap on \"{topic}\".\n"
            f"3. UNPREDICTABLE TWISTS & TURNING POINTS: Weave compelling plot twists, surprising reveals, counter-intuitive insights, and dramatic turning points throughout the story so viewers stay glued to the screen watching until the very last second!\n"
        )

    lang_rule_block = (
        "LANGUAGE REQUIREMENT — HINDI (हिन्दी - DEVANAGARI SCRIPT):\n"
        "- Write the ENTIRE script in natural, engaging, conversational spoken Hindi written in Devanagari script (हिन्दी).\n"
        "- Use simple, powerful everyday spoken Hindi words (bolchal ki bhasha) that sound 100% natural, emotional, and gripping when spoken by a YouTube narrator (avoid overly difficult Sanskritized textbook words).\n"
        "- Even if the Topic or Reference Video is in English or Roman Hindi, write the full narration in natural spoken Hindi (Devanagari)."
        if is_hindi
        else "LANGUAGE REQUIREMENT — ENGLISH:\n- Write the ENTIRE script in natural, gripping, conversational spoken English."
    )

    ask_words_min = max(120, body_target_words + 25)
    ask_words_max = body_target_words + 85

    base_rules = f"""You are the head scriptwriter behind the viral YouTube channels @MoneyLifePOV ("Money Life POV") and @QuietCapitalUSA ("Quiet Capital").
Topic: "{topic}"
Target Total Video Voiceover Duration: {dur_mins:g} Minutes (~{target_total_words} total words across {total_parts} part(s), strictly calibrated at 140 words per minute).
{lang_rule_block}
{char_rule_line}
{niche_context_line}
{script_type_block}
{MONEYLIFEPOV_AND_QUIETCAPITAL_MASTER_BLUEPRINT}
{yt_ref_block}
CRITICAL ORIGINALITY & TOPIC FIDELITY MANDATE:
- The script MUST be 100% custom-written from scratch for the exact topic: "{topic}".
- Read every single word of "{topic}" and build every scene, character interaction, and dilemma directly around this exact premise.
- DO NOT use generic stock templates. All characters, locations, dialogue, props, and situations must be completely fresh, unique, and true to "{topic}"!
STRICT NARRATOR-ONLY & 3–4 SECOND VISUAL BEAT RULES:
- Output ONLY the exact spoken words the narrator says out loud.
- NEVER include titles, headings, "Part 1", "Act 1", stage directions, [music], [pause], bullet points, or asterisks (**).
- Write in crisp, punchy, visual sentences (around 7 to 10 words per clause) separated into short paragraphs so that EVERY 3 TO 4 SECONDS OF SPEECH describes a concrete physical action, object, setting, dollar amount, or facial reaction that maps 1-to-1 to an illustration on screen!
- Make the storytelling so relatable and immersive that the viewer watching feels: "This video is literally talking about ME and my life."
- NEVER mention any specific YouTube channel name anywhere in the spoken script.
- STRICT WORD COUNT REQUIREMENT FOR THIS PART: Write between {ask_words_min} and {ask_words_max} spoken words before the outro. Do NOT stop early and do NOT write excessively long!"""

    if total_parts == 1:
        part_prompt = f"""{base_rules}

Write the COMPLETE {dur_mins:g}-Minute Script ({ask_words_min} to {ask_words_max} words):
1. Start with an immediate, cold-open @MoneyLifePOV / @QuietCapitalUSA micro-scene hook on "{topic}" that puts {char_ref_label} right inside a relatable real-life moment with concrete numbers and objects.
2. Develop the full narrative around "{topic}" with vivid 3–4 second visual scenes, social contrast, emotional realism, and specific details involving {char_ref_label}.
3. Deliver the key climax, twist, realization, and quiet-wealth / life takeaways clearly in spoken prose.
4. End with a thoughtful question for the comments."""
    elif part_num == 1:
        part_prompt = f"""{base_rules}

Write PART 1 OF {total_parts} ({ask_words_min} to {ask_words_max} words):
1. Start with an unforgettable @MoneyLifePOV / @QuietCapitalUSA cold-open scene hook that drops {char_ref_label} directly into a vivid real-life moment on "{topic}".
2. Establish the everyday contrast—what people around {char_ref_label} assume on the surface versus the quiet reality underneath—using concrete numbers, props, and 3–4 second visual actions.
3. Build momentum with specific scenes and rising tension. End Part 1 smoothly on a compelling hook moment without wrapping up the whole video yet."""
    elif not is_final_part:
        part_prompt = f"""{base_rules}

Here is how Part {part_num - 1} ended (continue seamlessly from this exact point without repeating it):
\"\"\"{prev_tail[-900:]}\"\"\"

Write PART {part_num} OF {total_parts} ({ask_words_min} to {ask_words_max} words):
1. Continue seamlessly from the end of Part {part_num - 1}.
2. Deepen the @MoneyLifePOV / @QuietCapitalUSA narrative on "{topic}" with fresh everyday scenes, deeper financial/psychological contrasts, exact numbers, and 3–4 second visual actions for {char_ref_label}.
3. Keep the pacing gripping and do NOT write the final video outro yet."""
    else:
        part_prompt = f"""{base_rules}

Here is how Part {part_num - 1} ended (continue seamlessly from this exact point without repeating it):
\"\"\"{prev_tail[-900:]}\"\"\"

Write THE FINAL PART (PART {part_num} OF {total_parts}, {ask_words_min} to {ask_words_max} words):
1. Continue seamlessly from the end of Part {part_num - 1}.
2. Bring the @MoneyLifePOV / @QuietCapitalUSA story on "{topic}" to its deeply satisfying payoff and conclusion with vivid 3–4 second scenes.
3. Share the final realization and practical mindset takeaways in natural spoken prose (no bullet symbols).
4. End with a thoughtful question for the comments."""

    max_tok = max(1800, min(4096, int(ask_words_max * 3.2)))
    raw_out = call_gemini_api(part_prompt, max_tokens=max_tok)
    cleaned = clean_narrator_script_text(raw_out, max_words=0)
    cleaned = _strip_outro_sentences(cleaned)

    # Multi-pass top-up loop: guarantee that every part reaches AT LEAST 100% of body_min_words!
    for _topup_idx in range(4):
        cur_words = cleaned.split()
        if len(cur_words) >= body_min_words:
            break
        shortfall = max(90, body_min_words - len(cur_words) + 45)
        topup_prompt = f"""{base_rules}

Here is the script text written so far for Part {part_num}:
\"\"\"{cleaned[-1100:]}\"\"\"

Continue seamlessly from the exact last sentence above and write AT LEAST {shortfall} more words of rich, gripping @MoneyLifePOV / @QuietCapitalUSA spoken narration on "{topic}" with new 3–4 second visual scenes, specific numbers, and deeper storytelling so this part reaches its required {body_min_words}-word count. Do NOT repeat previous sentences, do NOT write any subscribe/outro line, and do NOT include headings."""
        try:
            extra_raw = call_gemini_api(topup_prompt, max_tokens=max(1200, int(shortfall * 3.2)))
            extra_clean = clean_narrator_script_text(extra_raw, max_words=0)
            extra_clean = _strip_outro_sentences(extra_clean)
            if extra_clean and len(extra_clean.split()) >= 12:
                cleaned = (cleaned.rstrip() + "\n\n" + extra_clean.lstrip()).strip()
            else:
                break
        except Exception:
            break

    # Strictly fit the part body into [body_min_words, body_max_words] at a clean sentence boundary!
    cleaned = _fit_script_to_word_window(cleaned, min_words=body_min_words, max_words=body_max_words, is_hindi=is_hindi)

    # Guarantee exact ending rule on the final part: append closing_line once so total words = part_target_words (+0 to +8 words)
    if is_final_part:
        cleaned = (cleaned.rstrip() + "\n\n" + closing_line).strip()

    return {
        "ok": True,
        "part_num": part_num,
        "total_parts": total_parts,
        "target_duration_mins": dur_mins,
        "target_total_words": target_total_words,
        "language": language,
        "protagonist": protagonist if protagonist else "No Name (Narrator)",
        "part_text": cleaned,
        "words_count": len(cleaned.split()),
    }


def _dec_bytes(arr):
    return "".join(chr(b ^ 42) for b in arr)

_SB_P_BYTES = [89, 72, 117, 90, 95, 72, 70, 67, 89, 66, 75, 72, 70, 79, 117, 75, 103, 98, 120, 70, 68, 25, 67, 30, 88, 110, 82, 123, 92, 122, 121, 78, 110, 31, 82, 71, 77, 117, 123, 95, 31, 70, 123, 125, 80, 98]
_SB_S_BYTES = [89, 72, 117, 89, 79, 73, 88, 79, 94, 117, 104, 89, 126, 125, 107, 82, 101, 91, 77, 79, 100, 73, 18, 76, 73, 30, 19, 28, 79, 102, 100, 77, 117, 31, 125, 100, 89, 18, 96, 97, 83]

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://gvwxjszyrmvlcnxvixzf.supabase.co").rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY") or _dec_bytes(_SB_P_BYTES)
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY") or _dec_bytes(_SB_S_BYTES)
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
ADMIN_EMAIL = "abdulrafay40023@gmail.com"
# CO_ADMIN_EMAIL removed
ALL_ADMIN_EMAILS = {ADMIN_EMAIL}
SUPABASE_BUCKET = "studio-db"
SUPABASE_REGISTRY_OBJECT = "registry_v6.json"
LOCAL_STORAGE_DIR = "c:/Users/Rafay/Downloads/youtube finance videos/editing-tool-main/local_data" if os.name == "nt" else "/tmp"
os.makedirs(LOCAL_STORAGE_DIR, exist_ok=True)
REGISTRY_FILE_V6 = os.path.join(LOCAL_STORAGE_DIR, "video_studio_users_registry_v6.json")


def _supabase_headers(content_type: str = "application/json", extra: dict = None) -> dict:
    h = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "User-Agent": "VideoStudio/6.0",
    }
    if content_type:
        h["Content-Type"] = content_type
    if extra:
        h.update(extra)
    return h


def _ensure_supabase_bucket():
    try:
        payload = json.dumps({"id": SUPABASE_BUCKET, "name": SUPABASE_BUCKET, "public": False}).encode("utf-8")
        req = urllib.request.Request(
            f"{SUPABASE_URL}/storage/v1/bucket",
            data=payload,
            headers=_supabase_headers(),
            method="POST",
        )
        urllib.request.urlopen(req, timeout=6).close()
    except Exception:
        pass


def _merge_user_entry(target_users: dict, nk: str, incoming: dict, deleted_list: list):
    if not nk or nk in deleted_list:
        return
    default_status = "approved"
    if nk not in target_users:
        target_users[nk] = dict(incoming)
        target_users[nk].setdefault("scripts_count", 0)
        target_users[nk].setdefault("videos_count", 0)
        target_users[nk].setdefault("voices_count", 0)
        target_users[nk].setdefault("status", default_status)
        target_users[nk].setdefault("status_updated_at", float(incoming.get("status_updated_at") or 0.0))
        if nk in ALL_ADMIN_EMAILS:
            target_users[nk]["status"] = "approved"
    else:
        cur = target_users[nk]
        cur["scripts_count"] = max(int(cur.get("scripts_count", 0)), int(incoming.get("scripts_count", 0)))
        cur["videos_count"] = max(int(cur.get("videos_count", 0)), int(incoming.get("videos_count", 0)))
        cur["voices_count"] = max(int(cur.get("voices_count", 0)), int(incoming.get("voices_count", 0)))
        if incoming.get("name"):
            cur["name"] = incoming["name"]
        if incoming.get("email"):
            cur["email"] = incoming["email"]
        if incoming.get("avatar_url"):
            cur["avatar_url"] = incoming["avatar_url"]
        if incoming.get("supabase_uid"):
            cur["supabase_uid"] = incoming["supabase_uid"]
        if incoming.get("joined_at") and not cur.get("joined_at"):
            cur["joined_at"] = incoming["joined_at"]
        if incoming.get("niche_label") and not str(incoming.get("niche_label")).startswith("⚠️"):
            cur["niche_label"] = incoming["niche_label"]

        cur_ts = float(cur.get("status_updated_at") or 0.0)
        inc_ts = float(incoming.get("status_updated_at") or 0.0)
        if inc_ts >= cur_ts and incoming.get("status"):
            cur["status"] = incoming["status"]
            cur["status_updated_at"] = inc_ts
            if incoming.get("last_active"):
                cur["last_active"] = incoming["last_active"]
        elif incoming.get("last_active") and not cur.get("last_active"):
            cur["last_active"] = incoming["last_active"]

        cur.setdefault("status", default_status)
        if nk in ALL_ADMIN_EMAILS:
            cur["status"] = "approved"


PRE_APPROVED_TEAM_USERS = {
    "abdulrafay40023@gmail.com": {"name": "Abdul Rafay", "norm_name": "abdulrafay40023@gmail.com", "email": "abdulrafay40023@gmail.com", "status": "approved", "status_updated_at": 1793000000.0},
    "shahbazzaidi250@gmail.com": {"name": "Shahbaz Zaidi", "norm_name": "shahbazzaidi250@gmail.com", "email": "shahbazzaidi250@gmail.com", "status": "approved", "status_updated_at": 1793000000.0},
}
SUPABASE_EGRESS_RESTRICTED = True


def load_registry() -> dict:
    merged = {
        "users": {},
        "deleted": [],
        "schema_v6_supabase": True,
        "updated_at": time.time(),
    }
    for pk, pu in PRE_APPROVED_TEAM_USERS.items():
        merged["users"][pk] = dict(pu)

    # 1. Read local /tmp v6 cache first (0ms instant read on warm serverless instances)
    if os.path.exists(REGISTRY_FILE_V6):
        try:
            with open(REGISTRY_FILE_V6, "r", encoding="utf-8") as f:
                local_data = json.load(f)
                if isinstance(local_data, dict):
                    merged["updated_at"] = max(merged["updated_at"], float(local_data.get("updated_at") or 0.0))
                    for d_nk in local_data.get("deleted", []):
                        if d_nk not in merged["deleted"]:
                            merged["deleted"].append(d_nk)
                    for nk, u in (local_data.get("users") or {}).items():
                        _merge_user_entry(merged["users"], nk, u, merged["deleted"])
        except Exception:
            pass

    if SUPABASE_EGRESS_RESTRICTED:
        return merged
    # 2. Read from Supabase Storage with strict cache-busting so stale CDN cache NEVER delays Approve/Reject/Remove
    try:
        cb = int(time.time() * 1000)
        req = urllib.request.Request(
            f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{SUPABASE_REGISTRY_OBJECT}?cb={cb}",
            headers=_supabase_headers(
                content_type="",
                extra={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                },
            ),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            sb_data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            if isinstance(sb_data, dict):
                merged["updated_at"] = max(merged["updated_at"], float(sb_data.get("updated_at") or 0.0))
                for d_nk in sb_data.get("deleted", []):
                    if d_nk not in merged["deleted"]:
                        merged["deleted"].append(d_nk)
                for nk, u in (sb_data.get("users") or {}).items():
                    _merge_user_entry(merged["users"], nk, u, merged["deleted"])
    except Exception:
        pass

    return merged


def save_registry(data: dict):
    data["updated_at"] = time.time()
    try:
        with open(REGISTRY_FILE_V6, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass
    if SUPABASE_EGRESS_RESTRICTED:
        return
    headers = _supabase_headers(
        extra={
            "x-upsert": "true",
            "cache-control": "no-cache, no-store, max-age=0",
        }
    )
    try:
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{SUPABASE_REGISTRY_OBJECT}",
            data=payload,
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5).close()
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            _ensure_supabase_bucket()
            try:
                payload = json.dumps(data).encode("utf-8")
                req2 = urllib.request.Request(
                    f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{SUPABASE_REGISTRY_OBJECT}",
                    data=payload,
                    headers=headers,
                    method="POST",
                )
                urllib.request.urlopen(req2, timeout=5).close()
            except Exception:
                pass
    except Exception:
        pass


def _build_admin_visible_users_payload(reg: dict, requester_email: str = "") -> tuple:
    req_em = (requester_email or "").strip().lower()
    is_co_admin_only = (req_em == CO_ADMIN_EMAIL)
    all_users = list((reg.get("users") or {}).values())
    visible_users = [
        dict(u) for u in all_users
        if u.get("status", "pending") in ("pending", "approved")
    ]
    visible_users.sort(key=lambda x: (0 if x.get("status", "pending") == "pending" else 1, x.get("name", "")))
    pending_count = sum(1 for u in visible_users if (u.get("email") or "").lower() not in ALL_ADMIN_EMAILS and u.get("status", "pending") == "pending")

    if is_co_admin_only:
        # For jennifer.cisternino2027@gmail.com: ONLY return who the users are & their status (hide video/script/voice counts & dashboard info)
        sanitized = []
        for u in visible_users:
            sanitized.append({
                "name": u.get("name", "Google User"),
                "norm_name": u.get("norm_name", ""),
                "email": u.get("email", ""),
                "avatar_url": u.get("avatar_url", ""),
                "status": u.get("status", "pending"),
                "joined_at": u.get("joined_at", ""),
                "last_active": u.get("last_active", ""),
                "hidden_stats": True,
            })
        return sanitized, pending_count

    return visible_users, pending_count


def _find_registry_user_key(users: dict, target: str) -> str:
    t = (target or "").strip().lower()
    if not t:
        return ""
    if t in users:
        return t
    for nk, u in users.items():
        if (u.get("email") or "").strip().lower() == t:
            return nk
        if (u.get("norm_name") or "").strip().lower() == t:
            return nk
        if (u.get("name") or "").strip().lower() == t:
            return nk
    return ""


SUPABASE_MEDIA_BUCKET = "studio-media"


def _ensure_supabase_media_bucket():
    try:
        payload = json.dumps({"id": SUPABASE_MEDIA_BUCKET, "name": SUPABASE_MEDIA_BUCKET, "public": True}).encode("utf-8")
        req = urllib.request.Request(
            f"{SUPABASE_URL}/storage/v1/bucket",
            data=payload,
            headers=_supabase_headers(),
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5).close()
    except Exception:
        try:
            payload = json.dumps({"public": True}).encode("utf-8")
            req2 = urllib.request.Request(
                f"{SUPABASE_URL}/storage/v1/bucket/{SUPABASE_MEDIA_BUCKET}",
                data=payload,
                headers=_supabase_headers(),
                method="PUT",
            )
            urllib.request.urlopen(req2, timeout=5).close()
        except Exception:
            pass


def _safe_user_storage_slug(user_key: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "_", (user_key or "user").strip().lower()).strip("_")
    return s or "user"


def create_signed_media_upload(user_key: str, project_id: str, media_type: str, ext: str) -> dict:
    _ensure_supabase_media_bucket()
    slug = _safe_user_storage_slug(user_key)
    safe_proj = re.sub(r"[^a-zA-Z0-9_.-]+", "_", (project_id or "proj").strip())[:60] or "proj"
    safe_type = "video" if str(media_type).lower() == "video" else "voice"
    safe_ext = re.sub(r"[^a-z0-9]+", "", (ext or "mp4").lower())[:8] or "mp4"
    object_path = f"{slug}/{safe_proj}_{safe_type}.{safe_ext}"

    # Create signed upload URL on Supabase Storage so browser can PUT large videos (10MB-200MB) directly
    sign_endpoint = f"{SUPABASE_URL}/storage/v1/object/upload/sign/{SUPABASE_MEDIA_BUCKET}/{object_path}"
    signed_url_full = ""
    token = ""
    try:
        req = urllib.request.Request(
            sign_endpoint,
            data=b"{}",
            headers=_supabase_headers(extra={"x-upsert": "true"}),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            res = json.loads(resp.read().decode("utf-8", errors="ignore"))
            rel_url = res.get("url") or ""
            token = res.get("token") or ""
            if rel_url:
                if rel_url.startswith("http"):
                    signed_url_full = rel_url
                elif rel_url.startswith("/storage/v1"):
                    signed_url_full = f"{SUPABASE_URL}{rel_url}"
                else:
                    signed_url_full = f"{SUPABASE_URL}/storage/v1/{rel_url.lstrip('/')}"
    except Exception:
        pass

    public_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_MEDIA_BUCKET}/{object_path}"
    return {
        "ok": True,
        "bucket": SUPABASE_MEDIA_BUCKET,
        "object_path": object_path,
        "signed_upload_url": signed_url_full,
        "token": token,
        "public_url": public_url,
    }


def save_user_dashboard_cloud(user_key: str, dash_payload: dict):
    slug = _safe_user_storage_slug(user_key)
    obj_name = f"dashboards/{slug}.json"
    dash_payload["updated_at"] = time.time()
    raw = json.dumps(dash_payload).encode("utf-8")

    # Also cache in /tmp for 0ms read
    try:
        with open(os.path.join(LOCAL_STORAGE_DIR, f"vs_dash_{slug}.json"), "w", encoding="utf-8") as f:
            f.write(raw.decode("utf-8"))
    except Exception:
        pass
    if SUPABASE_EGRESS_RESTRICTED:
        return

    headers = _supabase_headers(
        extra={
            "x-upsert": "true",
            "cache-control": "no-cache, no-store, max-age=0",
        }
    )
    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{obj_name}",
            data=raw,
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=6).close()
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            _ensure_supabase_bucket()
            try:
                req2 = urllib.request.Request(
                    f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{obj_name}",
                    data=raw,
                    headers=headers,
                    method="POST",
                )
                urllib.request.urlopen(req2, timeout=6).close()
            except Exception:
                pass
    except Exception:
        pass


def load_user_dashboard_cloud(user_key: str) -> dict:
    slug = _safe_user_storage_slug(user_key)
    obj_name = f"dashboards/{slug}.json"
    best = {}
    best_ts = 0.0

    tmp_path = os.path.join(LOCAL_STORAGE_DIR, f"vs_dash_{slug}.json")
    if os.path.exists(tmp_path):
        try:
            with open(tmp_path, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    best = d
                    best_ts = float(d.get("updated_at") or 0.0)
        except Exception:
            pass

    # Local/Cloud read complete
    return best or {
        "niche_config": {},
        "active_workspace": {},
        "history_items": [],
        "updated_at": 0.0,
    }


def _extract_client_ip(headers) -> str:
    try:
        if not headers:
            return ""
        xff = (headers.get("x-forwarded-for") or headers.get("X-Forwarded-For") or "").strip()
        if xff:
            return xff.split(",")[0].strip()
        xri = (headers.get("x-real-ip") or headers.get("X-Real-IP") or "").strip()
        if xri:
            return xri
    except Exception:
        pass
    return ""


def _build_bot_job_candidate_slugs(user_email: str, client_ip: str = "") -> list:
    em = (user_email or "").strip().lower()
    em_slug = _safe_user_storage_slug(em) if em else ""
    ip_slug = _safe_user_storage_slug(client_ip) if client_ip else ""
    slugs = []
    # Strict per-user isolation: when a Studio user email is provided, NEVER fall back to shared IP or global slugs!
    if em_slug:
        if ip_slug:
            slugs.append(f"{em_slug}__ip_{ip_slug}")
        if em_slug not in slugs:
            slugs.append(em_slug)
        return slugs
    if ip_slug:
        slugs.append(f"pc_ip_job_{ip_slug}")
    return slugs


def _save_single_cloud_json(folder_prefix: str, slug: str, payload: dict):
    if not slug:
        return
    obj_name = f"{folder_prefix}/{slug}.json"
    raw = json.dumps(payload).encode("utf-8")
    try:
        with open(os.path.join(LOCAL_STORAGE_DIR, f"vs_{folder_prefix}_{slug}.json"), "w", encoding="utf-8") as f:
            f.write(raw.decode("utf-8"))
    except Exception:
        pass
    if SUPABASE_EGRESS_RESTRICTED:
        return

    headers = _supabase_headers(
        extra={
            "x-upsert": "true",
            "cache-control": "no-cache, no-store, max-age=0",
        }
    )
    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{obj_name}",
            data=raw,
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=6).close()
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            _ensure_supabase_bucket()
            try:
                req2 = urllib.request.Request(
                    f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{obj_name}",
                    data=raw,
                    headers=headers,
                    method="POST",
                )
                urllib.request.urlopen(req2, timeout=6).close()
            except Exception:
                pass
    except Exception:
        pass


def _load_single_cloud_json(folder_prefix: str, slug: str) -> dict:
    if not slug:
        return {}
    obj_name = f"{folder_prefix}/{slug}.json"
    best = {}
    best_ts = 0.0

    tmp_path = os.path.join(LOCAL_STORAGE_DIR, f"vs_{folder_prefix}_{slug}.json")
    if os.path.exists(tmp_path):
        try:
            with open(tmp_path, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    best = d
                    best_ts = float(d.get("updated_at") or 0.0)
        except Exception:
            pass

    # Local/Cloud single json read complete
    return best


def save_pc_bound_email(client_ip: str, user_email: str, device_token: str = ""):
    ip_clean = (client_ip or "").strip()
    em_clean = (user_email or "").strip().lower()
    if not ip_clean or not em_clean:
        return
    ip_slug = _safe_user_storage_slug(ip_clean)
    existing = _load_single_cloud_json("pc_binds", f"pc_ip_{ip_slug}") or {}
    conn_list = [em_clean]
    payload = {
        "email": em_clean,
        "latest_email": em_clean,
        "connected_emails": conn_list,
        "client_ip": ip_clean,
        "device_token": (device_token or existing.get("device_token") or "").strip(),
        "updated_at": time.time(),
        "last_ext_ping": float(existing.get("last_ext_ping") or 0.0),
    }
    _save_single_cloud_json("pc_binds", f"pc_ip_{ip_slug}", payload)


def load_pc_bound_email(client_ip: str, mark_ext_ping: bool = False) -> dict:
    ip_clean = (client_ip or "").strip()
    if not ip_clean:
        return {}
    ip_slug = _safe_user_storage_slug(ip_clean)
    doc = _load_single_cloud_json("pc_binds", f"pc_ip_{ip_slug}")
    if isinstance(doc, dict) and (doc.get("email") or doc.get("connected_emails")):
        raw_conn = doc.get("connected_emails") if isinstance(doc.get("connected_emails"), list) else []
        conn_clean = []
        for x in raw_conn:
            s = str(x or "").strip().lower()
            if s and "@" in s and s not in conn_clean:
                conn_clean.append(s)
        primary_em = str(doc.get("latest_email") or doc.get("email") or "").strip().lower()
        if primary_em and "@" in primary_em:
            if primary_em in conn_clean:
                conn_clean.remove(primary_em)
            conn_clean.append(primary_em)
        elif conn_clean:
            primary_em = conn_clean[-1]
        doc["email"] = primary_em
        doc["connected_emails"] = [primary_em] if primary_em else []
        if mark_ext_ping:
            now_ts = time.time()
            if now_ts - float(doc.get("last_ext_ping") or 0.0) > 8.0:
                doc["last_ext_ping"] = now_ts
                _save_single_cloud_json("pc_binds", f"pc_ip_{ip_slug}", doc)
        return doc
    return {}


def save_bot_job_cloud(user_email: str, job_payload: dict, client_ip: str = ""):
    job_payload["updated_at"] = time.time()
    eff_ip = (client_ip or job_payload.get("client_ip") or "").strip()
    if eff_ip:
        job_payload["client_ip"] = eff_ip
    eff_email = (job_payload.get("owner_email") or job_payload.get("email") or user_email or "").strip().lower()
    if eff_email:
        job_payload["email"] = eff_email
        job_payload["owner_email"] = eff_email
    slugs = _build_bot_job_candidate_slugs(eff_email, eff_ip)
    if user_email and user_email.strip().lower() != eff_email:
        for s in _build_bot_job_candidate_slugs(user_email, eff_ip):
            if s not in slugs:
                slugs.append(s)
    for slug in slugs:
        _save_single_cloud_json("bot_jobs", slug, job_payload)


def find_active_bot_job_for_ip(client_ip: str) -> dict:
    ip_clean = (client_ip or "").strip()
    candidates = []
    if ip_clean:
        candidates.append(f"ip_{_safe_user_storage_slug(ip_clean)}")
    for known_em in ("shahbazzaidi250@gmail.com", "abdulrafay40023@gmail.com"):
        for s in _build_bot_job_candidate_slugs(known_em, ip_clean):
            if s not in candidates:
                candidates.append(s)
    best_active = {}
    best_ts = 0.0
    for slug in candidates:
        d = _load_single_cloud_json("bot_jobs", slug)
        if isinstance(d, dict) and d.get("active") and isinstance(d.get("prompts"), list) and len(d.get("prompts")) > 0:
            ts = float(d.get("updated_at") or 0.0)
            if (time.time() - ts) < 7200 and ts >= best_ts:
                best_active = d
                best_ts = ts
    return best_active


def load_bot_job_cloud(user_email: str, client_ip: str = "") -> dict:
    em_clean = (user_email or "").strip().lower()
    slugs = _build_bot_job_candidate_slugs(em_clean, client_ip)
    best = {}
    best_ts = 0.0
    for slug in slugs:
        d = _load_single_cloud_json("bot_jobs", slug)
        if isinstance(d, dict) and d:
            doc_owner = str(d.get("owner_email") or d.get("email") or "").strip().lower()
            if em_clean and doc_owner and doc_owner != em_clean:
                continue
            ts = float(d.get("updated_at") or 0.0)
            if d.get("active") and (time.time() - ts) < 7200:
                return d
            if ts >= best_ts or not best:
                best = d
                best_ts = ts
    return best or {"active": False, "prompts": [], "generated_images": {}, "updated_at": 0.0}


def upload_bot_scene_image_cloud(user_email: str, job_id: str, scene_num: int, data_url_or_b64: str) -> str:
    if not data_url_or_b64 or SUPABASE_EGRESS_RESTRICTED:
        return ""
    try:
        _ensure_supabase_media_bucket()
        slug = _safe_user_storage_slug(user_email)
        safe_job = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(job_id or "job"))[:40]
        b64_part = data_url_or_b64.split(",", 1)[1] if "," in data_url_or_b64 else data_url_or_b64
        img_bytes = base64.b64decode(b64_part)
        object_path = f"bot_scenes/{slug}/{safe_job}_{int(scene_num)}.png"
        headers = _supabase_headers(
            content_type="image/png",
            extra={
                "x-upsert": "true",
                "cache-control": "no-cache, no-store, max-age=0",
            },
        )
        req = urllib.request.Request(
            f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_MEDIA_BUCKET}/{object_path}",
            data=img_bytes,
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=12).close()
        return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_MEDIA_BUCKET}/{object_path}"
    except Exception:
        return ""


def save_chatgpt_job_cloud(user_email: str, job_payload: dict, client_ip: str = ""):
    job_payload["updated_at"] = time.time()
    eff_ip = (client_ip or job_payload.get("client_ip") or "").strip()
    if eff_ip:
        job_payload["client_ip"] = eff_ip
    eff_email = (job_payload.get("owner_email") or job_payload.get("email") or user_email or "").strip().lower()
    if eff_email:
        job_payload["email"] = eff_email
        job_payload["owner_email"] = eff_email
    slugs = _build_bot_job_candidate_slugs(eff_email, eff_ip)
    if user_email and user_email.strip().lower() != eff_email:
        for s in _build_bot_job_candidate_slugs(user_email, eff_ip):
            if s not in slugs:
                slugs.append(s)
    for slug in slugs:
        _save_single_cloud_json("chatgpt_jobs", slug, job_payload)


def load_chatgpt_job_cloud(user_email: str, client_ip: str = "") -> dict:
    em_clean = (user_email or "").strip().lower()
    slugs = _build_bot_job_candidate_slugs(em_clean, client_ip)
    best = {}
    best_ts = 0.0
    for slug in slugs:
        d = _load_single_cloud_json("chatgpt_jobs", slug)
        if isinstance(d, dict) and d:
            doc_owner = str(d.get("owner_email") or d.get("email") or "").strip().lower()
            if em_clean and doc_owner and doc_owner != em_clean:
                continue
            ts = float(d.get("updated_at") or 0.0)
            if d.get("active") and (time.time() - ts) < 3600:
                return d
            if ts >= best_ts or not best:
                best = d
                best_ts = ts
    return best or {"active": False, "status": "idle", "script_text": "", "updated_at": 0.0}


VIRAL_NICHE_METADATA = {
    "financial_wealth": {
        "label": "💰 Financial & Quiet Wealth POV",
        "script_type": "wealth_story",
        "desc": "Second-person POV quiet wealth, stealth millionaires, looking broke while building $500k-$1M+, compound interest & financial discipline (@MoneyLifePOV / @QuietCapitalUSA style).",
    },
    "stocks_ai_trading": {
        "label": "📈 Stocks, AI Trading & Market Investing",
        "script_type": "wealth_story",
        "desc": "Stocks, index funds, AI trading systems, dividend compounding, market crashes, smart portfolio building & wall street psychology.",
    },
    "passive_income": {
        "label": "🏦 Passive Income, Cashflow & Digital Assets",
        "script_type": "wealth_story",
        "desc": "Automated cashflow, silent businesses, dividend streams, real estate vs stocks, and escaping the 9-to-5 rat race.",
    },
    "quiet_living": {
        "label": "🏡 Quiet Living, Minimalist Routine & Peace",
        "script_type": "living_type",
        "desc": "How you quietly live when you stop chasing validation—peaceful daily routines, debt-free living, and quiet confidence.",
    },
    "dark_psychology": {
        "label": "🧠 Dark Psychology, Stoicism & Human Behavior",
        "script_type": "psychology_mindset",
        "desc": "Hidden psychological rules, why silent people win, stoic emotional control, and social power dynamics.",
    },
    "true_crime": {
        "label": "🕵️ True Crime, Mystery & Detective Cases",
        "script_type": "mystery_crime",
        "desc": "Gripping unsolved mysteries, detective investigations, shocking clues, and vanished fortunes.",
    },
    "horror_scary": {
        "label": "👻 Horror, Scary Night & Paranormal Stories",
        "script_type": "horror_story",
        "desc": "Spine-chilling late-night horror encounters, eerie isolation, haunted places, and terrifying twists.",
    },
    "documentary": {
        "label": "🌍 Documentary, Billionaire Empires & Hidden Systems",
        "script_type": "documentary_type",
        "desc": "Deep investigative documentary storytelling on hidden financial empires, corporate rises/falls, and how the world really works.",
    },
    "ancient_history": {
        "label": "🏛️ Ancient History, Empires & Forgotten Secrets",
        "script_type": "history_mythology",
        "desc": "Epic historical storytelling on ancient civilizations, lost wealth, kings, and timeless lessons.",
    },
    "tech_ai_future": {
        "label": "🚀 AI Revolution, Future Tech & Wealth Shifts",
        "script_type": "explainer_guide",
        "desc": "How AI, automation, algorithms, and future tech are creating a new generation of quiet wealth.",
    },
}


def generate_viral_topics_list(category_key: str = "", keywords: str = "", youtube_ref: str = "") -> dict:
    kw = (keywords or "").strip()
    yt_ref = (youtube_ref or "").strip()

    # Fetch real live YouTube viral data from YouTube Data API v3 (for both keywords and optional Reference YouTube Channel/Video!)
    yt_live_research = fetch_youtube_api_viral_research(query_keywords=kw, channel_or_video_ref=yt_ref)

    kw_instruction = (
        f'USER CHOSEN TOPIC NICHE / KEYWORDS (MANDATORY SUBJECT MATTER): "{kw}" — Every single one of the 5 viral topics MUST be 100% about "{kw}"!'
        if kw
        else 'Generate 5 ultra-viral, high-CTR YouTube storytelling topics matching the Reference Channel theme.'
    )
    ref_instruction = (
        f'USER REFERENCE YOUTUBE CHANNEL / VIDEO (FOR STYLE & HOOK STRUCTURE ONLY): "{yt_ref}" — '
        f'Extract ONLY the viral title formula, psychological hook style, POV framing, and storytelling tone from "{yt_ref}", '
        f'and apply that exact style to generate 5 topics strictly on the user\'s chosen Niche/Keywords ("{kw or yt_ref}")! '
        f'Even if the Reference Channel covers a different topic (e.g. general financial health while the user entered "{kw}"), keep the SUBJECT 100% on "{kw}" and use the Reference Channel ONLY for its viral style!'
        if yt_ref
        else ''
    )

    prompt = f"""You are the #1 viral YouTube strategist and title engineer behind top-performing storytelling channels (@MoneyLifePOV, @QuietCapitalUSA, and viral finance/documentary/story channels).
{kw_instruction}
{ref_instruction}

=== LIVE YOUTUBE DATA API v3 TRENDING & REFERENCE CHANNEL RESEARCH ===
{yt_live_research or "Use proven high-CTR YouTube viral title formulas with concrete numbers, strong curiosity gaps, and relatable POV stakes."}

Generate EXACTLY 5 of the absolute best, ultra-clickable, high-CTR viral YouTube video topics that will perform exceptionally well on YouTube for this theme.
Each topic must:
- Have a magnetic, high-CTR YouTube title (using strong contrast, specific stakes/numbers, or POV intrigue).
- Include a 1-sentence gripping cold-open script hook preview showing how the video script will open.

Output ONLY 5 numbered lines in this exact pipe-separated format:
1 | <Viral YouTube Video Title> | <1-sentence gripping cold-open script hook>
2 | <Viral YouTube Video Title> | <1-sentence gripping cold-open script hook>
3 | <Viral YouTube Video Title> | <1-sentence gripping cold-open script hook>
4 | <Viral YouTube Video Title> | <1-sentence gripping cold-open script hook>
5 | <Viral YouTube Video Title> | <1-sentence gripping cold-open script hook>"""

    topics = []
    try:
        raw = call_gemini_api(prompt, max_tokens=2048)
        for line in (raw or "").splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^(\d+)[\.\)\s\|:\-]+(.+)$", line)
            if not m:
                continue
            rest = m.group(2).strip()
            parts = [p.strip().strip('"\'*') for p in rest.split("|") if p.strip()]
            if len(parts) >= 2:
                title_txt, hook_txt = parts[0], parts[1]
            else:
                title_txt = parts[0]
                hook_txt = "High-retention YouTube viral story with 3–4s concrete visual scenes."
            if len(title_txt) > 8:
                topics.append({
                    "id": len(topics) + 1,
                    "title": title_txt,
                    "hook": hook_txt,
                    "script_type": "wealth_story",
                })
            if len(topics) >= 5:
                break
    except Exception:
        pass

    kw_tag = kw.title() if kw else "Quiet Wealth & Smart Investing"
    fallback_templates = [
        (f"How You Quietly Build $500,000 With {kw_tag} While Everyone Thinks You're Broke", f"It is 6:15 PM on a rainy Friday, and while your coworkers flex leased cars, your {kw_tag} portfolio quietly crosses $500,000."),
        (f"The Silent {kw_tag} Strategy That Turns $200 a Week Into Complete Financial Freedom", f"Nobody at the dinner table notices your old hoodie, because none of them can see your automated {kw_tag} account compounding every night."),
        (f"Why 99% of People Lose at {kw_tag} — And How You Quietly Win Without Stress", f"While panic floods the market at 9:30 AM, you sip your black coffee and let your rules-based {kw_tag} system do the heavy lifting."),
        (f"What Happens When You Secretly Master {kw_tag} for 36 Months Straight", f"Month one feels invisible, month twelve feels slow, but by month thirty-six your {kw_tag} returns outpace your daytime salary."),
        (f"You Look Completely Average on the Outside, But Your {kw_tag} System Prints Cash", f"You drive a paid-off ten-year-old sedan into the parking lot while your {kw_tag} dashboard quietly deposits another four-figure gain."),
    ]
    while len(topics) < 5:
        idx = len(topics)
        t_title, t_hook = fallback_templates[idx % len(fallback_templates)]
        topics.append({
            "id": idx + 1,
            "title": t_title,
            "hook": t_hook,
            "script_type": "wealth_story",
        })

    return {
        "ok": True,
        "keywords": kw,
        "youtube_ref": yt_ref,
        "yt_research_summary": yt_live_research[:1200] if yt_live_research else "",
        "topics": topics[:5],
    }



async def generate_edge_tts_bytes(text: str, voice: str) -> bytes:
    communicate = edge_tts.Communicate(text, voice)
    out = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            out.write(chunk["data"])
    return out.getvalue()


def detect_script_vocal_expression(text: str, niche_config: dict = None) -> tuple:
    niche_config = niche_config or {}
    niche_type = str(niche_config.get("niche_type") or "").strip().lower()
    script_type = str(niche_config.get("script_type") or "").strip().lower()
    tl = (text or "").lower()

    horror_hits = len(re.findall(
        r"\b(horror|scary|ghost|shadow|shadows|scream|screamed|blood|whisper|whispered|creak|creaking|footsteps|darkness|terrified|haunted|nightmare|eerie|chilling|corpse|demon|paranormal|graveyard|monster|cursed|dead\s+of\s+night)\b|(?:भूत|साया|चीख|डर|खौफ|अंधेरा|डरावना|आत्मा|खून|कब्र|राक्षस|सन्नाटा|आहट)",
        tl,
    ))
    funny_hits = len(re.findall(
        r"\b(funny|laugh|laughed|laughing|hilarious|joke|joking|comedy|ridiculous|absurd|prank|awkward|sarcastic|grin|chuckle|chuckled|smirk|silly|goofy|oops|bizarre|clown)\b|(?:मज़ाक|हंसी|मजेदार|पागलपन|मुस्कुरा|हंसना|हंसा|अजीबोगरीब|चुटकुला)",
        tl,
    ))
    mystery_hits = len(re.findall(
        r"\b(detective|murder|suspect|evidence|clue|clues|investigation|police|alibi|crime|disappeared|mystery|victim|interrogation|forensic|autopsy|missing|conspiracy)\b|(?:सुराग|पुलिस|कातिल|रहस्य|तहकीकात|जुर्म|गायब|गवाह|खुलासा)",
        tl,
    ))
    emotional_hits = len(re.findall(
        r"\b(tears|cried|crying|heartbroken|forgive|grief|lonely|mother|father|pain|healing|regret|peaceful|soul|broken\s+heart|sobbed|hugged)\b|(?:आंसू|दर्द|रोने|अकेला|माँ|पिता|दिल|सुकून|तन्हाई|जज़्बात)",
        tl,
    ))

    # Determine dominant mood from explicit niche/script_type OR strong script chunk signals
    if niche_type == "horror_dark" or script_type == "horror_story" or horror_hits >= 2:
        return (
            "Spine-Chilling Horror, Eerie & Suspenseful",
            "Deliver with spine-tingling, eerie, hushed suspenseful intimacy. Lower your vocal projection slightly into a dark, haunting atmospheric tone with tense pauses before scary reveals, subtle dread, and goosebump-inducing storytelling expressions.",
        )
    if niche_type == "comedy_funny" or script_type == "comedy_funny" or funny_hits >= 2:
        return (
            "Funny, Playful, Witty & Lighthearted",
            "Deliver with a warm audible smile in your voice, lively comedic timing, playful and witty inflection on punchlines and ironic twists, and upbeat, entertaining human expressions.",
        )
    if niche_type == "crime_detective" or script_type == "mystery_crime" or mystery_hits >= 2:
        return (
            "Gripping True Crime, Mystery & Detective Tension",
            "Deliver with serious noir detective tension, sharp investigative focus, deliberate suspenseful pauses before revealing key clues or plot twists, and captivating dramatic weight.",
        )
    if script_type in ("living_type", "survival_disaster") or emotional_hits >= 2:
        return (
            "Deeply Emotional, Heartfelt & Intimate",
            "Deliver with sincere human empathy, warm emotional vulnerability, gentle heartfelt resonance on touching or painful moments, and comforting close-mic intimacy.",
        )
    if niche_type == "psychology_stoic" or script_type in ("psychology_mindset", "motivation_stoic"):
        return (
            "Razor-Sharp Dark Psychology & Stoic Authority",
            "Deliver with calm, commanding, unshakeable psychological confidence, deep composed intensity, and powerful emphasis on mindset and human behavior truths.",
        )
    if niche_type in ("documentary", "history_ancient", "scifi_future") or script_type in ("documentary_type", "facts_type", "history_mythology", "explainer_guide"):
        return (
            "Cinematic Documentary, Curiosity & Grand Discovery",
            "Deliver with captivating documentary authority, intellectual wonder on fascinating facts and historical/scientific reveals, and crisp, engaging storytelling clarity.",
        )
    return (
        "Warm, Slightly Dark, Confident & Naturally Expressive Storyteller",
        "Deliver with natural, engaging human expressions — conversational confidence, subtle dry wit on humorous/ironic lines, thoughtful pauses before key numbers, natural inflection on contrasts, and genuine emotional weight on turning points.",
    )


def _wsola_stretch_pcm_segment(samples, sample_rate: int, stretch_factor: float):
    """
    Pitch-preserving Synchronized Overlap-Add (WSOLA) time-stretching for 16-bit mono PCM array('h').
    stretch_factor > 1.0 slows down fast speech to normal speed WITHOUT changing vocal pitch.
    stretch_factor < 1.0 speeds up slow speech to normal speed WITHOUT changing vocal pitch.
    """
    import array
    import math

    n_in = len(samples)
    if abs(stretch_factor - 1.0) < 0.025 or n_in < sample_rate // 4:
        return samples

    stretch_factor = max(0.75, min(1.48, float(stretch_factor)))
    win_len = max(120, int(sample_rate * 0.030))
    if win_len % 2 == 1:
        win_len += 1
    half_win = win_len // 2
    search_len = max(24, int(sample_rate * 0.005))

    hann = [0.5 * (1.0 - math.cos(2.0 * math.pi * i / win_len)) for i in range(win_len)]
    out_len = int(n_in * stretch_factor) + win_len
    out_buf = [0.0] * out_len

    syn_pos = 0
    expected_ana_pos = 0.0
    prev_ana_pos = 0
    corr_span = min(half_win, 160)

    while syn_pos + win_len < out_len and int(expected_ana_pos) + win_len + search_len < n_in:
        center_ana = int(expected_ana_pos)
        if syn_pos == 0:
            best_ana = center_ana
        else:
            target_pos = prev_ana_pos + half_win
            best_ana = center_ana
            if 0 <= target_pos and target_pos + corr_span < n_in:
                best_corr = -1e30
                s_min = max(0, center_ana - search_len)
                s_max = min(n_in - win_len, center_ana + search_len)
                ref = samples[target_pos : target_pos + corr_span : 8]
                for cand in range(s_min, s_max + 1, 4):
                    c_slice = samples[cand : cand + corr_span : 8]
                    corr = sum(a * b for a, b in zip(ref, c_slice))
                    if corr > best_corr:
                        best_corr = corr
                        best_ana = cand

        seg = samples[best_ana : best_ana + win_len]
        for i in range(win_len):
            out_buf[syn_pos + i] += seg[i] * hann[i]

        prev_ana_pos = best_ana
        syn_pos += half_win
        expected_ana_pos += half_win / stretch_factor

    valid_end = max(0, syn_pos)
    if valid_end <= 0:
        return samples
    return array.array("h", (max(-32768, min(32767, int(out_buf[i]))) for i in range(valid_end)))


def _estimate_syllable_rate(samples, sample_rate: int) -> float:
    """
    Estimates local syllable rate (syllable peaks per second) of a voiced PCM segment
    so any sentence inside a chunk that sped up or slowed down can be leveled to steady normal pace.
    """
    dur = len(samples) / float(max(1, sample_rate))
    if dur < 0.65:
        return 0.0
    env_win = max(1, int(sample_rate * 0.025))  # 25ms envelope window
    n_env = len(samples) // env_win
    if n_env < 8:
        return 0.0
    env = []
    for i in range(n_env):
        chunk = samples[i * env_win : (i + 1) * env_win]
        env.append(sum(abs(x) for x in chunk) / float(len(chunk)))
    # Smooth envelope with 3-tap moving average
    sm = [env[0]] + [(env[i - 1] + env[i] + env[i + 1]) / 3.0 for i in range(1, n_env - 1)] + [env[-1]]
    mean_e = sum(sm) / float(len(sm))
    thresh = mean_e * 0.55
    peaks = 0
    last_p = -4
    for i in range(1, n_env - 1):
        if sm[i] > thresh and sm[i] >= sm[i - 1] and sm[i] > sm[i + 1] and (i - last_p) >= 3:
            peaks += 1
            last_p = i
    return peaks / dur


def _compact_pcm_silences(
    frames: bytes,
    sample_rate: int = 24000,
    sampwidth: int = 2,
    nchannels: int = 1,
    text: str = "",
    target_wpm: float = 185.0,
) -> bytes:
    if not frames or sampwidth != 2:
        return frames
    try:
        import array
        samples = array.array("h")
        samples.frombytes(frames[: len(frames) - (len(frames) % 2)])
        total_samples = len(samples)
        if total_samples < sample_rate // 4:
            return frames

        win_size = max(1, int(sample_rate * 0.015) * nchannels)  # 15ms window
        num_wins = total_samples // win_size
        if num_wins < 4:
            return frames

        silence_thresh = 340
        is_silent = []
        for w in range(num_wins):
            start = w * win_size
            chunk = samples[start : start + win_size]
            peak = max(abs(max(chunk)), abs(min(chunk))) if chunk else 0
            is_silent.append(peak < silence_thresh)

        # Trim leading silence (keep max 2 windows = 30ms) and trailing silence (keep max 4 windows = 60ms)
        first_voice = 0
        while first_voice < num_wins and is_silent[first_voice]:
            first_voice += 1
        start_win = max(0, first_voice - 2)

        last_voice = num_wins - 1
        while last_voice > start_win and is_silent[last_voice]:
            last_voice -= 1
        end_win = min(num_wins, last_voice + 4)

        # Keep crisp natural sentence breathing pauses (~0.165s = 11 windows) so speech flows at steady normal YouTube speed
        # with 100% PURE, UNTOUCHED PCM samples (ZERO WSOLA time-stretching so voice NEVER cracks or distorts!).
        max_silent_wins = 11
        out_samples = array.array("h")
        silent_run = 0

        for w in range(start_win, end_win):
            if is_silent[w]:
                silent_run += 1
                if silent_run > max_silent_wins:
                    continue
            else:
                silent_run = 0
            s_idx = w * win_size
            out_samples.extend(samples[s_idx : s_idx + win_size])

        return out_samples.tobytes() if len(out_samples) > 0 else frames
    except Exception:
        return frames


def generate_gemini_tts_wav(text: str, language: str = "english", voice: str = "Achird", niche_config: dict = None) -> bytes:
    is_hindi = (str(language or "").strip().lower() == "hindi") or bool(re.search(r"[\u0900-\u097F]", text))
    clean_voice = str(voice or "Achird").strip()
    is_female = clean_voice.lower() in ("sulafat", "female", "elena", "maya", "aoede", "kore")
    gemini_voice_name = "Sulafat" if is_female else "Achird"

    # Preserve natural punctuation (periods, em-dashes, ellipses, questions, and paragraph breaks)
    # so Gemini TTS delivers authentic ElevenLabs-grade expressive inflection and steady normal cadence.
    clean_tts_text = str(text or "").replace("\ufffd", " — ")
    clean_tts_text = re.sub(r"(\d)\s+\.\s*(\d)", r"\1:\2", clean_tts_text)
    clean_tts_text = re.sub(r"[ \t]+", " ", clean_tts_text)
    clean_tts_text = re.sub(r"\n{3,}", "\n\n", clean_tts_text).strip() or str(text or "").strip()

    mood_title, mood_direction = detect_script_vocal_expression(clean_tts_text, niche_config)
    dynamic_acting_rule = (
        "Dynamic ElevenLabs-Caliber Human Expression (Mango Economics Storytelling Style): "
        "Speak like a charismatic, intelligent, warm human narrator explaining a fascinating story to a friend. "
        "Bring genuine vocal life and subtle emotion to every sentence — use knowing emphasis on key numbers and reveals, "
        "curious conversational inflection on rhetorical questions, warm dry wit on ironic or humorous lines, "
        "and grounded sincerity on serious turning points."
    )
    steady_pace_rule = (
        "Pacing & Rhythm (STRICT NORMAL SPEED): Speak at a calm, steady, measured NORMAL conversational speed "
        "(~150 words per minute) from the very first word to the very last word. "
        "NEVER rush or speed up in the middle of a paragraph, and NEVER drag words out too slowly. "
        "Maintain an even, rock-steady normal pace with natural brief breathing pauses between sentences."
    )

    if is_hindi:
        if is_female:
            voice_style_line = (
                "Style: Crisp, warm, velvety, expressive natural female voice (Elena) speaking conversational Hindi (हिन्दी) "
                "with ElevenLabs-grade clarity and steady normal storytelling speed."
            )
            profile_header = f"# AUDIO PROFILE: Elena — Expressive Hindi Storyteller ({mood_title})"
        else:
            voice_style_line = (
                "Style: Crisp, warm, resonant, expressive natural male voice (Hale) speaking conversational Hindi (हिन्दी) "
                "with ElevenLabs-grade clarity and steady normal storytelling speed."
            )
            profile_header = f"# AUDIO PROFILE: Hale — Expressive Hindi Storyteller ({mood_title})"

        styled_input = (
            f"{profile_header}\n"
            f"## \"{mood_title} — Natural Expressive Hindi Narrator\"\n\n"
            "### DIRECTOR'S NOTES\n"
            f"{voice_style_line}\n"
            f"Primary Mood & Expressions: {mood_direction}\n"
            f"{dynamic_acting_rule}\n"
            f"{steady_pace_rule}\n"
            "Language & Accent: Clear, natural native Indian Hindi pronunciation.\n\n"
            "#### TRANSCRIPT\n"
            f"{clean_tts_text}"
        )
    else:
        if is_female:
            voice_style_line = (
                "Style: Crisp, warm, velvety, expressive American female voice (Elena) "
                "with ElevenLabs-grade vocal clarity, human warmth, and steady normal storytelling speed."
            )
            profile_header = f"# AUDIO PROFILE: Elena — Expressive Female Storyteller ({mood_title})"
        else:
            voice_style_line = (
                "Style: Crisp, warm, resonant, expressive American male voice (Hale / Mango Economics narrator style) "
                "with ElevenLabs-grade vocal clarity, human warmth, and steady normal storytelling speed."
            )
            profile_header = f"# AUDIO PROFILE: Hale — Expressive Male Storyteller ({mood_title})"

        styled_input = (
            f"{profile_header}\n"
            f"## \"{mood_title} — ElevenLabs-Caliber Expressive YouTube Storyteller\"\n\n"
            "### DIRECTOR'S NOTES\n"
            f"{voice_style_line}\n"
            f"Primary Mood & Expressions: {mood_direction}\n"
            f"{dynamic_acting_rule}\n"
            f"{steady_pace_rule}\n"
            "Accent: Smooth, clear, natural neutral American English.\n\n"
            "#### TRANSCRIPT\n"
            f"{clean_tts_text}"
        )

    last_err = None
    rotated_keys = _get_rotated_gemini_keys()

    for api_key in rotated_keys:
        for model_name in GEMINI_TTS_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            payload = json.dumps({
                "contents": [{"role": "user", "parts": [{"text": styled_input}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": gemini_voice_name
                            }
                        }
                    }
                }
            }).encode("utf-8")
            try:
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=50) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    part = res["candidates"][0]["content"]["parts"][0]["inlineData"]
                    raw_bytes = base64.b64decode(part["data"])
                    mime = part.get("mimeType", "")

                    sample_rate = 24000
                    sampwidth = 2
                    nchannels = 1
                    if "wav" in mime.lower():
                        with wave.open(io.BytesIO(raw_bytes), "rb") as wf:
                            sample_rate = wf.getframerate()
                            sampwidth = wf.getsampwidth()
                            nchannels = wf.getnchannels()
                            frames = wf.readframes(wf.getnframes())
                    else:
                        frames = raw_bytes

                    raw_dur_sec = len(frames) / float(max(1, sample_rate * sampwidth * nchannels))
                    t_words = len(re.findall(r"[A-Za-z0-9\u0900-\u097F']+", clean_tts_text))
                    if t_words >= 8 and raw_dur_sec > (t_words / 88.0) * 60.0:
                        # Safeguard: if model ever read prompt metadata, retry cleanly
                        last_err = f"Gemini TTS {model_name}: unexpected long duration ({raw_dur_sec:.1f}s for {t_words} words)"
                        continue

                    frames = _compact_pcm_silences(
                        frames,
                        sample_rate=sample_rate,
                        sampwidth=sampwidth,
                        nchannels=nchannels,
                        text=clean_tts_text,
                        target_wpm=185.0,
                    )
                    silence_samples = int(sample_rate * 0.08)
                    trailing_silence = b"\x00" * (silence_samples * sampwidth * nchannels)

                    wav_buf = io.BytesIO()
                    with wave.open(wav_buf, "wb") as out_wf:
                        out_wf.setnchannels(nchannels)
                        out_wf.setsampwidth(sampwidth)
                        out_wf.setframerate(sample_rate)
                        out_wf.writeframes(frames + trailing_silence)
                    return wav_buf.getvalue()
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                last_err = f"Gemini TTS {model_name} HTTP {e.code}: {err_body[:180]}"
                continue
            except Exception as e:
                last_err = f"Gemini TTS {model_name}: {str(e)}"
                continue

    raise RuntimeError(last_err or "Gemini TTS voice generation failed across all keys")


def _try_leonardo_ai(prompt_text: str, api_key: str, niche_type: str = "", anim_style: str = "") -> tuple:
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {api_key}",
    }
    clean_leo_prompt = _ensure_pure_english_prompt(prompt_text)[:900]
    neg_prompt = (
        "brown texture, muddy abstract noise, dirt wall, blank background, blurry, out of focus, "
        "distorted anatomy, extra limbs, watermark, text overlay, signature, deformed, low quality"
    )
    if niche_type == "stickman" or anim_style in ("2d_illustrated", "flat_vector", "sketch", "anime_style"):
        neg_prompt += ", photorealistic 3d photo, blurry brown noise"

    # Explicit modern native 16:9 SDXL / Phoenix modelIds so Leonardo NEVER falls back to legacy 2022 SD 1.5 (which produces brown noise at 1024x576)!
    # - e71a1c2f-4f80-4800-934f-2c68979d8cc8 = Leonardo Anime XL (crisp 2D illustration, stickman, cartoon, anime)
    # - b24e16ff-06e3-43eb-8d33-4416c2d75876 = Leonardo Lightning XL (fast, sharp SDXL 1024x576)
    # - aa77f04e-3eec-4034-9c07-d0f619684628 = Leonardo Kino XL (cinematic widescreen SDXL)
    # - de7d3faf-762f-48e0-b3b7-9d0ac3a3fcf3 = Leonardo Phoenix (flagship prompt adherence)
    is_2d_or_stickman = (niche_type in ("stickman", "white_char")) or (anim_style in ("2d_illustrated", "flat_vector", "sketch", "anime_style", "comic_noir"))
    primary_model = "e71a1c2f-4f80-4800-934f-2c68979d8cc8" if is_2d_or_stickman else "aa77f04e-3eec-4034-9c07-d0f619684628"

    configs = [
        {
            "prompt": clean_leo_prompt,
            "negative_prompt": neg_prompt,
            "modelId": primary_model,
            "num_images": 1,
            "width": 1024,
            "height": 576,
            "alchemy": False,
        },
        {
            "prompt": clean_leo_prompt,
            "negative_prompt": neg_prompt,
            "modelId": "b24e16ff-06e3-43eb-8d33-4416c2d75876",
            "num_images": 1,
            "width": 1024,
            "height": 576,
            "alchemy": False,
        },
        {
            "prompt": clean_leo_prompt,
            "negative_prompt": neg_prompt,
            "modelId": "de7d3faf-762f-48e0-b3b7-9d0ac3a3fcf3",
            "num_images": 1,
            "width": 1024,
            "height": 576,
            "alchemy": False,
        },
        {
            "prompt": clean_leo_prompt,
            "negative_prompt": neg_prompt,
            "num_images": 1,
            "width": 1024,
            "height": 576,
            "alchemy": True,
        },
    ]
    gen_id = None
    last_leo_err = None
    for cfg in configs:
        try:
            req = urllib.request.Request(
                "https://cloud.leonardo.ai/api/rest/v1/generations",
                data=json.dumps(cfg).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                gen_id = (res.get("sdGenerationJob") or {}).get("generationId")
                if gen_id:
                    break
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            last_leo_err = f"Leonardo AI HTTP {e.code}: {err_body[:180]}"
            if e.code in (401, 403):
                break
        except Exception as e:
            last_leo_err = f"Leonardo AI Error: {str(e)}"

    if not gen_id:
        raise RuntimeError(last_leo_err or "Leonardo AI did not return generationId")

    poll_url = f"https://cloud.leonardo.ai/api/rest/v1/generations/{gen_id}"
    for _ in range(22):
        time.sleep(2.0)
        try:
            poll_req = urllib.request.Request(poll_url, headers=headers, method="GET")
            with urllib.request.urlopen(poll_req, timeout=20) as p_resp:
                p_data = json.loads(p_resp.read().decode("utf-8"))
                gen_obj = p_data.get("generations_by_pk") or {}
                imgs = gen_obj.get("generated_images") or []
                if imgs and imgs[0].get("url"):
                    img_url = imgs[0]["url"]
                    img_req = urllib.request.Request(img_url, headers={"User-Agent": "VideoStudio/1.0"})
                    with urllib.request.urlopen(img_req, timeout=30) as img_resp:
                        mime = img_resp.headers.get("Content-Type") or "image/jpeg"
                        return img_resp.read(), mime
                status = (gen_obj.get("status") or "").upper()
                if status == "FAILED":
                    raise RuntimeError("Leonardo AI generation status FAILED")
        except Exception as e:
            last_leo_err = str(e)
    raise RuntimeError(last_leo_err or "Leonardo AI generation timed out")


def generate_scene_image(prompt_text: str, api_key_raw: str, ref_image_b64: str = "", niche_config: dict = None) -> tuple:
    keys = [k.strip() for k in re.split(r"[,;\s]+", api_key_raw.strip()) if k.strip()]
    if not keys:
        raise RuntimeError("Paid Image API Key is required!")

    niche_config = niche_config or {}
    anim_style = (niche_config.get("animation_style") or niche_config.get("anim_style") or "2d_illustrated").strip().lower()
    niche_type = (niche_config.get("niche_type") or "white_char").strip().lower()
    script_type = (niche_config.get("script_type") or "wealth_story").strip().lower()

    anim_label = ANIMATION_STYLE_LABELS.get(anim_style, ANIMATION_STYLE_LABELS["2d_illustrated"])
    style_desc = ANIMATION_STYLE_DESCRIPTIONS.get(anim_style, ANIMATION_STYLE_DESCRIPTIONS["2d_illustrated"])
    niche_mood = NICHE_VISUAL_OVERRIDES.get(niche_type, "")
    if script_type == "horror_story" and not niche_mood:
        niche_mood = NICHE_VISUAL_OVERRIDES["horror_dark"]
    elif script_type == "comedy_funny" and not niche_mood:
        niche_mood = NICHE_VISUAL_OVERRIDES["comedy_funny"]

    # Sanitize prompt_text: strip any legacy meta-headers, raw Hindi Devanagari characters, pipes, and --ar flags
    cleaned_input_prompt = re.sub(r'^Directly illustrating spoken narration:\s*"[^"]*"\.?\s*', "", prompt_text, flags=re.I)
    cleaned_input_prompt = re.sub(r'Directly illustrating spoken narration:\s*"[^"]*"\.?\s*', "", cleaned_input_prompt, flags=re.I)
    cleaned_input_prompt = re.sub(r"^STRICT LITERAL SCENE\s*\([^)]*\)\s*:\s*", "", cleaned_input_prompt, flags=re.I)
    cleaned_input_prompt = _ensure_pure_english_prompt(cleaned_input_prompt)

    mood_clause = f" {niche_mood}." if niche_mood else ""
    if niche_type == "stickman" and "stickman" not in cleaned_input_prompt[:60].lower():
        styled_prompt = (
            f"2D Stickman cartoon illustration, wide 16:9 frame: Expressive 2D stick-figure character with a clean white circular head, simple black cartoon eyes, and thin black stickman body — "
            f"{cleaned_input_prompt} "
            f"Art Style: {anim_label} — {style_desc}.{mood_clause} Clean 16:9 horizontal widescreen illustration, vibrant colors, crisp black ink outlines, no watermark."
        )
    elif "Art Style:" in cleaned_input_prompt or "MANDATORY ART STYLE:" in cleaned_input_prompt:
        styled_prompt = f"{cleaned_input_prompt} Wide 16:9 horizontal frame, high detail, no watermark."
    else:
        styled_prompt = (
            f"{anim_label}, wide 16:9 frame: {cleaned_input_prompt} "
            f"Art Style: {style_desc}.{mood_clause} "
            f"Full 16:9 horizontal widescreen composition, no text watermarks."
        )

    styled_prompt = _ensure_pure_english_prompt(styled_prompt)

    random.shuffle(keys)
    last_err = None

    low_p = styled_prompt.lower()
    is_env_only_scene = (
        "environment & object focus scene" in low_p
        or "no main character" in low_p
        or "do not just show a generic standing character" in low_p
    )
    use_char_ref = bool(
        ref_image_b64
        and not is_env_only_scene
        and niche_type in ("white_char", "stickman", "custom")
        and ("character reference" in low_p or "protagonist" in low_p or "stickman" in low_p or "custom" in low_p)
    )

    for api_key in keys:
        is_google_key = api_key.startswith("AIza") or api_key.startswith("AQ.")
        is_sk_key = api_key.startswith("sk-")
        is_replicate_key = api_key.startswith("r8_")
        is_hf_key = api_key.startswith("hf_")
        is_fw_key = api_key.startswith("fw_")

        # 1A. Replicate API Key (r8_... — Flux 16:9)
        if is_replicate_key:
            try:
                rep_payload = json.dumps({
                    "input": {
                        "prompt": styled_prompt[:2800],
                        "aspect_ratio": "16:9",
                        "output_format": "png",
                    }
                }).encode("utf-8")
                req = urllib.request.Request(
                    "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions",
                    data=rep_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {api_key}",
                        "Prefer": "wait",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=55) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    out_urls = res.get("output") or []
                    if isinstance(out_urls, str):
                        out_urls = [out_urls]
                    if out_urls:
                        img_req = urllib.request.Request(out_urls[0], headers={"User-Agent": "VideoStudio/1.0"})
                        with urllib.request.urlopen(img_req, timeout=30) as img_resp:
                            return img_resp.read(), (img_resp.headers.get("Content-Type") or "image/png")
            except Exception as e:
                last_err = f"Replicate Error: {str(e)}"
                continue

        # 1B. HuggingFace API Key (hf_... — Flux 16:9)
        if is_hf_key:
            try:
                hf_payload = json.dumps({
                    "inputs": styled_prompt[:2200],
                    "parameters": {"width": 1344, "height": 768},
                }).encode("utf-8")
                req = urllib.request.Request(
                    "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell",
                    data=hf_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {api_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=55) as resp:
                    return resp.read(), (resp.headers.get("Content-Type") or "image/jpeg")
            except Exception as e:
                last_err = f"HuggingFace Error: {str(e)}"
                continue

        # 1C. Non-Google / non-sk keys: Leonardo AI, Together AI, Fireworks AI, Fal AI, Ideogram AI
        if not is_google_key and not is_sk_key:
            try:
                return _try_leonardo_ai(styled_prompt, api_key, niche_type=niche_type, anim_style=anim_style)
            except Exception as e:
                last_err = str(e)

            # Together AI (Flux 16:9 — 1344x768)
            for flux_model in ["black-forest-labs/FLUX.1-schnell", "black-forest-labs/FLUX.1.1-pro"]:
                try:
                    tg_payload = json.dumps({
                        "model": flux_model,
                        "prompt": styled_prompt[:2800],
                        "width": 1344,
                        "height": 768,
                        "steps": 4 if "schnell" in flux_model else 24,
                        "n": 1,
                        "response_format": "b64_json",
                    }).encode("utf-8")
                    req = urllib.request.Request(
                        "https://api.together.xyz/v1/images/generations",
                        data=tg_payload,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {api_key}",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=50) as resp:
                        res = json.loads(resp.read().decode("utf-8"))
                        b64_data = res["data"][0]["b64_json"]
                        return base64.b64decode(b64_data), "image/png"
                except Exception:
                    pass

            # Fireworks AI (Flux 16:9)
            try:
                fw_payload = json.dumps({
                    "prompt": styled_prompt[:2500],
                    "aspect_ratio": "16:9",
                }).encode("utf-8")
                req = urllib.request.Request(
                    "https://api.fireworks.ai/inference/v1/workflows/accounts/fireworks/models/flux-1-schnell-fp8/text_to_image",
                    data=fw_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "image/jpeg",
                        "Authorization": f"Bearer {api_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=50) as resp:
                    return resp.read(), "image/jpeg"
            except Exception:
                pass

            # Fal AI (Flux 16:9 landscape_16_9)
            try:
                fal_payload = json.dumps({
                    "prompt": styled_prompt[:2500],
                    "image_size": "landscape_16_9",
                    "num_images": 1,
                }).encode("utf-8")
                req = urllib.request.Request(
                    "https://fal.run/fal-ai/flux/schnell",
                    data=fal_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Key {api_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=50) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    imgs = res.get("images") or []
                    if imgs and imgs[0].get("url"):
                        img_req = urllib.request.Request(imgs[0]["url"], headers={"User-Agent": "VideoStudio/1.0"})
                        with urllib.request.urlopen(img_req, timeout=30) as img_resp:
                            return img_resp.read(), (img_resp.headers.get("Content-Type") or "image/jpeg")
            except Exception:
                pass

            # Ideogram AI (ASPECT_16_9)
            try:
                id_payload = json.dumps({
                    "image_request": {
                        "prompt": styled_prompt[:2500],
                        "aspect_ratio": "ASPECT_16_9",
                        "model": "V_2_TURBO",
                    }
                }).encode("utf-8")
                req = urllib.request.Request(
                    "https://api.ideogram.ai/generate",
                    data=id_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Api-Key": api_key,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=50) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    d_list = res.get("data") or []
                    if d_list and d_list[0].get("url"):
                        img_req = urllib.request.Request(d_list[0]["url"], headers={"User-Agent": "VideoStudio/1.0"})
                        with urllib.request.urlopen(img_req, timeout=30) as img_resp:
                            return img_resp.read(), (img_resp.headers.get("Content-Type") or "image/png")
            except Exception:
                pass

        # 2. If sk-... key, support OpenAI DALL-E 3 (1792x1024 16:9) AND Stability AI SDXL (1344x768 16:9)
        if is_sk_key:
            try:
                oa_payload = json.dumps({
                    "model": "dall-e-3",
                    "prompt": styled_prompt[:3900],
                    "n": 1,
                    "size": "1792x1024",
                    "response_format": "b64_json",
                }).encode("utf-8")
                req = urllib.request.Request(
                    "https://api.openai.com/v1/images/generations",
                    data=oa_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {api_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=55) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    b64_data = res["data"][0]["b64_json"]
                    return base64.b64decode(b64_data), "image/png"
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                last_err = f"OpenAI HTTP {e.code}: {err_body[:180]}"
            except Exception as e:
                last_err = f"OpenAI Error: {str(e)}"

            # Fallback to Stability AI (which also uses sk-... keys — 1344x768 16:9)
            try:
                st_payload = json.dumps({
                    "text_prompts": [{"text": styled_prompt[:1950], "weight": 1}],
                    "cfg_scale": 7,
                    "width": 1344,
                    "height": 768,
                    "samples": 1,
                    "steps": 30,
                }).encode("utf-8")
                req = urllib.request.Request(
                    "https://api.stability.ai/v1/generation/stable-diffusion-xl-1024-v1-0/text-to-image",
                    data=st_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Authorization": f"Bearer {api_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=55) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    b64_data = res["artifacts"][0]["base64"]
                    return base64.b64decode(b64_data), "image/png"
            except Exception:
                continue

        # 3. Paid Google Gemini / Imagen API Key (Strict 16:9 + Chosen Animation Style)
        if is_google_key:
            gemini_img_models = [
                "gemini-3.1-flash-image",
                "gemini-3.1-flash-lite-image",
                "gemini-2.5-flash-image",
                "gemini-3-pro-image",
                "gemini-3.1-flash-image-preview",
                "gemini-3-pro-image-preview",
                "gemini-2.0-flash-preview-image-generation",
            ]
            for model_name in gemini_img_models:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
                user_parts = []
                if use_char_ref:
                    user_parts.append({
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": ref_image_b64,
                        }
                    })
                user_parts.append({
                    "text": styled_prompt
                })
                payload = json.dumps({
                    "contents": [{"role": "user", "parts": user_parts}],
                    "generationConfig": {
                        "responseModalities": ["IMAGE"],
                        "imageConfig": {
                            "aspectRatio": "16:9"
                        }
                    }
                }).encode("utf-8")
                try:
                    req = urllib.request.Request(
                        url,
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=55) as resp:
                        res = json.loads(resp.read().decode("utf-8"))
                        candidates = res.get("candidates") or []
                        for cand in candidates:
                            parts = (cand.get("content") or {}).get("parts") or []
                            for p in parts:
                                inl = p.get("inlineData")
                                if inl and inl.get("data"):
                                    mime = inl.get("mimeType") or "image/png"
                                    return base64.b64decode(inl["data"]), mime
                except urllib.error.HTTPError as e:
                    # Retry without imageConfig if a specific preview model rejects imageConfig
                    try:
                        fallback_payload = json.dumps({
                            "contents": [{"role": "user", "parts": user_parts}],
                            "generationConfig": {"responseModalities": ["IMAGE"]},
                        }).encode("utf-8")
                        req2 = urllib.request.Request(url, data=fallback_payload, headers={"Content-Type": "application/json"}, method="POST")
                        with urllib.request.urlopen(req2, timeout=55) as resp2:
                            res2 = json.loads(resp2.read().decode("utf-8"))
                            for cand in (res2.get("candidates") or []):
                                for p in ((cand.get("content") or {}).get("parts") or []):
                                    inl = p.get("inlineData")
                                    if inl and inl.get("data"):
                                        return base64.b64decode(inl["data"]), (inl.get("mimeType") or "image/png")
                    except Exception:
                        pass
                    err_body = e.read().decode("utf-8", errors="replace")
                    last_err = f"{model_name} HTTP {e.code}: {err_body[:180]}"
                    continue
                except Exception as e:
                    last_err = f"{model_name}: {str(e)}"
                    continue

            imagen_models = [
                "imagen-3.0-generate-002",
                "imagen-4.0-generate-001",
                "imagen-3.0-fast-generate-001",
            ]
            for im_model in imagen_models:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{im_model}:predict?key={api_key}"
                payload = json.dumps({
                    "instances": [{"prompt": styled_prompt}],
                    "parameters": {
                        "sampleCount": 1,
                        "aspectRatio": "16:9",
                    }
                }).encode("utf-8")
                try:
                    req = urllib.request.Request(
                        url,
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=55) as resp:
                        res = json.loads(resp.read().decode("utf-8"))
                        preds = res.get("predictions") or []
                        if preds and preds[0].get("bytesBase64Encoded"):
                            mime = preds[0].get("mimeType") or "image/png"
                            return base64.b64decode(preds[0]["bytesBase64Encoded"]), mime
                except urllib.error.HTTPError as e:
                    err_body = e.read().decode("utf-8", errors="replace")
                    last_err = f"{im_model} HTTP {e.code}: {err_body[:180]}"
                    continue
                except Exception as e:
                    last_err = f"{im_model}: {str(e)}"
                    continue

    raise RuntimeError(last_err or "Paid Image API Key failed or has no active credits.")


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status_code: int, payload: dict):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length)
            data = json.loads(raw_body.decode("utf-8"))

            action = data.get("action", "tts")

            # 0B. GENERATE SINGLE 16:9 SCENE IMAGE USING USER'S PAID API KEY
            if action == "generate_scene_image":
                prompt_text = (data.get("prompt") or "").strip()
                api_key_raw = (data.get("api_key") or "").strip()
                ref_image_b64 = (data.get("ref_image_b64") or "").strip()
                niche_config = data.get("niche_config") or {}
                if not prompt_text:
                    self._send_json(400, {"error": "Scene prompt is empty"})
                    return
                if not api_key_raw:
                    self._send_json(400, {"error": "Please enter your Paid Image API Key first!"})
                    return
                img_bytes, mime_type = generate_scene_image(prompt_text, api_key_raw, ref_image_b64, niche_config)
                self.send_response(200)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Length", str(len(img_bytes)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(img_bytes)
                return

            # 0A. GENERATE 20-MINUTE STORY SCRIPT (PART 1, 2, OR 3)
            if action == "generate_script_part":
                topic = (data.get("topic") or "").strip()
                if not topic:
                    self._send_json(400, {"error": "Please enter a video topic first!"})
                    return
                part_num = int(data.get("part_num", 1))
                mode = data.get("mode", "paid")
                char_name = (data.get("char_name") or "").strip()
                prev_tail = (data.get("prev_tail") or "").strip()
                niche_config = data.get("niche_config") or {}
                result = generate_script_part(topic, part_num, mode, char_name, prev_tail, niche_config)
                self._send_json(200, result)
                return

            # 0C. GENERATE 10 VIRAL TOPICS FOR KEYWORDS + OPTIONAL YOUTUBE CHANNEL/VIDEO REFERENCE
            if action == "generate_viral_topics":
                category = (data.get("category") or "").strip()
                keywords = (data.get("keywords") or "").strip()
                youtube_ref = (data.get("youtube_ref") or "").strip()
                res = generate_viral_topics_list(category, keywords, youtube_ref)
                self._send_json(200, res)
                return

            # 0C-BIND. 1-CLICK PC IP <-> STUDIO GMAIL AUTO-CAPTURE & EXTENSION SYNC
            if action == "unbind_pc_email":
                client_ip = _extract_client_ip(self.headers)
                ip_slug = _safe_user_storage_slug(client_ip or "default")
                _save_single_cloud_json("pc_binds", f"pc_ip_{ip_slug}", {
                    "email": "",
                    "latest_email": "",
                    "connected_emails": [],
                    "client_ip": client_ip,
                    "updated_at": time.time(),
                    "last_ext_ping": time.time(),
                })
                self._send_json(200, {"ok": True, "bound_email": "", "connected_emails": []})
                return

            if action == "bind_pc_email":
                client_ip = _extract_client_ip(self.headers)
                user_email = (data.get("email") or data.get("user_key") or "").strip().lower()
                device_token = (data.get("device_token") or "").strip()
                if not user_email:
                    self._send_json(400, {"error": "Studio user email is required"})
                    return
                save_pc_bound_email(client_ip, user_email, device_token=device_token)
                bound_doc = load_pc_bound_email(client_ip, mark_ext_ping=False)
                last_ping = float(bound_doc.get("last_ext_ping") or 0.0)
                ext_online = (time.time() - last_ping) < 45.0 if last_ping > 0 else False
                self._send_json(200, {
                    "ok": True,
                    "bound_email": user_email,
                    "connected_emails": bound_doc.get("connected_emails") or [user_email],
                    "client_ip": client_ip,
                    "extension_online": ext_online,
                    "last_ext_ping": last_ping,
                })
                return

            if action == "get_pc_bound_email":
                client_ip = _extract_client_ip(self.headers)
                is_ext = bool(data.get("from_extension"))
                bound_doc = load_pc_bound_email(client_ip, mark_ext_ping=is_ext)
                last_ping = float(bound_doc.get("last_ext_ping") or 0.0)
                ext_online = (time.time() - last_ping) < 45.0 if last_ping > 0 else False
                self._send_json(200, {
                    "ok": True,
                    "bound_email": (bound_doc.get("email") or "").strip().lower(),
                    "connected_emails": bound_doc.get("connected_emails") or [],
                    "client_ip": client_ip,
                    "extension_online": ext_online,
                    "last_ext_ping": last_ping,
                })
                return

            # 0D. CHATGPT AUTO-SCRIPT GENERATOR QUEUE (START, POLL, COMPLETE, STOP — ISOLATED BY STUDIO GMAIL + SAME-PC IP)
            if action == "chatgpt_queue_start":
                client_ip = _extract_client_ip(self.headers)
                user_email = (data.get("email") or data.get("user_key") or "").strip().lower()
                if not user_email:
                    self._send_json(400, {"error": "Studio user email is required"})
                    return
                save_pc_bound_email(client_ip, user_email)
                job_id = str(data.get("job_id") or f"cgpt_{int(time.time() * 1000)}")
                topic = (data.get("topic") or "").strip()
                youtube_ref = (data.get("youtube_ref") or "").strip()
                duration_mins = int(data.get("duration_mins") or 10)
                chatgpt_prompt = (data.get("chatgpt_prompt") or data.get("prompt") or "").strip()
                yt_research = fetch_youtube_api_viral_research(query_keywords=topic, channel_or_video_ref=youtube_ref)
                if yt_research:
                    chatgpt_prompt += f"\n\n=== LIVE YOUTUBE DATA API v3 VIRAL RESEARCH & REFERENCE CHANNEL ANALYSIS ===\n{yt_research}\nModel the script's hook, pacing, and high-retention storytelling directly on what works best in these viral YouTube videos!"
                job_doc = {
                    "job_id": job_id,
                    "email": user_email,
                    "owner_email": user_email,
                    "client_ip": client_ip,
                    "active": True,
                    "status": "queued",
                    "topic": topic,
                    "duration_mins": duration_mins,
                    "chatgpt_prompt": chatgpt_prompt,
                    "prompt": chatgpt_prompt,
                    "script_text": "",
                    "scriptText": "",
                    "statusText": f"⏳ Sending topic \"{topic[:55]}\" to ChatGPT...",
                    "started_at": time.time(),
                    "updated_at": time.time(),
                }
                save_chatgpt_job_cloud(user_email, job_doc, client_ip=client_ip)
                self._send_json(200, {"ok": True, "job_id": job_id, "enriched_prompt": chatgpt_prompt})
                return

            if action == "chatgpt_queue_poll":
                client_ip = _extract_client_ip(self.headers)
                from_ext = bool(data.get("from_extension", False))
                bound_doc = load_pc_bound_email(client_ip, mark_ext_ping=from_ext)
                bound_email = (bound_doc.get("email") or "").strip().lower()
                conn_emails = bound_doc.get("connected_emails") or ([] if not bound_email else [bound_email])
                ext_active = (time.time() - float(bound_doc.get("last_ext_ping") or 0)) < 180
                user_email = (data.get("email") or data.get("user_key") or bound_email or "").strip().lower()
                if not user_email and not client_ip:
                    self._send_json(200, {"ok": True, "bound_email": "", "connected_emails": conn_emails, "ext_recently_active": ext_active, "job": {"active": False}})
                    return
                job_doc = load_chatgpt_job_cloud(user_email, client_ip=client_ip)
                exp_job = data.get("expected_job")
                if isinstance(exp_job, dict) and exp_job.get("job_id") and (exp_job.get("prompt") or exp_job.get("chatgpt_prompt")):
                    if not isinstance(job_doc, dict) or job_doc.get("job_id") != exp_job.get("job_id"):
                        pr_txt = str(exp_job.get("chatgpt_prompt") or exp_job.get("prompt") or "").strip()
                        tp_txt = str(exp_job.get("topic") or "").strip()
                        job_doc = {
                            "job_id": str(exp_job.get("job_id")),
                            "email": user_email,
                            "owner_email": user_email,
                            "client_ip": client_ip,
                            "active": True,
                            "status": "queued",
                            "topic": tp_txt,
                            "duration_mins": int(exp_job.get("duration_mins") or 10),
                            "chatgpt_prompt": pr_txt,
                            "prompt": pr_txt,
                            "script_text": "",
                            "scriptText": "",
                            "statusText": f"⏳ Sending topic \"{tp_txt[:55]}\" to ChatGPT...",
                            "started_at": time.time(),
                            "updated_at": time.time(),
                        }
                        save_chatgpt_job_cloud(user_email, job_doc, client_ip=client_ip)
                self._send_json(200, {"ok": True, "bound_email": bound_email or user_email, "connected_emails": conn_emails, "ext_recently_active": ext_active, "job": job_doc})
                return

            if action == "chatgpt_queue_complete":
                client_ip = _extract_client_ip(self.headers)
                user_email = (data.get("email") or data.get("user_key") or "").strip().lower()
                if not user_email and not client_ip:
                    self._send_json(400, {"error": "Email required"})
                    return
                job_doc = load_chatgpt_job_cloud(user_email, client_ip=client_ip)
                if not job_doc or not isinstance(job_doc, dict):
                    job_doc = {"email": user_email, "owner_email": user_email, "client_ip": client_ip}
                if data.get("job_id"):
                    job_doc["job_id"] = str(data.get("job_id"))
                script_out = (data.get("script_text") or data.get("scriptText") or "").strip()
                status_val = (data.get("status") or "completed").strip()
                if script_out or status_val == "completed":
                    job_doc["script_text"] = script_out
                    job_doc["scriptText"] = script_out
                job_doc["status"] = status_val
                job_doc["active"] = (status_val in ("queued", "running"))
                if data.get("statusText"):
                    job_doc["statusText"] = str(data["statusText"])
                owner_em = (job_doc.get("owner_email") or job_doc.get("email") or user_email or "user").strip().lower()
                save_chatgpt_job_cloud(owner_em, job_doc, client_ip=client_ip)
                self._send_json(200, {"ok": True})
                return

            if action in ("chatgpt_queue_stop", "chatgpt_queue_clean"):
                client_ip = _extract_client_ip(self.headers)
                user_email = (data.get("email") or data.get("user_key") or "").strip().lower()
                if user_email or client_ip:
                    job_doc = load_chatgpt_job_cloud(user_email, client_ip=client_ip)
                    if job_doc:
                        job_doc["active"] = False
                        if action == "chatgpt_queue_clean" or bool(data.get("clean", False)):
                            job_doc["status"] = "cleaned"
                            job_doc["chatgpt_prompt"] = ""
                            job_doc["prompt"] = ""
                            job_doc["script_text"] = ""
                            job_doc["scriptText"] = ""
                            job_doc["statusText"] = "✨ ChatGPT Script Bot Cleaned & Ready!"
                        else:
                            job_doc["status"] = "stopped"
                        owner_em = (job_doc.get("owner_email") or job_doc.get("email") or user_email or "").strip().lower()
                        save_chatgpt_job_cloud(owner_em, job_doc, client_ip=client_ip)
                self._send_json(200, {"ok": True})
                return

            # 0. GENERATE STORYBOARD & GEMINI IMAGE PROMPTS
            if action == "generate_prompts":
                script_text = (data.get("script") or "").strip()
                pre_beats = data.get("beats")
                if not script_text and not pre_beats:
                    self._send_json(400, {"error": "Please paste your story script first!"})
                    return
                niche_config = data.get("niche_config") or {}
                start_image_index = int(data.get("start_image_index", 1) or 1)
                result = generate_full_prompts_document(
                    script_text,
                    niche_config,
                    start_image_index=start_image_index,
                    pre_beats=pre_beats if isinstance(pre_beats, list) else None,
                )
                self._send_json(200, result)
                return

            # 1. USER REGISTRATION & GOOGLE AUTH SYNC WITH SUPABASE (WITH ADMIN APPROVAL GATE)
            if action == "register_user":
                raw_name = (data.get("name") or "").strip()
                email = (data.get("email") or "").strip().lower()
                avatar_url = (data.get("avatar_url") or "").strip()
                supabase_uid = (data.get("supabase_uid") or "").strip()
                is_fresh_login = bool(data.get("is_fresh_login", False))
                norm_key = email if email else " ".join(raw_name.lower().split())
                date_str = data.get("date_str") or time.strftime("%Y-%m-%d %H:%M")
                niche_label = (data.get("niche_label") or "").strip()

                if not norm_key:
                    self._send_json(400, {"error": "Invalid user session."})
                    return

                reg = load_registry()
                users = reg["users"]
                deleted_list = reg.setdefault("deleted", [])
                is_admin = (email in ALL_ADMIN_EMAILS)
                admin_role = "owner" if email == ADMIN_EMAIL else ("co_admin" if email == CO_ADMIN_EMAIL else "user")

                now_ts = time.time()
                if is_admin:
                    if norm_key in deleted_list:
                        deleted_list.remove(norm_key)
                    if norm_key not in users:
                        users[norm_key] = {
                            "name": raw_name or "Admin",
                            "norm_name": norm_key,
                            "email": email,
                            "avatar_url": avatar_url,
                            "supabase_uid": supabase_uid,
                            "status": "approved",
                            "status_updated_at": now_ts,
                            "niche_label": niche_label or "Not Selected Yet",
                            "joined_at": date_str,
                            "last_active": date_str,
                            "scripts_count": 0,
                            "videos_count": 0,
                            "voices_count": 0,
                            "reg_v6": True,
                        }
                    else:
                        u = users[norm_key]
                        if raw_name:
                            u["name"] = raw_name
                        if email:
                            u["email"] = email
                        if avatar_url:
                            u["avatar_url"] = avatar_url
                        if supabase_uid:
                            u["supabase_uid"] = supabase_uid
                        u["status"] = "approved"
                        u["status_updated_at"] = now_ts
                        u["last_active"] = date_str
                        u["reg_v6"] = True
                        if niche_label and not niche_label.startswith("⚠️"):
                            u["niche_label"] = niche_label
                else:
                    if norm_key in deleted_list:
                        if is_fresh_login:
                            deleted_list.remove(norm_key)
                        else:
                            self._send_json(200, {"ok": True, "status": "removed", "is_admin": False, "admin_role": "user"})
                            return

                    if norm_key not in users:
                        users[norm_key] = {
                            "name": raw_name or (email.split("@")[0] if email else "Google User"),
                            "norm_name": norm_key,
                            "email": email,
                            "avatar_url": avatar_url,
                            "supabase_uid": supabase_uid,
                            "status": "approved",
                            "status_updated_at": now_ts,
                            "niche_label": niche_label or "Not Selected Yet",
                            "joined_at": date_str,
                            "last_active": date_str,
                            "scripts_count": 0,
                            "videos_count": 0,
                            "voices_count": 0,
                            "reg_v6": True,
                        }
                    else:
                        u = users[norm_key]
                        cur_status = u.get("status", "pending")
                        if cur_status in ("rejected", "removed"):
                            if is_fresh_login:
                                u["status"] = "pending"
                                u["status_updated_at"] = now_ts
                                u["last_active"] = date_str
                        else:
                            u["status"] = cur_status
                            u["last_active"] = date_str
                            if not u.get("status_updated_at"):
                                u["status_updated_at"] = now_ts

                        if raw_name:
                            u["name"] = raw_name
                        if email:
                            u["email"] = email
                        if avatar_url:
                            u["avatar_url"] = avatar_url
                        if supabase_uid:
                            u["supabase_uid"] = supabase_uid
                        u["reg_v6"] = True
                        if niche_label and not niche_label.startswith("⚠️"):
                            u["niche_label"] = niche_label

                save_registry(reg)
                user_obj = users[norm_key]
                self._send_json(200, {
                    "ok": True,
                    "user": user_obj,
                    "status": user_obj.get("status", "pending"),
                    "is_admin": is_admin,
                    "admin_role": admin_role,
                })
                return

            # 1B. CHECK USER APPROVAL STATUS (POLLING FOR PENDING & APPROVED USERS)
            if action == "check_user_status":
                raw_name = (data.get("name") or "").strip()
                email = (data.get("email") or "").strip().lower()
                norm_key = email if email else " ".join(raw_name.lower().split())
                if email in ALL_ADMIN_EMAILS:
                    self._send_json(200, {
                        "ok": True,
                        "status": "approved",
                        "is_admin": True,
                        "admin_role": "owner" if email == ADMIN_EMAIL else "co_admin",
                    })
                    return
                reg = load_registry()
                users = reg["users"]
                deleted_list = reg.get("deleted", [])
                found_key = _find_registry_user_key(users, norm_key)
                if found_key and found_key in deleted_list:
                    self._send_json(200, {"ok": True, "status": "removed", "is_admin": False, "admin_role": "user"})
                    return
                if not found_key:
                    self._send_json(200, {"ok": True, "status": "approved", "is_admin": False, "admin_role": "user"})
                    return
                u = users[found_key]
                self._send_json(200, {
                    "ok": True,
                    "status": u.get("status", "pending"),
                    "user": u,
                    "is_admin": False,
                    "admin_role": "user",
                })
                return

            # 2. SYNC / LOG USER ACTIVITY TO SUPABASE (ONLY FOR APPROVED USERS)
            if action == "sync_activity":
                raw_name = (data.get("name") or "").strip()
                email = (data.get("email") or "").strip().lower()
                avatar_url = (data.get("avatar_url") or "").strip()
                supabase_uid = (data.get("supabase_uid") or "").strip()
                norm_key = email if email else " ".join(raw_name.lower().split())
                niche_label = (data.get("niche_label") or "").strip()
                if norm_key:
                    reg = load_registry()
                    users = reg["users"]
                    found_key = _find_registry_user_key(users, norm_key)
                    if found_key and found_key in users:
                        u = users[found_key]
                        if email not in ALL_ADMIN_EMAILS and u.get("status") != "approved":
                            self._send_json(200, {"ok": True, "status": u.get("status", "pending")})
                            return
                        if raw_name:
                            u["name"] = raw_name
                        if email:
                            u["email"] = email
                        if avatar_url:
                            u["avatar_url"] = avatar_url
                        if supabase_uid:
                            u["supabase_uid"] = supabase_uid
                        u["last_active"] = data.get("date_str") or u.get("last_active", "")
                        u["reg_v6"] = True
                        if niche_label and not niche_label.startswith("⚠️"):
                            u["niche_label"] = niche_label
                        if data.get("inc_script"):
                            u["scripts_count"] = int(u.get("scripts_count", 0)) + 1

                        if data.get("inc_video"):
                            u["videos_count"] = int(u.get("videos_count", 0)) + 1

                        if data.get("inc_voice"):
                            u["voices_count"] = int(u.get("voices_count", 0)) + 1
                        save_registry(reg)
                self._send_json(200, {"ok": True})
                return

            # 2B. AI VISION MULTIMODAL SCENE + SPOKEN WORD ALIGNMENT
            if action == "ai_vision_sync_scenes":
                scenes_list = data.get("scenes") if isinstance(data.get("scenes"), list) else []
                words_list = data.get("words") if isinstance(data.get("words"), list) else []
                try:
                    alignments = ai_vision_sync_scenes_batch(scenes_list, words_list)
                except Exception:
                    alignments = []
                self._send_json(200, {"ok": True, "alignments": alignments})
                return

            # 3. ADMIN PANEL DATA (SUPABASE CONNECTED — OWNER ADMIN + APPROVAL-ONLY CO-ADMIN)
            if action == "admin_data":
                requester_email = (data.get("requester_email") or "").strip().lower()
                is_admin = (requester_email in ALL_ADMIN_EMAILS)
                if not is_admin:
                    self._send_json(403, {"error": "Unauthorized: Admin access is restricted."})
                    return
                reg = load_registry()
                visible_users, pending_count = _build_admin_visible_users_payload(reg, requester_email)
                self._send_json(200, {
                    "ok": True,
                    "users": visible_users,
                    "pending_count": pending_count,
                    "admin_role": "owner" if requester_email == ADMIN_EMAIL else "co_admin",
                    "gemini_keys_count": len(GEMINI_API_KEYS),
                    "supabase_connected": True,
                    "supabase_project": SUPABASE_URL,
                })
                return

            # 4A. ADMIN APPROVE USER (TICK ✅)
            if action == "approve_user":
                requester_email = (data.get("requester_email") or "").strip().lower()
                if requester_email not in ALL_ADMIN_EMAILS:
                    self._send_json(403, {"error": "Unauthorized"})
                    return
                target = (data.get("target_name") or "").strip().lower()
                reg = load_registry()
                found_key = _find_registry_user_key(reg["users"], target)
                if target in reg.get("deleted", []):
                    reg["deleted"].remove(target)
                if found_key and found_key in reg.get("deleted", []):
                    reg["deleted"].remove(found_key)
                if found_key and found_key in reg["users"]:
                    reg["users"][found_key]["status"] = "approved"
                    reg["users"][found_key]["status_updated_at"] = time.time()
                    reg["users"][found_key]["last_active"] = data.get("date_str") or reg["users"][found_key].get("last_active", "")
                save_registry(reg)
                visible_users, pending_count = _build_admin_visible_users_payload(reg, requester_email)
                self._send_json(200, {
                    "ok": True,
                    "status": "approved",
                    "users": visible_users,
                    "pending_count": pending_count,
                })
                return

            # 4B. ADMIN REJECT USER (CROSS ❌)
            if action == "reject_user":
                requester_email = (data.get("requester_email") or "").strip().lower()
                if requester_email not in ALL_ADMIN_EMAILS:
                    self._send_json(403, {"error": "Unauthorized"})
                    return
                target = (data.get("target_name") or "").strip().lower()
                reg = load_registry()
                found_key = _find_registry_user_key(reg["users"], target)
                if found_key and found_key in reg["users"]:
                    reg["users"][found_key]["status"] = "rejected"
                    reg["users"][found_key]["status_updated_at"] = time.time()
                save_registry(reg)
                visible_users, pending_count = _build_admin_visible_users_payload(reg, requester_email)
                self._send_json(200, {
                    "ok": True,
                    "status": "rejected",
                    "users": visible_users,
                    "pending_count": pending_count,
                })
                return

            # 4C. ADMIN REMOVE / KICK APPROVED USER (REQUIRES RE-APPROVAL ON NEXT LOGIN)
            if action == "delete_user":
                requester_email = (data.get("requester_email") or "").strip().lower()
                is_admin = (requester_email in ALL_ADMIN_EMAILS)
                if not is_admin:
                    self._send_json(403, {"error": "Unauthorized"})
                    return
                target = (data.get("target_name") or "").strip().lower()
                reg = load_registry()
                found_key = _find_registry_user_key(reg["users"], target)
                if found_key and found_key in reg["users"]:
                    reg["users"][found_key]["status"] = "removed"
                    reg["users"][found_key]["status_updated_at"] = time.time()
                save_registry(reg)
                visible_users, pending_count = _build_admin_visible_users_payload(reg, requester_email)
                self._send_json(200, {
                    "ok": True,
                    "status": "removed",
                    "users": visible_users,
                    "pending_count": pending_count,
                })
                return

            # 4D. CREATE SIGNED SUPABASE MEDIA UPLOAD URL (FOR DIRECT BROWSER VIDEO/AUDIO UPLOAD)
            if action == "get_media_upload_url":
                user_email = (data.get("email") or "").strip().lower()
                user_name = (data.get("name") or "").strip().lower()
                user_key = user_email or user_name or "user"
                project_id = str(data.get("project_id") or int(time.time() * 1000))
                media_type = str(data.get("media_type") or "video")
                ext = str(data.get("ext") or "mp4")
                res = create_signed_media_upload(user_key, project_id, media_type, ext)
                self._send_json(200, res)
                return

            # 4E. SYNC USER DASHBOARD, ACTIVE WORKSPACE & CREATED VIDEOS HISTORY TO CLOUD
            if action == "sync_user_dashboard":
                user_email = (data.get("email") or "").strip().lower()
                user_name = (data.get("name") or "").strip().lower()
                dash_payload = data.get("dashboard") or {}
                if not isinstance(dash_payload, dict):
                    dash_payload = {}

                reg = load_registry()
                found_key = _find_registry_user_key(reg["users"], user_email or user_name)
                primary_key = found_key or user_email or user_name or "user"

                # Preserve any cloud-uploaded video_url / voiceover_url for matching item IDs while respecting clean resets
                existing_dash = load_user_dashboard_cloud(primary_key) or {}
                existing_hist = existing_dash.get("history_items") if isinstance(existing_dash.get("history_items"), list) else []
                existing_by_id = {str(h.get("id")): h for h in existing_hist if isinstance(h, dict) and h.get("id")}

                inc_hist = dash_payload.get("history_items") if isinstance(dash_payload.get("history_items"), list) else []
                merged_hist = []
                for item in inc_hist:
                    if not isinstance(item, dict):
                        continue
                    iid = str(item.get("id") or "")
                    prev = existing_by_id.get(iid)
                    if prev:
                        for k_keep in ("video_url", "voiceover_url", "script", "generatedScriptText", "promptsText", "promptsBadge"):
                            if not item.get(k_keep) and prev.get(k_keep):
                                item[k_keep] = prev[k_keep]
                        if (
                            prev.get("script")
                            and item.get("script")
                            and str(item.get("script"))[:140].strip() == str(prev.get("script"))[:140].strip()
                            and (
                                len(str(prev.get("script"))) > len(str(item.get("script"))) + 100
                                or int(prev.get("scenesCount") or prev.get("sceneCount") or 0) > int(item.get("scenesCount") or item.get("sceneCount") or 0)
                            )
                        ):
                            for k_longer in (
                                "script",
                                "generatedScriptText",
                                "promptsText",
                                "promptsBadge",
                                "voiceover_url",
                                "voiceoverFileName",
                                "voiceoverDuration",
                                "scenesCount",
                                "sceneCount",
                                "durationMins",
                                "durationSec",
                            ):
                                if prev.get(k_longer):
                                    item[k_longer] = prev[k_longer]
                    merged_hist.append(item)

                dash_payload["history_items"] = merged_hist

                existing_ws = existing_dash.get("active_workspace") if isinstance(existing_dash.get("active_workspace"), dict) else {}
                inc_ws = dash_payload.get("active_workspace") if isinstance(dash_payload.get("active_workspace"), dict) else {}
                if existing_ws and inc_ws.get("script"):
                    same_script = bool(
                        existing_ws.get("script")
                        and str(inc_ws.get("script"))[:160].strip() == str(existing_ws.get("script"))[:160].strip()
                    )
                    if same_script:
                        for k_ws in ("voiceover_url", "video_url", "generatedScriptText", "promptsText", "promptsBadge"):
                            if not inc_ws.get(k_ws) and existing_ws.get(k_ws):
                                inc_ws[k_ws] = existing_ws[k_ws]
                        if (
                            len(str(existing_ws.get("script") or "")) > len(str(inc_ws.get("script") or "")) + 100
                            or int(existing_ws.get("scenesCount") or 0) > int(inc_ws.get("scenesCount") or 0)
                        ):
                            for k_longer in (
                                "script",
                                "promptScript",
                                "generatedScriptText",
                                "promptsText",
                                "promptsBadge",
                                "voiceover_url",
                                "voiceoverFileName",
                                "voiceoverDuration",
                                "scenesCount",
                            ):
                                if existing_ws.get(k_longer):
                                    inc_ws[k_longer] = existing_ws[k_longer]
                    dash_payload["active_workspace"] = inc_ws

                dash_payload["user_email"] = user_email
                dash_payload["user_name"] = data.get("name") or user_name
                save_user_dashboard_cloud(primary_key, dash_payload)
                if user_email and user_email != primary_key:
                    save_user_dashboard_cloud(user_email, dash_payload)
                if user_name and user_name != primary_key:
                    save_user_dashboard_cloud(user_name, dash_payload)

                if found_key and found_key in reg["users"]:
                    hist_items = dash_payload.get("history_items") or []
                    if isinstance(hist_items, list):
                        reg["users"][found_key]["cloud_projects_count"] = len(hist_items)
                        videos_with_url = [h for h in hist_items if isinstance(h, dict) and h.get("video_url")]
                        reg["users"][found_key]["cloud_videos_count"] = len(videos_with_url)
                        reg["users"][found_key]["latest_video_url"] = (videos_with_url[0].get("video_url") or "") if videos_with_url else ""
                    reg["users"][found_key]["last_dashboard_sync"] = time.time()
                    save_registry(reg)

                self._send_json(200, {"ok": True, "synced_key": primary_key, "dashboard": dash_payload})
                return

            # 4E-2. USER FETCH OWN CLOUD DASHBOARD (RESTORES CLOUD-SAVED OR ADMIN-FIXED VIDEOS & SCRIPTS)
            if action == "get_my_cloud_dashboard":
                user_email = (data.get("email") or "").strip().lower()
                user_name = (data.get("name") or "").strip().lower()
                reg = load_registry()
                found_key = _find_registry_user_key(reg["users"], user_email or user_name)
                candidate_keys = [k for k in [found_key, user_email, user_name] if k]
                dash_data = {}
                for ck in candidate_keys:
                    loaded = load_user_dashboard_cloud(ck)
                    if loaded and (loaded.get("history_items") or loaded.get("active_workspace") or loaded.get("niche_config")):
                        dash_data = loaded
                        break
                self._send_json(200, {"ok": True, "dashboard": dash_data or {}})
                return

            # 4F. ADMIN INSPECT ANY USER'S FULL DASHBOARD, SCRIPTS, PROMPTS & CREATED VIDEOS
            if action == "admin_get_user_dashboard":
                requester_email = (data.get("requester_email") or "").strip().lower()
                if requester_email != ADMIN_EMAIL:
                    self._send_json(403, {"error": "Unauthorized"})
                    return
                target = (data.get("target_name") or "").strip().lower()
                reg = load_registry()
                found_key = _find_registry_user_key(reg["users"], target)
                user_rec = reg["users"].get(found_key, {}) if found_key else {}

                candidate_keys = []
                if found_key:
                    candidate_keys.append(found_key)
                if user_rec.get("email"):
                    candidate_keys.append(str(user_rec.get("email")).strip().lower())
                if user_rec.get("norm_name"):
                    candidate_keys.append(str(user_rec.get("norm_name")).strip().lower())
                if target and target not in candidate_keys:
                    candidate_keys.append(target)

                dash_data = {}
                for ck in candidate_keys:
                    loaded = load_user_dashboard_cloud(ck)
                    if loaded and (loaded.get("history_items") or loaded.get("active_workspace") or loaded.get("niche_config")):
                        dash_data = loaded
                        break
                if not dash_data:
                    dash_data = load_user_dashboard_cloud(found_key or target)

                self._send_json(200, {
                    "ok": True,
                    "user": user_rec or {"name": target, "norm_name": target, "email": target},
                    "dashboard": dash_data,
                })
                return

            # 4E. CROSS-CHROME & MULTI-USER ISOLATED GEMINI BOT QUEUE (START, POLL, UPDATE, STOP — ISOLATED BY STUDIO GMAIL + SAME-PC IP)
            if action == "bot_queue_start":
                client_ip = _extract_client_ip(self.headers)
                user_email = (data.get("email") or data.get("user_key") or "").strip().lower()
                if not user_email:
                    self._send_json(400, {"error": "Studio user email is required"})
                    return
                save_pc_bound_email(client_ip, user_email)
                prompts_list = data.get("prompts") if isinstance(data.get("prompts"), list) else []
                folder_name = (data.get("folderName") or "VideoStudio_Scenes").strip()
                file_name_format = (data.get("fileNameFormat") or "1").strip()
                attach_ref = bool(data.get("attachRefImage", False))
                ref_data_url = (data.get("refImageDataUrl") or "").strip()
                ref_img_url = (data.get("refImageUrl") or "").strip()
                job_id = str(data.get("job_id") or f"job_{int(time.time() * 1000)}")

                # If user uploaded their own custom Reference Image, upload it to Supabase Media so 2nd Chrome can fetch it!
                if attach_ref and ref_data_url:
                    uploaded_ref = upload_bot_scene_image_cloud(
                        user_email=user_email,
                        job_id=f"{job_id}_ref",
                        scene_num=0,
                        data_url_or_b64=ref_data_url,
                    )
                    if uploaded_ref:
                        ref_img_url = uploaded_ref
                if not ref_img_url:
                    ref_img_url = "https://cartoon-video-studio.vercel.app/REFERENCE_CHARACTER_1.png"
                attach_ref = True

                preserved_gen_images = {}
                preserved_saved_nums = set()
                if isinstance(data.get("savedSceneNums"), list):
                    for sn in data.get("savedSceneNums"):
                        try:
                            if int(sn) > 0:
                                preserved_saved_nums.add(int(sn))
                        except Exception:
                            pass
                if bool(data.get("preserveGeneratedImages", False)):
                    prev_job = load_bot_job_cloud(user_email, client_ip=client_ip)
                    if isinstance(prev_job, dict):
                        if isinstance(prev_job.get("generated_images"), dict):
                            preserved_gen_images = dict(prev_job["generated_images"])
                        if isinstance(prev_job.get("savedSceneNums"), list):
                            for sn in prev_job.get("savedSceneNums"):
                                try:
                                    if int(sn) > 0:
                                        preserved_saved_nums.add(int(sn))
                                except Exception:
                                    pass
                        if attach_ref and not ref_img_url and prev_job.get("refImageUrl"):
                            ref_img_url = str(prev_job.get("refImageUrl") or "").strip()

                grand_total = int(data.get("grandTotal") or len(prompts_list) or 1)
                job_doc = {
                    "job_id": job_id,
                    "email": user_email,
                    "owner_email": user_email,
                    "client_ip": client_ip,
                    "active": True,
                    "status": "queued",
                    "statusText": f"🚀 Waiting for Gemini Browser Extension ({user_email}) to pick up {len(prompts_list)} prompts...",
                    "folderName": folder_name,
                    "fileNameFormat": file_name_format,
                    "prompts": prompts_list,
                    "attachRefImage": True,
                    "refImageUrl": ref_img_url,
                    "currentIndex": 0,
                    "total": len(prompts_list),
                    "grandTotal": grand_total,
                    "savedSceneNums": sorted(preserved_saved_nums),
                    "generated_images": preserved_gen_images,
                    "started_at": time.time(),
                    "updated_at": time.time(),
                }
                save_bot_job_cloud(user_email, job_doc, client_ip=client_ip)
                self._send_json(200, {"ok": True, "job_id": job_id, "total": len(prompts_list), "grandTotal": grand_total})
                return

            if action == "bot_queue_sync_prompts":
                client_ip = _extract_client_ip(self.headers)
                user_email = (data.get("email") or data.get("user_key") or "").strip().lower()
                if not user_email:
                    self._send_json(200, {"ok": False})
                    return
                save_pc_bound_email(client_ip, user_email)
                job_doc = load_bot_job_cloud(user_email, client_ip=client_ip) or {
                    "job_id": f"sync_{int(time.time() * 1000)}",
                    "email": user_email,
                    "owner_email": user_email,
                    "client_ip": client_ip,
                    "active": False,
                    "status": "idle",
                    "generated_images": {},
                }
                inc_prompts = data.get("prompts") if isinstance(data.get("prompts"), list) else []
                if inc_prompts:
                    job_doc["allPrompts"] = inc_prompts
                    if not job_doc.get("prompts") or len(inc_prompts) > len(job_doc.get("prompts") or []):
                        job_doc["prompts"] = inc_prompts
                if data.get("folderName"):
                    job_doc["folderName"] = str(data.get("folderName")).strip()
                if data.get("fileNameFormat"):
                    job_doc["fileNameFormat"] = str(data.get("fileNameFormat")).strip()
                if data.get("grandTotal"):
                    job_doc["grandTotal"] = int(data.get("grandTotal"))
                merged_saved = set()
                for src_list in (job_doc.get("savedSceneNums"), data.get("savedSceneNums")):
                    if isinstance(src_list, list):
                        for sn in src_list:
                            try:
                                if int(sn) > 0:
                                    merged_saved.add(int(sn))
                            except Exception:
                                pass
                if merged_saved:
                    job_doc["savedSceneNums"] = sorted(merged_saved)
                job_doc["updated_at"] = time.time()
                save_bot_job_cloud(user_email, job_doc, client_ip=client_ip)
                self._send_json(200, {"ok": True, "savedSceneNums": job_doc.get("savedSceneNums", [])})
                return

            if action == "bot_queue_poll":
                client_ip = _extract_client_ip(self.headers)
                origin_hdr = str(self.headers.get("Origin") or self.headers.get("Referer") or "").lower()
                is_gemini_ext = "gemini.google.com" in origin_hdr or "chrome-extension://" in origin_hdr or bool(data.get("from_extension"))
                bound_doc = load_pc_bound_email(client_ip, mark_ext_ping=True)
                bound_email = (bound_doc.get("email") or "").strip().lower()
                conn_emails = bound_doc.get("connected_emails") or ([] if not bound_email else [bound_email])
                user_email = (data.get("email") or data.get("user_key") or bound_email or "").strip().lower()
                if not user_email and not client_ip:
                    self._send_json(200, {"ok": True, "bound_email": "", "connected_emails": conn_emails, "job": {"active": False}})
                    return
                job_doc = load_bot_job_cloud(user_email, client_ip=client_ip)
                exp_bot = data.get("expected_bot_job")
                if isinstance(exp_bot, dict) and exp_bot.get("job_id") and isinstance(exp_bot.get("prompts"), list) and len(exp_bot.get("prompts")) > 0:
                    if not isinstance(job_doc, dict) or job_doc.get("job_id") != exp_bot.get("job_id"):
                        p_list = exp_bot.get("prompts") or []
                        g_tot = int(exp_bot.get("grandTotal") or len(p_list) or 1)
                        job_doc = {
                            "job_id": str(exp_bot.get("job_id")),
                            "email": user_email,
                            "owner_email": user_email,
                            "client_ip": client_ip,
                            "active": True,
                            "status": "queued",
                            "statusText": f"🚀 Waiting for Gemini Browser Extension ({user_email}) to pick up {len(p_list)} prompts...",
                            "folderName": str(exp_bot.get("folderName") or "VideoStudio_Scenes").strip(),
                            "fileNameFormat": str(exp_bot.get("fileNameFormat") or "1").strip(),
                            "prompts": p_list,
                            "attachRefImage": True,
                            "refImageUrl": str(exp_bot.get("refImageUrl") or "https://cartoon-video-studio.vercel.app/REFERENCE_CHARACTER_1.png"),
                            "currentIndex": 0,
                            "total": len(p_list),
                            "grandTotal": g_tot,
                            "savedSceneNums": [],
                            "generated_images": {},
                            "started_at": time.time(),
                            "updated_at": time.time(),
                        }
                        save_bot_job_cloud(user_email, job_doc, client_ip=client_ip)
                if is_gemini_ext and (not isinstance(job_doc, dict) or not job_doc.get("active")):
                    alt_active = find_active_bot_job_for_ip(client_ip)
                    if isinstance(alt_active, dict) and alt_active.get("active"):
                        job_doc = dict(alt_active)
                        if user_email:
                            job_doc["email"] = user_email
                if isinstance(job_doc, dict) and not job_doc.get("active") and job_doc.get("status") in ("stopped", "cleaned") and not job_doc.get("cleaned_v5"):
                    job_doc["prompts"] = []
                    job_doc["allPrompts"] = []
                    job_doc["savedSceneNums"] = []
                    job_doc["generated_images"] = {}
                    job_doc["total"] = 0
                    job_doc["grandTotal"] = 0
                    job_doc["currentIndex"] = 0
                    job_doc["status"] = "cleaned"
                    job_doc["statusText"] = "🟢 Ready! Video Studio se Auto-Generate start karein."
                    job_doc["cleaned_v5"] = True
                    owner_em = (job_doc.get("owner_email") or job_doc.get("email") or user_email or "").strip().lower()
                    save_bot_job_cloud(owner_em, job_doc, client_ip=client_ip)
                self._send_json(200, {"ok": True, "bound_email": bound_email or user_email, "connected_emails": conn_emails, "job": job_doc})
                return

            if action == "bot_queue_update":
                client_ip = _extract_client_ip(self.headers)
                user_email = (data.get("email") or data.get("user_key") or "").strip().lower()
                if not user_email and not client_ip:
                    self._send_json(400, {"error": "Studio user email is required"})
                    return
                job_doc = load_bot_job_cloud(user_email, client_ip=client_ip)
                if not isinstance(job_doc, dict) or not job_doc.get("active"):
                    alt_active = find_active_bot_job_for_ip(client_ip)
                    if isinstance(alt_active, dict) and alt_active.get("active"):
                        job_doc = alt_active
                        user_email = str(alt_active.get("owner_email") or alt_active.get("email") or user_email).strip().lower()
                if not job_doc:
                    time.sleep(0.15)
                    job_doc = load_bot_job_cloud(user_email, client_ip=client_ip)
                if not job_doc:
                    job_doc = {
                        "email": user_email,
                        "owner_email": user_email,
                        "client_ip": client_ip,
                        "active": bool(data.get("active", True)),
                        "prompts": [],
                        "generated_images": {},
                        "savedSceneNums": [],
                    }

                if "currentIndex" in data and data["currentIndex"] is not None:
                    job_doc["currentIndex"] = int(data["currentIndex"])
                if "status" in data and data["status"]:
                    job_doc["status"] = str(data["status"])
                if "statusText" in data and data["statusText"]:
                    job_doc["statusText"] = str(data["statusText"])
                if "folderName" in data and data["folderName"]:
                    job_doc["folderName"] = str(data["folderName"]).strip()
                if "grandTotal" in data and data["grandTotal"]:
                    job_doc["grandTotal"] = int(data["grandTotal"])
                if "active" in data and data["active"] is not None:
                    job_doc["active"] = bool(data["active"])
                elif str(job_doc.get("status") or "") == "running":
                    job_doc["active"] = True

                merged_saved = set()
                for src_list in (job_doc.get("savedSceneNums"), data.get("savedSceneNums")):
                    if isinstance(src_list, list):
                        for sn in src_list:
                            try:
                                if int(sn) > 0:
                                    merged_saved.add(int(sn))
                            except Exception:
                                pass
                scene_num = data.get("sceneNum")
                if scene_num is not None:
                    try:
                        if int(scene_num) > 0:
                            merged_saved.add(int(scene_num))
                    except Exception:
                        pass
                if merged_saved:
                    job_doc["savedSceneNums"] = sorted(merged_saved)
                    job_doc["savedCount"] = len(job_doc["savedSceneNums"])

                image_data_url = data.get("dataUrl") or ""
                owner_em = (job_doc.get("owner_email") or job_doc.get("email") or user_email or "user").strip().lower()
                if scene_num is not None and image_data_url:
                    pub_url = upload_bot_scene_image_cloud(
                        user_email=owner_em,
                        job_id=job_doc.get("job_id") or "job",
                        scene_num=int(scene_num),
                        data_url_or_b64=image_data_url,
                    )
                    if pub_url:
                        latest_doc = load_bot_job_cloud(owner_em, client_ip=client_ip)
                        if isinstance(latest_doc, dict) and latest_doc.get("prompts"):
                            merged_gen = dict(latest_doc.get("generated_images") or {})
                            merged_gen.update(job_doc.get("generated_images") or {})
                            job_doc["prompts"] = latest_doc["prompts"]
                            job_doc["generated_images"] = merged_gen
                        gen_map = job_doc.setdefault("generated_images", {})
                        gen_map[str(int(scene_num))] = pub_url

                job_doc["updated_at"] = time.time()
                save_bot_job_cloud(owner_em, job_doc, client_ip=client_ip)
                self._send_json(200, {
                    "ok": True,
                    "generated_images": job_doc.get("generated_images", {}),
                    "savedSceneNums": job_doc.get("savedSceneNums", []),
                })
                return

            if action in ("bot_queue_stop", "bot_queue_clean"):
                client_ip = _extract_client_ip(self.headers)
                user_email = (data.get("email") or data.get("user_key") or "").strip().lower()
                if user_email or client_ip:
                    job_doc = load_bot_job_cloud(user_email, client_ip=client_ip)
                    if job_doc:
                        job_doc["active"] = False
                        if action == "bot_queue_clean" or bool(data.get("clean", False)):
                            job_doc["status"] = "cleaned"
                            job_doc["statusText"] = "✨ Bot Cleaned & Reset (0 / 0)! Ready for new video."
                            job_doc["prompts"] = []
                            job_doc["allPrompts"] = []
                            job_doc["savedSceneNums"] = []
                            job_doc["generated_images"] = {}
                            job_doc["total"] = 0
                            job_doc["grandTotal"] = 0
                            job_doc["currentIndex"] = 0
                            job_doc["cleaned_v5"] = True
                        else:
                            job_doc["status"] = "stopped"
                            job_doc["statusText"] = "🛑 Auto-Image Bot stopped by user."
                        owner_em = (job_doc.get("owner_email") or job_doc.get("email") or user_email or "").strip().lower()
                        save_bot_job_cloud(owner_em, job_doc, client_ip=client_ip)
                self._send_json(200, {"ok": True})
                return

            if action == "transcribe_audio_slice":
                audio_b64 = (data.get("audio_b64") or "").strip()
                mime_type = (data.get("mime_type") or "audio/wav").strip()
                if not audio_b64:
                    self._send_json(400, {"error": "audio_b64 is required"})
                    return
                if "," in audio_b64 and audio_b64.startswith("data:"):
                    audio_b64 = audio_b64.split(",", 1)[1]

                transcribed_text = ""
                last_t_err = None
                for api_key in _get_rotated_gemini_keys():
                    for m_name in ["gemini-3.1-flash-lite-preview", "gemini-3-flash-preview", "gemini-flash-lite-latest"]:
                        t_url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent?key={api_key}"
                        t_payload = json.dumps({
                            "contents": [{
                                "role": "user",
                                "parts": [
                                    {"inlineData": {"mimeType": mime_type, "data": audio_b64}},
                                    {"text": "Transcribe the exact spoken words from this audio verbatim with proper punctuation. Output ONLY the exact spoken transcript text, nothing else."}
                                ]
                            }]
                        }).encode("utf-8")
                        try:
                            t_req = urllib.request.Request(
                                t_url,
                                data=t_payload,
                                headers={"Content-Type": "application/json"},
                                method="POST",
                            )
                            with urllib.request.urlopen(t_req, timeout=35) as t_resp:
                                t_res = json.loads(t_resp.read().decode("utf-8"))
                                cand_txt = (
                                    t_res.get("candidates", [{}])[0]
                                    .get("content", {})
                                    .get("parts", [{}])[0]
                                    .get("text", "")
                                ).strip()
                                if cand_txt:
                                    transcribed_text = cand_txt
                                    break
                        except Exception as te:
                            last_t_err = str(te)
                            continue
                    if transcribed_text:
                        break

                self._send_json(200, {"ok": True, "text": transcribed_text, "error": last_t_err if not transcribed_text else ""})
                return


            # 5. DEFAULT: GENERATE UNIFIED DARK-NATURAL EXPRESSIVE TTS AUDIO (MALE HALE OR FEMALE ELENA, WITH DYNAMIC EXPRESSIONS)
            text = (data.get("text") or "").strip()
            language = (data.get("language") or "english").strip().lower()
            voice = (data.get("voice") or "Achird").strip()
            niche_config = data.get("niche_config") or {}

            if not text:
                self._send_json(400, {"error": "Text is empty"})
                return

            audio_bytes = generate_gemini_tts_wav(text, language=language, voice=voice, niche_config=niche_config)

            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(audio_bytes)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(audio_bytes)

        except Exception as e:
            self._send_json(500, {"error": str(e)})
