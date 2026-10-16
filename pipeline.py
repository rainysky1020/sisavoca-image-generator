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
from pathlib import Path
from typing import Callable


API_BASE = "https://api.openai.com/v1"
TEXT_MODEL = "gpt-6-sol"
IMAGE_MODEL = "gpt-image-2"
IMAGE_SIZE = "1024x1024"
STYLE_GUIDE = """The attached sheet is the publisher's authoritative illustration style reference.
Use clean white background; rounded, compact, friendly silhouettes; flat opaque color fields;
EVERY shape must use one uniform solid fill, including objects and small details. No gradients
anywhere, no highlights, texture, shading, realistic lighting, thin anatomical detail, or
decorative clutter. Crisp cut-paper/vector edges; no soft airbrush transitions.
Palette: warm light tan body, medium brown accents, pale cream details, soft pink cheeks,
black facial marks; other colors only when the target concept requires them. Bold, simple,
readable composition at mobile size. If a squirrel appears, preserve the sheet's large ears,
three dark forehead stripes, rounded eyes, pink cheeks, tiny black nose/mouth, round belly,
and curled brown tail. Its hands and feet are featureless rounded shapes: absolutely no
fingers, toes, claws, nails, or digit lines. If no squirrel is required, use only the sheet's
visual grammar and never copy the squirrel or add a character.
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
                   "issues": {"type": "string"}, "fix_instruction": {"type": "string"}},
    "required": ["decision", "issues", "fix_instruction"],
}


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
        raise APIError(f"이미지 크기가 1024×1024가 아닙니다: {width}×{height}")
    return image


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
    character = ("Include exactly one squirrel character matching the reference sheet."
                 if plan["use_squirrel"] else "No squirrel, no animals, no anthropomorphic actor.")
    text_rule = (f"Only visible text allowed: {permitted_text}. Keep it exact and minimal."
                 if permitted_text else
                 "No letters, words, numbers, captions, speech bubbles, or watermarks.")
    image_prompt = (f"Create a standalone 1024x1024 vocabulary illustration.\n{STYLE_GUIDE}\n"
                    f"{character}\n{text_rule}\nScene: {plan['scene']}\n"
                    f"Mnemonic cue: {plan['memory_cue']}\nTarget meaning: {item['meaning_ko']}. "
                    "One instantly readable central idea. Solid pure white background, no frame.")
    result = client.edit_image(image_prompt, [(sheet_path.name, reference)])
    _add_usage(item, result)
    image = _image(result)
    item["status"] = "reviewing"
    save(item)
    review_prompt = ("You are selecting usable vocabulary illustrations for publication. "
                     "Favor keeping an image when its Korean meaning is recognizable at mobile size "
                     "and it serves as a useful memory cue. Assess the reference sheet's overall "
                     "friendly, rounded, flat style, but treat small palette differences, slight "
                     "gradients, minor texture or imperfect resemblance as non-blocking. "
                     "Choose 'keep' for a usable image even when you mention minor issues. "
                     "Choose 'revise' only for a material problem: wrong or misleading meaning, "
                     "illegible central idea, an unwanted squirrel, significant style mismatch, "
                     "unwanted prominent text, or squirrel fingers, toes, claws, nails, or digit lines. "
                     "Do not fail solely because you are uncertain about a tiny artistic detail. "
                     "Give a concise reason and, only when revision is necessary, a specific fix.\n" +
                     _context(item) + f"\nPlanned scene: {plan['scene']}\nSquirrel allowed: "
                     f"{plan['use_squirrel']}\nVisible text allowed: {text_rule}")
    result = client.response(review_prompt + "\nImage 1 is the reference sheet; image 2 is the draft to judge.",
                             "vocabulary_image_review", REVIEW_SCHEMA,
                             [(sheet_path.name, reference), ("draft.png", image)])
    _add_usage(item, result)
    review = _text(result)
    item["review_notes"] = review["issues"]
    if review["decision"] == "keep":
        item.update(status="generated", final_reason="사용 가능 판정" +
                    (f" (경미한 참고 사항: {review['issues']})" if review["issues"] else ""))
    else:
        item["status"] = "revising"
        save(item)
        fix_prompt = (f"Revise the second input image to fix this material issue: {review['issues']}. "
                      f"{review['fix_instruction']} Preserve the intended scene and vocabulary meaning. "
                      f"The first input is the authoritative style sheet. {STYLE_GUIDE} "
                      f"Focus on the material issue instead of cosmetic restyling. {character} {text_rule} "
                      "Pure white background, 1024x1024 PNG.")
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
        if judgment["decision"] == "keep":
            item.update(status="revised", final_reason="1회 수정 후 사용 가능 판정" +
                        (f" (경미한 참고 사항: {judgment['issues']})" if judgment["issues"] else ""))
        else:
            item.update(status="discarded", final_reason=judgment["issues"] or "최종 검토 기준 미달")
            save(item)
            return item
    filename = image_filename(item["word"], output_dir)
    path = output_dir / filename
    path.write_bytes(image)
    item["image_file"] = filename
    save(item)
    return item
