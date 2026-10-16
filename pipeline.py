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
STYLE_GUIDE = """The attached sheet is the publisher's authoritative illustration style reference.
Draw the illustration isolated on PURE WHITE (#FFFFFF). The final delivered PNG must have an
opaque, exact #FFFFFF background, not off-white, cream, gray, or transparency. No colored
backdrop, floor, cast shadow, frame, or panel. Copy the sheet's compact rounded silhouettes,
clean vector-like edges, very few shapes, and completely flat opaque color fields. Every shape,
including the squirrel and every prop, uses one uniform solid fill. NO gradients on any surface,
highlights, texture, shadows,
realistic lighting, isometric rooms, perspective-heavy 3D scenes, tiny anatomical details,
or decorative clutter.
Make the overall illustration feel current and playful through bold scale, lively asymmetrical
placement, generous white space, and one clear visual surprise that reinforces the mnemonic.
Think contemporary mobile sticker/editorial illustration, not vintage storybook, sepia,
watercolor, rustic craft, or nostalgic classroom clip art. Modernity comes from composition,
NOT from restyling the squirrel or adding fashion, accessories, gradients, or extra detail.
Keep the sheet's warm tan, medium brown, pale cream, soft pink, and black as the dominant
palette. At most one crisp contemporary accent color may appear on a non-character object
when it makes the meaning easier to remember. Do not recolor the squirrel. Do not combine several
bright accent colors in one image.
If a squirrel appears, reproduce the sheet's character design: same large rounded ears,
three dark forehead stripes, big round eyes, pink oval cheeks, tiny black nose and mouth,
simple rounded belly, curled brown tail, and compact proportions. Vary only its pose and
expression as needed. It is the only character: no additional animal, mascot, human, face on
an object, or anthropomorphic toy. Its arms end in single smooth mitten-like paws, and its feet are
single smooth oval shapes. Hands and feet must have NO fingers, toes, claws, nails, splits,
notches, or digit-separating lines, even when holding an object. A prop may touch or sit
against a solid paw; never draw a gripping hand. If no squirrel is required, use the same
flat, rounded visual grammar without copying or adding any character.
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
    prompt = ("You are an expert vocabulary editor. Decide if ONE memorable, unambiguous image "
              "will materially help recall the specified Korean meaning. Skip if illustration "
              "would be generic, misleading, or merely decorative. Never infer another sense. "
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
    character = ("Include exactly one squirrel character matching the reference sheet. "
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
    image = _image(result)
    item["status"] = "reviewing"
    save(item)
    review_prompt = ("You are selecting usable vocabulary illustrations for publication. "
                     "Favor keeping an image when its Korean meaning is recognizable at mobile size "
                     "and it serves as a useful memory cue. Assess the reference sheet's overall "
                     "friendly, rounded, flat style and current playful graphic composition. "
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
                     "character_mismatch when core features or proportions differ materially from "
                     "the reference sheet. A non-none digit_issue, character_mismatch, "
                     "extra_character, or material style_issue requires "
                     "revision even if the mnemonic is otherwise useful. The output PNG must show "
                     "the illustration on an opaque, pure white (#FFFFFF) background without a "
                     "colored panel or gradient backdrop. "
                     "Give a concise reason and, "
                     "only when revision is necessary, a specific fix.\n" +
                     _context(item) + f"\nPlanned scene: {plan['scene']}\nSquirrel allowed: "
                     f"{plan['use_squirrel']}\nVisible text allowed: {text_rule}")
    result = client.response(review_prompt + "\nImage 1 is the reference sheet; image 2 is the draft to judge.",
                             "vocabulary_image_review", REVIEW_SCHEMA,
                             [(sheet_path.name, reference), ("draft.png", image)])
    _add_usage(item, result)
    review = _text(result)
    blocking = _blocking_issues(review)
    item["review_notes"] = "; ".join(filter(None, [review["issues"], blocking]))
    if review["decision"] == "keep" and not blocking:
        item.update(status="generated", final_reason="사용 가능 판정" +
                    (f" (경미한 참고 사항: {review['issues']})" if review["issues"] else ""))
    else:
        item["status"] = "revising"
        save(item)
        fix_prompt = (f"Revise the second input image to fix this material issue: "
                      f"{item['review_notes'] or 'material review failure'}. "
                      f"{review['fix_instruction']} Preserve the intended scene and vocabulary meaning. "
                      f"The first input is the authoritative style sheet. {STYLE_GUIDE} "
                      f"Focus on the material issue instead of cosmetic restyling. If paws or feet "
                      f"are involved, redraw them as unbroken solid shapes without digits. "
                      f"{character} {text_rule} "
                      "Pure white (#FFFFFF) opaque background with clear margins. The application "
                      "will export the final image as an 800x800 PNG.")
        result = client.edit_image(fix_prompt, [(sheet_path.name, reference), ("draft.png", image)])
        _add_usage(item, result)
        item["revision_count"] = 1
        image = _image(result)
        item["status"] = "reviewing"
        save(item)
        final = client.response(review_prompt + "\nImage 1 is the reference sheet; image 2 is the single revised version to judge. No further edits are allowed.",
                                "vocabulary_image_final_review", REVIEW_SCHEMA,
                                [(sheet_path.name, reference), ("revised.png", image)])
        _add_usage(item, final)
        judgment = _text(final)
        final_blocking = _blocking_issues(judgment)
        if judgment["decision"] == "keep" and not final_blocking:
            item.update(status="revised", final_reason="1회 수정 후 사용 가능 판정" +
                        (f" (경미한 참고 사항: {judgment['issues']})" if judgment["issues"] else ""))
        else:
            item.update(status="discarded", final_reason="; ".join(filter(None, [judgment["issues"], final_blocking]))
                        or "최종 검토 기준 미달")
            save(item)
            return item
    filename = image_filename(item["word"], output_dir)
    path = output_dir / filename
    path.write_bytes(image)
    item["image_file"] = filename
    save(item)
    return item
