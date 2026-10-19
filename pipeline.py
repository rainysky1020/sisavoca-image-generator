"""OpenAI planning, image generation, and image review for one vocabulary row."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import struct
import time
import unicodedata
import urllib.error
import urllib.request
from collections import deque
from io import BytesIO
from pathlib import Path
from typing import Callable

from PIL import Image, UnidentifiedImageError


API_BASE = "https://api.openai.com/v1"
TEXT_MODEL = "gpt-6-sol"
IMAGE_MODEL = "gpt-image-2"
IMAGE_SIZE = "1024x1024"
DELIVERY_SIZE = (800, 800)
STYLE_GUIDE = """Image 1, '다람쥐 캐릭터 시트2.jpg', is the publisher's authoritative NEW style
sheet. Its upper row shows the SAME squirrel from front, side and back; its lower two rows
are expression studies of that same face. They are views, NOT multiple characters to draw.
Copy this specific visual grammar even when the scene contains no squirrel:
- Pure opaque #FFFFFF background and open white margins. No floor, colored panel, frame,
  cast shadow, cream wash, or transparent background.
- Large soft rounded silhouettes, sparse interior detail, smooth medium-width dark warm-brown
  outlines (approximately #5C472A), rounded stroke ends and corners, and flat opaque fills.
  Use roughly consistent outline weight on the squirrel and props. No black outer outline,
  gradients, texture, broad shading, 3D perspective or realistic lighting.
- Reference palette: light golden-tan body about #E7BD83, pale cream inner ears about
  #F3E1B3, medium brown tail and oval feet about #B88C5F, dark brown markings and tail curl
  about #7F613B, soft coral-pink oval cheeks about #FF9F98, black eyes/nose/mouth and white
  eye whites. These are guides from the JPEG; match the visible sheet over exact hex codes.
  Reserve tiny cyan tears or red anger/blush marks for an expression that specifically needs
  them, as shown on the sheet. Do not recolor the squirrel or add decorative bright colors.
- Squirrel anatomy: oversized sideways-up rounded ears with cream inserts; broad low rounded
  head and compact pear/bean-shaped body with no long neck; three short vertical dark forehead
  stripes; widely spaced big round white eyes with black pupils; pink horizontal oval cheeks;
  tiny black triangular nose and simple curved mouth; one thick medium-brown curled tail with
  a single dark inner spiral. In profile, keep the same cheek, muzzle and tail; from behind,
  keep the three head stripes and single curled tail. No clothes, whiskers, hair, extra markings,
  or anatomy borrowed from a different squirrel design.
- The sheet's arms are ONLY short curved strokes on the upper body, without drawn separate
  hands or articulated wrists. Feet are two small, single-piece brown ovals. If an action needs
  contact with a prop, let the simple rounded arm/body overlap or touch it. NEVER draw a
  gripping hand, fingers, toes, claws, nails, digit gaps, notches, or separating lines.
- When the meaning involves pointing, indicating or showing a direction, NEVER pose the
  squirrel as pointing with an index finger, an extended hand, a narrow tapered arm, or
  a human-like gesture. Show the target with the squirrel's gaze and body orientation,
  a simple freestanding arrow or motion mark, or the position of the target object.
  Any visible arm stays a short, blunt, rounded curve attached to the body with no
  separate hand or tip. This rule applies even when the word itself means 'point'.
- Keep the visible arms complete and attached to the body: no missing, floating, doubled or
  protruding arm strokes. In a side view one arm can be visible; in a back view follow the
  sheet. Keep exactly one complete, connected mouth in a plausible position below the nose.
  No missing mouth, extra lips, duplicate mouth, stray black stroke or line extending past
  the face. Use only the sheet's simple mouth vocabulary.
- For emotion, use the sheet's face vocabulary: neutral round eyes, smiling crescent eyes,
  squeezed excited eyes, round surprised mouth, sparkling teary eyes, angled angry brows,
  or shy cheek marks. Change the face and simple pose, not the character's anatomy or palette.
- Keep eye size and pupil-to-white balance close to the sheet. Eyes are a supporting face
  feature, NEVER the main giant focal point. No bulging, glowing, realistic, hollow or
  isolated eyes, oversized pupils, extreme close-up stare, or horror-like expression.
Use one clear mnemonic idea at mobile size. Convey the Korean meaning DIRECTLY with one
recognizable object, visual indicator, action or restrained sheet-style expression. A viewer
should understand the action or relation without a caption. Keep a current 2026 mobile
illustration feeling through crisp flat shapes, confident size contrast, lively asymmetry,
clear visual hierarchy and generous white space. Make it gender-neutral and broadly inviting:
no frills, florals, lace, hearts, sparkles, cosmetic accents, powdery pastel wash or overly
sweet feminine coding. Avoid sepia, antique paper, vintage storybook, watercolor, rustic
craft, old textbook clip art and decorative clutter. Modernity must not restyle the squirrel.
If no actor is needed, draw only
objects/symbols with the SAME round brown-outlined flat visual grammar and palette; do not
invent another character or put faces on objects.
"""


class APIError(RuntimeError):
    pass


def load_api_key(env_path: Path) -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if line.lstrip().startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == "OPENAI_API_KEY":
                return value.strip().strip('"\'')
    raise APIError("OPENAI_API_KEY가 .env에 없습니다.")


class OpenAIClient:
    def __init__(self, key: str):
        self.key = key

    def _request(self, endpoint: str, body: bytes, content_type: str) -> dict:
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": content_type}
        last_error = None
        for attempt in range(3):
            request = urllib.request.Request(API_BASE + endpoint, body, headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=240) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                payload = exc.read(4096).decode("utf-8", "replace")
                try:
                    message = json.loads(payload).get("error", {}).get("message", payload)
                except json.JSONDecodeError:
                    message = payload
                last_error = APIError(f"OpenAI HTTP {exc.code}: {message[:350]}")
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise last_error from None
                retry_after = exc.headers.get("Retry-After", "")
                wait = min(float(retry_after), 20) if retry_after.replace(".", "", 1).isdigit() else 2 ** attempt
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = APIError(f"OpenAI 연결 오류: {exc.reason if hasattr(exc, 'reason') else exc}")
                if attempt == 2:
                    raise last_error from None
                time.sleep(2 ** attempt)
        raise last_error or APIError("OpenAI 요청 실패")

    def response(self, prompt: str, schema_name: str, schema: dict,
                 images: list[tuple[str, bytes]] | None = None) -> dict:
        content = [{"type": "input_text", "text": prompt}]
        for filename, image in images or []:
            mime = mimetypes.guess_type(filename)[0] or "image/png"
            content.append({"type": "input_image", "image_url":
                            f"data:{mime};base64," + base64.b64encode(image).decode("ascii")})
        body = {
            "model": TEXT_MODEL,
            "input": [{"role": "user", "content": content}],
            "text": {"format": {"type": "json_schema", "name": schema_name,
                                "strict": True, "schema": schema}},
        }
        return self._request("/responses", json.dumps(body).encode(), "application/json")

    def edit_image(self, prompt: str, images: list[tuple[str, bytes]]) -> dict:
        refs = []
        for filename, data in images:
            mime = mimetypes.guess_type(filename)[0] or "image/png"
            refs.append({"image_url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii")})
        body = {"model": IMAGE_MODEL, "prompt": prompt, "images": refs, "quality": "low",
                "size": IMAGE_SIZE, "output_format": "png", "background": "opaque", "n": 1}
        return self._request("/images/edits", json.dumps(body).encode(), "application/json")


PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"draw": {"type": "boolean"}, "reason": {"type": "string"},
                   "scene": {"type": "string"}, "memory_cue": {"type": "string"},
                   "use_squirrel": {"type": "boolean"}, "use_text": {"type": "boolean"},
                   "allowed_text": {"type": "string"},
                   "visual_signal": {"type": "string"},
                   "object_keys": {"type": "array", "items": {"type": "string"}}},
    "required": ["draw", "reason", "scene", "memory_cue", "use_squirrel", "use_text",
                 "allowed_text", "visual_signal", "object_keys"],
}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"decision": {"type": "string", "enum": ["keep", "revise"]},
                   "issues": {"type": "string"}, "fix_instruction": {"type": "string"},
                   "digit_issue": {"type": "string", "enum": ["none", "suspected", "visible"]},
                   "character_mismatch": {"type": "boolean"},
                   "extra_character": {"type": "boolean"},
                   "limb_issue": {"type": "boolean"},
                   "mouth_issue": {"type": "boolean"},
                   "eye_fear_issue": {"type": "boolean"},
                   "object_consistency_issue": {"type": "boolean"},
                   "meaning_cue_issue": {"type": "boolean"},
                   "style_issue": {"type": "string", "enum": ["none", "minor", "material"]}},
    "required": ["decision", "issues", "fix_instruction", "digit_issue", "character_mismatch",
                 "extra_character", "limb_issue", "mouth_issue", "eye_fear_issue",
                 "object_consistency_issue", "meaning_cue_issue", "style_issue"],
}
SELECTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"chosen_image": {"type": "string", "enum": ["original", "revised"]},
                   "reason": {"type": "string"}, "remaining_issues": {"type": "string"},
                   "original_digit_issue": {"type": "string", "enum": ["none", "suspected", "visible"]},
                   "revised_digit_issue": {"type": "string", "enum": ["none", "suspected", "visible"]}},
    "required": ["chosen_image", "reason", "remaining_issues",
                 "original_digit_issue", "revised_digit_issue"],
}


def _blocking_issues(review: dict) -> str:
    """Enforce character rules even if the reviewer otherwise chooses to keep."""
    problems = []
    if review["digit_issue"] != "none":
        problems.append("다람쥐 손·발에 손가락·발가락처럼 분리된 형태가 보이거나 의심됨")
    if review["character_mismatch"]:
        problems.append("다람쥐 캐릭터 시트와 핵심 형태가 다름")
    if review["extra_character"]:
        problems.append("다람쥐 외에 불필요한 얼굴 있는 캐릭터가 추가됨")
    if review["limb_issue"]:
        problems.append("다람쥐 팔이 빠지거나 중복·분리·돌출됨")
    if review["mouth_issue"]:
        problems.append("다람쥐 입이 빠지거나 중복·분리·돌출됨")
    if review["eye_fear_issue"]:
        problems.append("눈의 크기·시선이 과장되어 공포감을 줌")
    if review["object_consistency_issue"]:
        problems.append("기존 삽화의 동일 사물 디자인과 크게 다름")
    if review["meaning_cue_issue"]:
        problems.append("뜻을 직관적으로 보여주는 표시·동작·표정이 불분명함")
    if review["style_issue"] == "material":
        problems.append("입체 명암·그라데이션 또는 복고풍이 화풍을 크게 벗어남")
    return "; ".join(problems)


def _text(result: dict) -> dict:
    if result.get("status") == "incomplete":
        raise APIError("판단·검토 응답이 완성되지 않았습니다.")
    if result.get("output_text"):
        return json.loads(result["output_text"])
    for item in result.get("output", []):
        for part in item.get("content", []):
            if part.get("type") == "refusal":
                raise APIError("모델이 요청을 거부했습니다.")
            if part.get("type") == "output_text":
                return json.loads(part["text"])
    raise APIError("판단·검토 결과를 읽을 수 없습니다.")


def _image(result: dict) -> bytes:
    data = result.get("data", [])
    if not data or not data[0].get("b64_json"):
        raise APIError("이미지 응답에 PNG 데이터가 없습니다.")
    image = base64.b64decode(data[0]["b64_json"], validate=True)
    if len(image) < 24 or image[:8] != b"\x89PNG\r\n\x1a\n":
        raise APIError("이미지 응답이 PNG가 아닙니다.")
    width, height = struct.unpack(">II", image[16:24])
    if (width, height) != (1024, 1024):
        raise APIError(f"API 이미지 크기가 1024×1024가 아닙니다: {width}×{height}")
    try:
        with Image.open(BytesIO(image)) as source:
            source.load()
            if source.format != "PNG":
                raise APIError("이미지 응답이 PNG가 아닙니다.")
            return _make_delivery_png(source.convert("RGB"))
    except (OSError, UnidentifiedImageError) as exc:
        raise APIError("PNG 픽셀 데이터를 읽을 수 없습니다.") from exc


def _make_delivery_png(source: Image.Image) -> bytes:
    """Resize and make only the edge-connected near-white background exact white."""
    delivered = source.convert("RGB").resize(DELIVERY_SIZE, Image.Resampling.LANCZOS)
    width, height = delivered.size
    rgb = bytearray(delivered.tobytes())
    seen = bytearray(width * height)
    queue = deque()

    def add(index: int) -> None:
        if seen[index]:
            return
        seen[index] = 1
        offset = index * 3
        if min(rgb[offset:offset + 3]) >= 245:
            rgb[offset:offset + 3] = b"\xff\xff\xff"
            queue.append(index)

    for x in range(width):
        add(x)
        add((height - 1) * width + x)
    for y in range(height):
        add(y * width)
        add(y * width + width - 1)
    while queue:
        index = queue.popleft()
        x, y = index % width, index // width
        if x:
            add(index - 1)
        if x + 1 < width:
            add(index + 1)
        if y:
            add(index - width)
        if y + 1 < height:
            add(index + width)
    delivered = Image.frombytes("RGB", DELIVERY_SIZE, bytes(rgb))
    if not any(delivered.getpixel(point) == (255, 255, 255) for point in
               ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))):
        raise APIError("순백색 배경을 확인할 수 없어 PNG를 납품하지 않습니다.")
    output = BytesIO()
    delivered.save(output, format="PNG")
    return output.getvalue()


def _usage(result: dict) -> dict[str, int]:
    usage = result.get("usage") or {}
    detail = usage.get("input_tokens_details") or {}
    return {"api_calls": 1, "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "image_tokens": int(detail.get("image_tokens") or 0)}


def _add_usage(item: dict, result: dict) -> None:
    for key, value in _usage(result).items():
        item[key] = item.get(key, 0) + value


def _context(item: dict) -> str:
    return (f"Original-language word: {item['word']}\nKorean target meaning: "
            f"{item['meaning_ko']}\nIdentify the source language from the word if possible. "
            "Do not rely on an assumed language or an unrelated sense.")


def normalize_object_keys(values: object) -> list[str]:
    """Keep a small, stable set of canonical Korean nouns for reference matching."""
    if not isinstance(values, list):
        return []
    keys = []
    for value in values:
        if not isinstance(value, str):
            continue
        key = unicodedata.normalize("NFC", value).strip().casefold()
        if 1 <= len(key) <= 40 and key not in keys:
            keys.append(key)
        if len(keys) == 3:
            break
    return keys


def image_filename(word: str, output_dir: Path) -> str:
    """Preserve the original word in a portable filename and avoid overwriting duplicates."""
    stem = unicodedata.normalize("NFC", word).strip()
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).rstrip(" .")[:120].rstrip(" .")
    if not stem:
        stem = "word"
    if stem.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                         *(f"LPT{i}" for i in range(1, 10))}:
        stem = "_" + stem
    existing = {path.name.casefold() for path in output_dir.glob("*.png")}
    filename = f"{stem}.png"
    suffix = 2
    while filename.casefold() in existing:
        filename = f"{stem} ({suffix}).png"
        suffix += 1
    return filename


def process_item(item: dict, client: OpenAIClient, sheet_path: Path, output_dir: Path,
                 save: Callable[[dict], None],
                 find_object_references: Callable[[list[str]], list[tuple[str, str, bytes]]] | None = None,
                 known_object_keys: list[str] | None = None) -> dict:
    """Process one row; save is called after every irreversible API result."""
    reference = sheet_path.read_bytes()
    item["status"] = "planning"
    save(item)
    prompt = ("You are an expert vocabulary editor. BEFORE any image is made, design one "
              "concrete scene and decide whether it can help recall the specified Korean meaning. "
              "Skip when no faithful, memorable visual scene can be designed, or when any simple "
              "scene would be generic, misleading, or merely decorative. Never infer another sense. "
              "If you choose to draw, the app will deliver one generated candidate after review, "
              "so make the scene and mnemonic cue specific enough at this planning stage. "
              "Use a squirrel only when a visible actor, action, expression, or emotion is needed; "
              "otherwise use objects or symbols with no animal. Prefer no letters or numbers; "
              "permit only if essential to the mnemonic. Specify a DIRECT visual_signal: a "
              "recognizable indicator, motion, relationship, or sheet-style facial expression "
              "that makes the Korean meaning obvious without text. If the meaning or scene involves "
              "pointing or indicating, NEVER plan an index-finger or hand-pointing pose. Instead "
              "show the target with the squirrel's gaze/body direction, a separate simple arrow, "
              "or the target object's placement; its arms remain short blunt curves. This also "
              "applies when the Korean meaning is '가리키다'. List up to three reusable "
              "physical props as object_keys using singular Korean dictionary nouns, excluding "
              "the squirrel and generic backgrounds. Reuse an existing key when it names the same "
              "object. Return a concrete simple scene.\n" + _context(item))
    if known_object_keys:
        prompt += "\nExisting object names to reuse when relevant: " + ", ".join(known_object_keys[:100])
    result = client.response(prompt, "vocabulary_image_plan", PLAN_SCHEMA)
    _add_usage(item, result)
    plan = _text(result)
    permitted_text = str(plan["allowed_text"]).strip() if plan["use_text"] else ""
    object_keys = normalize_object_keys(plan["object_keys"])
    item.update(decision_reason=plan["reason"], scene=plan["scene"],
                memory_cue=plan["memory_cue"], use_squirrel=bool(plan["use_squirrel"]),
                use_text=bool(permitted_text), visual_signal=plan["visual_signal"],
                object_keys=object_keys)
    if not plan["draw"]:
        item.update(status="skipped", final_reason=plan["reason"])
        save(item)
        return item
    item["status"] = "generating"
    save(item)
    prior_objects = find_object_references(object_keys)[:2] if find_object_references else []
    item["object_reference_files"] = [source for _, source, _ in prior_objects]
    object_images = [(f"object_ref_{index}.png", data)
                     for index, (_, _, data) in enumerate(prior_objects, 1)]
    def object_instruction(start_index: int) -> str:
        if not prior_objects:
            return ""
        return ("Earlier delivered illustrations are supplied only to match a recurring "
                "OBJECT's silhouette, palette, outline and distinguishing details. "
                "Keep the new scene and meaning; do not copy an earlier composition or "
                "character pose. " + " ".join(
                    f"Image {index + start_index} contains the established '{key}' design."
                    for index, (key, _, _) in enumerate(prior_objects)))
    character = ("Include exactly one squirrel character matching the NEW multi-view reference sheet. "
                 "Use its front/side/back views for anatomy and its face studies only for expression. "
                 "No other faces, animals, mascots, or anthropomorphic objects."
                 if plan["use_squirrel"] else "No squirrel, no animals, no anthropomorphic actor.")
    text_rule = (f"Only visible text allowed: {permitted_text}. Keep it exact and minimal."
                 if permitted_text else
                 "No letters, words, numbers, captions, speech bubbles, or watermarks.")
    image_prompt = (f"Create a standalone vocabulary illustration on an exact pure white canvas.\n"
                    f"{STYLE_GUIDE}\n"
                    f"{character}\n{text_rule}\nScene: {plan['scene']}\n"
                    f"Mnemonic cue: {plan['memory_cue']}\nDirect visual signal: "
                    f"{plan['visual_signal']}\nTarget meaning: {item['meaning_ko']}. "
                    f"{object_instruction(2)} "
                    "If the scene says the squirrel points, reinterpret it as looking toward or "
                    "turning toward the target with a separate arrow or motion cue. Do not draw "
                    "an extended pointing arm, hand, index finger, or tapered finger-like tip. "
                    "One instantly readable central idea. Simplify any room or setting to a few flat "
                    "graphic props; avoid a rendered environment. Leave clear PURE WHITE (#FFFFFF) "
                    "margins; no off-white background, floor, shadow, or frame.")
    result = client.edit_image(image_prompt, [(sheet_path.name, reference), *object_images])
    _add_usage(item, result)
    original = _image(result)
    item["status"] = "reviewing"
    save(item)
    review_prompt = ("You are selecting usable vocabulary illustrations for publication. "
                     "Favor keeping an image when its Korean meaning is recognizable at mobile size "
                     "and it serves as a useful memory cue. Assess the reference sheet's overall "
                     "specific NEW multi-view sheet: warm-brown outlines, flat golden-tan body, "
                     "cream ear inserts, single curled brown tail, coral oval cheeks, and sparse "
                     "rounded props. The bottom rows are expression studies, not extra characters. "
                     "Keep a current 2026 mobile-illustration composition with clear hierarchy, "
                     "crisp flat forms and useful asymmetry. Flag material vintage/sepia treatment "
                     "or strong frilly/floral, powder-pastel, gender-coded decoration; the mascot "
                     "itself must still match the supplied sheet. "
                     "Treat small palette differences, a tiny local gradient, minor texture, or a minor "
                     "compositional imperfection as non-blocking. "
                     "Choose 'keep' for a usable image even when you mention minor issues. "
                     "Choose 'revise' only for a material problem: wrong or misleading meaning, "
                     "illegible central idea, an unwanted squirrel, vintage/3D rendering that "
                     "materially breaks the sheet's style, unwanted prominent text, or a changed "
                     "squirrel design. Set style_issue to 'material' when central shapes show obvious "
                     "3D shading or broad gradients, the scene looks vintage, or several bright "
                     "accent colors overwhelm the reference palette; use 'minor' only for tiny "
                     "local deviations. Set extra_character when another animal, mascot, human, "
                     "or face on a toy or object appears. Inspect EVERY visible squirrel paw and "
                     "foot closely, including "
                     "ones touching props: set digit_issue to 'visible' for fingers, toes, claws, "
                     "nails, splits, notches, or digit-separating lines, 'suspected' for ambiguous "
                     "digit-like marks, and 'none' only when no digit-like mark is visible. "
                     "For any pointing/indicating scene, zoom attention to the arm end: a single "
                     "index-finger shape counts as 'visible' even without multiple finger lines; "
                     "a tapered finger-like arm end counts as 'suspected'. A long, detached, or "
                     "human-like pointing arm also sets limb_issue. Gaze, body direction, and a "
                     "separate arrow are acceptable alternatives. "
                     "A paw hidden behind an object is not by itself suspicious. Set "
                     "character_mismatch when the large ears, three forehead stripes, simple face, "
                     "compact body, curled tail, or sheet-specific arm/foot design differs materially "
                     "from the reference. The arms should be simple curved strokes with NO separate "
                     "hands; feet should be two undivided brown ovals. Do not treat normal eye and "
                     "mouth changes from the sheet's expression studies as a mismatch. A non-none "
                     "digit_issue, character_mismatch, "
                     "extra_character, or material style_issue requires "
                     "revision even if the mnemonic is otherwise useful. Set limb_issue only for "
                     "clearly missing, doubled, detached or protruding arm strokes for the pose; "
                     "one visible arm in profile or no arms visible from behind can be correct. "
                     "Set mouth_issue for a missing, duplicated, detached or overflowing mouth, "
                     "not for a normal sheet-style change of expression. Set eye_fear_issue only "
                     "when enlarged or isolated eyes, extreme pupils or a fixed stare create a "
                     "frightening emphasis beyond the reference sheet. Set meaning_cue_issue "
                     "when the planned indicator, movement or expression fails to make the Korean "
                     "meaning intuitive. Set object_consistency_issue only when a matching prior "
                     "object reference is provided and the same object visibly changes design. "
                     "These clear issues also call for one revision. The output PNG must show "
                     "the illustration on an opaque, pure white (#FFFFFF) background without a "
                     "colored panel or gradient backdrop. "
                     "Give a concise reason and, "
                     "only when revision is necessary, a specific fix.\n" +
                     _context(item) + f"\nPlanned scene: {plan['scene']}\nDirect visual signal: "
                     f"{plan['visual_signal']}\nSquirrel allowed: {plan['use_squirrel']}\n"
                     f"Visible text allowed: {text_rule} " + " ".join(
                         f"Image {index + 3} is the established '{key}' object design."
                         for index, (key, _, _) in enumerate(prior_objects)))
    selected = original
    item["selected_version"] = "원본"
    try:
        result = client.response(review_prompt + "\nImage 1 is the reference sheet; image 2 is the draft to judge.",
                                 "vocabulary_image_review", REVIEW_SCHEMA,
                                 [(sheet_path.name, reference), ("draft.png", original), *object_images])
        _add_usage(item, result)
        review = _text(result)
        blocking = _blocking_issues(review)
    except (APIError, ValueError, KeyError, TypeError):
        item.update(status="generated", review_notes="자동 검토 실패",
                    final_reason="자동 검토를 완료하지 못해 생성 원본을 납품함")
    else:
        item["review_notes"] = "; ".join(filter(None, [review["issues"], blocking]))
        if review["decision"] == "keep" and not blocking:
            item.update(status="generated", final_reason="검토 후 생성 원본 선택" +
                        (f" (참고: {review['issues']})" if review["issues"] else ""))
        else:
            item["status"] = "revising"
            save(item)
            fix_prompt = (f"Revise the second input image to fix this material issue: "
                          f"{item['review_notes'] or 'material review failure'}. "
                          f"{review['fix_instruction']} Preserve the intended scene, direct visual "
                          f"signal ({plan['visual_signal']}) and vocabulary meaning. "
                          f"The first input is the authoritative style sheet. {STYLE_GUIDE} "
                          f"Focus on the material issue instead of cosmetic restyling. If paws or feet "
                          f"are involved, redraw arms as short curved strokes with no separate hands "
                          f"and feet as two small unbroken brown ovals without digits. "
                          "Keep the exact count and attachment of visible arms and one complete "
                          "sheet-style mouth. Keep eyes in the sheet's calm proportions without "
                          "a frightening oversized stare. "
                          "If the squirrel is pointing, remove the pointing hand/finger/extended "
                          "arm entirely. Preserve the meaning with its gaze or body direction and "
                          "a separate simple arrow or target placement instead; the arm must end "
                          "as a short blunt curve attached to the torso. "
                          f"{object_instruction(3)} "
                          f"{character} {text_rule} "
                          "Pure white (#FFFFFF) opaque background with clear margins. The application "
                          "will export the final image as an 800x800 PNG.")
            item["revision_count"] = 1
            try:
                result = client.edit_image(fix_prompt, [(sheet_path.name, reference),
                                                        ("draft.png", original), *object_images])
                _add_usage(item, result)
                revised = _image(result)
            except (APIError, ValueError, KeyError, TypeError):
                item.update(status="generated", final_reason="수정본 생성에 실패해 원본을 납품함")
                item["review_notes"] += "; 수정본 생성 실패"
            else:
                item["status"] = "reviewing"
                save(item)
                selection_prompt = (
                    "Choose exactly ONE of the two completed vocabulary illustrations for delivery. "
                    "Image 1 is the authoritative squirrel style sheet; image 2 is ORIGINAL; "
                    "image 3 is REVISED. Compare images 2 and 3 for the specified Korean meaning "
                    "and direct visual signal FIRST, then current gender-neutral style, prior object "
                    "consistency, complete arms and mouth, non-frightening eye proportions, fingerless "
                    "limbs, text and visual clarity. If exactly one version has a visible or "
                    "finger-like pointing hand and the other conveys the meaning without it, "
                    "choose the fingerless version. Independently report digit_issue for BOTH "
                    "original and revised; a single index finger or tapered finger-like tip "
                    "counts even without separated finger lines. Choose the more useful image even if neither "
                    "is perfect. "
                    "Do not discard, reject, or request another revision. State the selected image, "
                    "a concise reason and any remaining issues for the editor.\n" + _context(item) +
                    f"\nPlanned scene: {plan['scene']}\nMemory cue: {plan['memory_cue']}\n"
                    f"Direct visual signal: {plan['visual_signal']}\n"
                    f"Initial review notes: {item['review_notes']} " + " ".join(
                        f"Image {index + 4} is the established '{key}' object design."
                        for index, (key, _, _) in enumerate(prior_objects)))
                try:
                    result = client.response(selection_prompt, "vocabulary_image_selection", SELECTION_SCHEMA,
                                             [(sheet_path.name, reference), ("original.png", original),
                                              ("revised.png", revised), *object_images])
                    _add_usage(item, result)
                    choice = _text(result)
                    chosen = choice["chosen_image"]
                    if chosen not in ("original", "revised"):
                        raise ValueError("비교 결과의 선택본이 올바르지 않습니다.")
                    original_digits = choice["original_digit_issue"]
                    revised_digits = choice["revised_digit_issue"]
                    if original_digits != "none" and revised_digits == "none":
                        chosen = "revised"
                    elif revised_digits != "none" and original_digits == "none":
                        chosen = "original"
                except (APIError, ValueError, KeyError, TypeError):
                    item.update(status="generated", final_reason="원본·수정본 비교에 실패해 원본을 납품함")
                    item["review_notes"] += "; 최종 비교 실패"
                else:
                    selected = revised if chosen == "revised" else original
                    item["selected_version"] = "수정본" if chosen == "revised" else "원본"
                    item["status"] = "revised" if chosen == "revised" else "generated"
                    if chosen != choice["chosen_image"]:
                        item["final_reason"] = (
                            f"원본·수정본 비교 후 {item['selected_version']} 선택: "
                            "손가락 없는 후보 우선 선택; 비교 모델 의견: " + choice["reason"])
                    else:
                        item["final_reason"] = (f"원본·수정본 비교 후 {item['selected_version']} 선택: "
                                                f"{choice['reason']}")
                    if choice["remaining_issues"]:
                        item["review_notes"] += "; 선택본 참고 사항: " + choice["remaining_issues"]
    filename = image_filename(item["word"], output_dir)
    path = output_dir / filename
    path.write_bytes(selected)
    item["image_file"] = filename
    save(item)
    return item
