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
- For emotion, use the sheet's face vocabulary: neutral round eyes, smiling crescent eyes,
  squeezed excited eyes, round surprised mouth, sparkling teary eyes, angled angry brows,
  or shy cheek marks. Change the face and simple pose, not the character's anatomy or palette.
Use one clear mnemonic idea at mobile size. Keep the modern, playful contemporary mobile
sticker feeling through bold scale, lively asymmetry and generous white space, NOT through
restyling, vintage storybook texture, fashion or clutter. If no actor is needed, draw only
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
                   "allowed_text": {"type": "string"}},
    "required": ["draw", "reason", "scene", "memory_cue", "use_squirrel", "use_text", "allowed_text"],
}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"decision": {"type": "string", "enum": ["keep", "revise"]},
                   "issues": {"type": "string"}, "fix_instruction": {"type": "string"},
                   "digit_issue": {"type": "string", "enum": ["none", "suspected", "visible"]},
                   "character_mismatch": {"type": "boolean"},
                   "extra_character": {"type": "boolean"},
                   "style_issue": {"type": "string", "enum": ["none", "minor", "material"]}},
    "required": ["decision", "issues", "fix_instruction", "digit_issue", "character_mismatch",
                 "extra_character", "style_issue"],
}
SELECTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"chosen_image": {"type": "string", "enum": ["original", "revised"]},
                   "reason": {"type": "string"}, "remaining_issues": {"type": "string"}},
    "required": ["chosen_image", "reason", "remaining_issues"],
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
                 save: Callable[[dict], None]) -> dict:
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
              "permit only if essential to the mnemonic. Return a concrete simple scene.\n" + _context(item))
    result = client.response(prompt, "vocabulary_image_plan", PLAN_SCHEMA)
    _add_usage(item, result)
    plan = _text(result)
    permitted_text = str(plan["allowed_text"]).strip() if plan["use_text"] else ""
    item.update(decision_reason=plan["reason"], scene=plan["scene"],
                memory_cue=plan["memory_cue"], use_squirrel=bool(plan["use_squirrel"]),
                use_text=bool(permitted_text))
    if not plan["draw"]:
        item.update(status="skipped", final_reason=plan["reason"])
        save(item)
        return item
    item["status"] = "generating"
    save(item)
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
                    f"Mnemonic cue: {plan['memory_cue']}\nTarget meaning: {item['meaning_ko']}. "
                    "One instantly readable central idea. Simplify any room or setting to a few flat "
                    "graphic props; avoid a rendered environment. Leave clear PURE WHITE (#FFFFFF) "
                    "margins; no off-white background, floor, shadow, or frame.")
    result = client.edit_image(image_prompt, [(sheet_path.name, reference)])
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
                     "Keep its current playful graphic composition. "
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
                     "A paw hidden behind an object is not by itself suspicious. Set "
                     "character_mismatch when the large ears, three forehead stripes, simple face, "
                     "compact body, curled tail, or sheet-specific arm/foot design differs materially "
                     "from the reference. The arms should be simple curved strokes with NO separate "
                     "hands; feet should be two undivided brown ovals. Do not treat normal eye and "
                     "mouth changes from the sheet's expression studies as a mismatch. A non-none "
                     "digit_issue, character_mismatch, "
                     "extra_character, or material style_issue requires "
                     "revision even if the mnemonic is otherwise useful. The output PNG must show "
                     "the illustration on an opaque, pure white (#FFFFFF) background without a "
                     "colored panel or gradient backdrop. "
                     "Give a concise reason and, "
                     "only when revision is necessary, a specific fix.\n" +
                     _context(item) + f"\nPlanned scene: {plan['scene']}\nSquirrel allowed: "
                     f"{plan['use_squirrel']}\nVisible text allowed: {text_rule}")
    selected = original
    item["selected_version"] = "원본"
    try:
        result = client.response(review_prompt + "\nImage 1 is the reference sheet; image 2 is the draft to judge.",
                                 "vocabulary_image_review", REVIEW_SCHEMA,
                                 [(sheet_path.name, reference), ("draft.png", original)])
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
                          f"{review['fix_instruction']} Preserve the intended scene and vocabulary meaning. "
                          f"The first input is the authoritative style sheet. {STYLE_GUIDE} "
                          f"Focus on the material issue instead of cosmetic restyling. If paws or feet "
                          f"are involved, redraw arms as short curved strokes with no separate hands "
                          f"and feet as two small unbroken brown ovals without digits. "
                          f"{character} {text_rule} "
                          "Pure white (#FFFFFF) opaque background with clear margins. The application "
                          "will export the final image as an 800x800 PNG.")
            item["revision_count"] = 1
            try:
                result = client.edit_image(fix_prompt, [(sheet_path.name, reference), ("draft.png", original)])
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
                    "and memory cue FIRST, then character consistency, fingerless limbs, text and "
                    "visual clarity. Choose the more useful image even if neither is perfect. "
                    "Do not discard, reject, or request another revision. State the selected image, "
                    "a concise reason and any remaining issues for the editor.\n" + _context(item) +
                    f"\nPlanned scene: {plan['scene']}\nMemory cue: {plan['memory_cue']}\n"
                    f"Initial review notes: {item['review_notes']}")
                try:
                    result = client.response(selection_prompt, "vocabulary_image_selection", SELECTION_SCHEMA,
                                             [(sheet_path.name, reference), ("original.png", original),
                                              ("revised.png", revised)])
                    _add_usage(item, result)
                    choice = _text(result)
                    chosen = choice["chosen_image"]
                    if chosen not in ("original", "revised"):
                        raise ValueError("비교 결과의 선택본이 올바르지 않습니다.")
                except (APIError, ValueError, KeyError, TypeError):
                    item.update(status="generated", final_reason="원본·수정본 비교에 실패해 원본을 납품함")
                    item["review_notes"] += "; 최종 비교 실패"
                else:
                    selected = revised if chosen == "revised" else original
                    item["selected_version"] = "수정본" if chosen == "revised" else "원본"
                    item["status"] = "revised" if chosen == "revised" else "generated"
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
