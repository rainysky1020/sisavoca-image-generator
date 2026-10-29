import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from PIL import Image, ImageDraw

from app import parse_csv
from pipeline import APIError, OpenAIClient, _image, image_filename, process_item
from report_xlsx import write_report

TEST_TEMP = Path(__file__).resolve().parents[1] / "output" / "test_tmp"
TEST_TEMP.mkdir(parents=True, exist_ok=True)


def png(width=1024, height=1024, background=(253, 254, 252), foreground="#bf815a"):
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((width // 4, height // 4, width * 3 // 4, height * 3 // 4), fill=foreground)
    draw.ellipse((width // 2 - 30, height // 2 - 30, width // 2 + 30, height // 2 + 30), fill="white")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class FakeClient:
    def __init__(self, mode):
        self.mode = mode
        self.edits = 0
        self.reviews = 0
        self.image_prompts = []
        self.edit_images = []
        self.response_images = {}
        self.response_prompts = {}

    def response(self, prompt, schema_name, schema, images=None):
        self.response_prompts[schema_name] = prompt
        self.response_images[schema_name] = [name for name, _ in images or []]
        if schema_name == "vocabulary_image_plan":
            content = {"draw": self.mode != "skip", "reason": "연상에 도움" if self.mode != "skip" else "시각화 이득 없음",
                       "scene": "문 앞에서 망설이는 모습", "memory_cue": "멈춘 동작",
                       "use_squirrel": self.mode not in ("object", "text"),
                       "use_text": self.mode == "text", "allowed_text": "A" if self.mode == "text" else "",
                       "visual_signal": "멈춘 자세", "object_keys": ["문"]}
        elif schema_name == "vocabulary_image_selection":
            if self.mode == "selection_error":
                raise APIError("comparison unavailable")
            content = {"chosen_image": "original" if self.mode in ("choose_original", "digit_override") else "revised",
                       "reason": "뜻을 더 잘 기억할 수 있음", "remaining_issues": "손 모양 참고" if self.mode == "discard" else "",
                       "original_digit_issue": "visible" if self.mode in ("digit_override", "digit_persist") else "none",
                       "revised_digit_issue": "visible" if self.mode == "digit_persist" else "none"}
            self.selection_images = [name for name, _ in images]
        else:
            if self.mode == "review_error":
                raise APIError("review unavailable")
            self.reviews += 1
            passing = self.mode in ("pass", "minor", "object", "text", "digit_override",
                                    "digit_persist", "character_override", "style_override",
                                    "extra_character_override") or (self.mode == "fix" and self.reviews == 2)
            digit_issue = ("visible" if self.mode == "digit_persist" or
                           (self.mode == "digit_override" and self.reviews == 1) else "none")
            content = {"decision": "keep" if passing else "revise",
                       "issues": "약한 그라데이션" if self.mode == "minor" else ("" if passing else "손가락이 보임"),
                       "fix_instruction": "" if passing else "손가락을 없애라",
                       "digit_issue": digit_issue,
                       "character_mismatch": self.mode == "character_override" and self.reviews == 1,
                       "extra_character": self.mode == "extra_character_override" and self.reviews == 1,
                       "limb_issue": False, "mouth_issue": False, "eye_fear_issue": False,
                       "object_consistency_issue": False, "meaning_cue_issue": False,
                       "style_issue": "material" if self.mode == "style_override" and self.reviews == 1 else "none"}
        return {"output_text": json.dumps(content), "usage": {"input_tokens": 3, "output_tokens": 2}}

    def edit_image(self, prompt, images):
        import base64
        self.edits += 1
        self.image_prompts.append(prompt)
        self.edit_images.append([name for name, _ in images])
        if self.mode == "revision_error" and self.edits == 2:
            raise APIError("revision unavailable")
        foreground = "#643e9c" if self.edits == 2 else "#bf815a"
        return {"data": [{"b64_json": base64.b64encode(png(foreground=foreground)).decode()}],
                "usage": {"input_tokens": 4, "output_tokens": 10}}


class WorkflowTests(unittest.TestCase):
    def test_csv_validation_and_duplicate(self):
        data = "word,meaning_ko\nhesitate,망설이다\nhesitate,주저하다\n,놀라다\n".encode()
        rows = parse_csv(data)
        self.assertEqual([x["status"] for x in rows], ["queued", "queued", "error"])
        self.assertEqual(rows[1]["status"], "queued")
        self.assertIn("word 누락", rows[2]["final_reason"])
        with self.assertRaises(ValueError):
            parse_csv(b"word\napple\n")
        with self.assertRaises(ValueError):
            parse_csv(b"word,meaning_ko\n\xff,test\n")

    def run_case(self, mode):
        item = parse_csv("word,meaning_ko\nhesitate,망설이다\n".encode())[0]
        client = FakeClient(mode)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            root = Path(directory)
            sheet = root / "sheet.jpg"
            sheet.write_bytes(b"fake reference")
            result = process_item(item, client, sheet, root, lambda _: None)
            self.assertEqual(client.edits, 0 if mode == "skip" else
                             (1 if mode in ("pass", "minor", "review_error") else 2))
            self.assertEqual(result["status"], {"skip": "skipped", "pass": "generated",
                                                "minor": "generated", "fix": "revised", "discard": "revised",
                                                "digit_override": "revised", "digit_persist": "revised",
                                                "character_override": "revised", "style_override": "revised",
                                                "extra_character_override": "revised",
                                                "choose_original": "generated", "review_error": "generated",
                                                "revision_error": "generated", "selection_error": "generated"}[mode])
            self.assertEqual(len(list(root.glob("*.png"))), 0 if mode == "skip" else 1)
            if mode not in ("skip", "pass", "minor", "review_error"):
                self.assertEqual(result["revision_count"], 1)
            if mode != "skip":
                self.assertEqual(result["selected_version"],
                                 "수정본" if result["status"] == "revised" else "원본")
                with Image.open(root / result["image_file"]) as delivered:
                    expected = (100, 62, 156) if result["status"] == "revised" else (191, 129, 90)
                    self.assertEqual(delivered.getpixel((300, 300)), expected)
            if mode in ("fix", "discard", "choose_original"):
                self.assertEqual(client.selection_images, ["sheet.jpg", "original.png", "revised.png",
                                                           "샘플1.jpg", "샘플2.jpg", "샘플3.jpg"])
            return result.copy()

    def test_decisions_and_single_revision(self):
        for mode in ("skip", "pass", "minor", "fix", "discard", "digit_override",
                     "digit_persist", "character_override", "style_override",
                     "extra_character_override", "choose_original", "review_error",
                     "revision_error", "selection_error"):
            with self.subTest(mode=mode):
                self.run_case(mode)

    def test_modern_style_and_featureless_paws_in_generation_and_revision(self):
        item = parse_csv("word,meaning_ko\nhesitate,망설이다\n".encode())[0]
        client = FakeClient("digit_override")
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            root = Path(directory)
            sheet = root / "sheet.jpg"
            sheet.write_bytes(b"fake reference")
            result = process_item(item, client, sheet, root, lambda _: None)
            self.assertEqual(result["status"], "revised")
            self.assertIn("current 2026 mobile", client.image_prompts[0])
            self.assertIn("arms are ONLY short curved strokes", client.image_prompts[0])
            self.assertIn("NEVER draw a", client.image_prompts[0])
            self.assertIn("No missing mouth", client.image_prompts[0])
            self.assertIn("No bulging, glowing", client.image_prompts[0])
            self.assertIn("Direct visual signal: 멈춘 자세", client.image_prompts[0])
            self.assertIn("no separate hands", client.image_prompts[1])
            self.assertIn("two small unbroken brown ovals", client.image_prompts[1])
            self.assertIn("손가락·발가락", result["review_notes"])

    def test_pointing_uses_gaze_or_arrow_and_inspects_single_finger(self):
        item = parse_csv("word,meaning_ko\npoint,가리키다\n".encode())[0]
        client = FakeClient("digit_override")
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            root = Path(directory)
            sheet = root / "sheet.jpg"
            sheet.write_bytes(b"fake reference")
            result = process_item(item, client, sheet, root, lambda _: None)
            self.assertEqual(result["status"], "revised")
            self.assertIn("NEVER plan an index-finger", client.response_prompts["vocabulary_image_plan"])
            self.assertIn("reinterpret it as looking toward", client.image_prompts[0])
            self.assertIn("single index-finger shape counts as 'visible'",
                          client.response_prompts["vocabulary_image_review"])
            self.assertIn("remove the pointing hand/finger/extended arm", client.image_prompts[1])
            self.assertIn("choose the fingerless version",
                          client.response_prompts["vocabulary_image_selection"])
            self.assertIn("손가락 없는 후보 우선 선택", result["final_reason"])

    def test_sheet3_and_new_visual_rules_reach_every_stage(self):
        import app
        self.assertEqual(app.SHEET.name, "다람쥐 캐릭터 시트3.jpg")
        item = parse_csv("word,meaning_ko\ndrop,떨어뜨리다\n".encode())[0]
        client = FakeClient("fix")
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            root = Path(directory)
            sheet = root / "다람쥐 캐릭터 시트3.jpg"
            sheet.write_bytes(b"fake reference")
            process_item(item, client, sheet, root, lambda _: None)
            plan = client.response_prompts["vocabulary_image_plan"]
            self.assertIn("emphasize the falling object over the actor", plan)
            self.assertIn("simple visual comparison", plan)
            self.assertIn("Never place the Korean target meaning", plan)
            for image_prompt in client.image_prompts:
                self.assertIn("falling", image_prompt)
                self.assertIn("speech bubbles", image_prompt)
                self.assertIn("warm", image_prompt)
            review = client.response_prompts["vocabulary_image_review"]
            selection = client.response_prompts["vocabulary_image_selection"]
            self.assertIn("falling object must be the focal point", review)
            self.assertIn("Korean target meaning", review)
            self.assertIn("falling object", selection)
            self.assertIn("Korean-meaning speech bubble", selection)

    def test_object_and_text_rules(self):
        for mode, expected in (("object", "No squirrel"), ("text", "Only visible text allowed: A")):
            item = parse_csv("word,meaning_ko\napple,사과\n".encode())[0]
            client = FakeClient(mode)
            with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
                root = Path(directory)
                sheet = root / "sheet.jpg"
                sheet.write_bytes(b"fake reference")
                process_item(item, client, sheet, root, lambda _: None)
                self.assertIn(expected, client.image_prompts[0])

    def test_reuses_prior_object_in_generation_revision_and_review(self):
        item = parse_csv("word,meaning_ko\nhesitate,망설이다\n".encode())[0]
        client = FakeClient("digit_override")
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            root = Path(directory)
            sheet = root / "sheet.jpg"
            sheet.write_bytes(b"fake reference")
            lookups = []
            def find_refs(keys):
                lookups.append(keys)
                return [("문", "older-job/door.png", png())]
            result = process_item(item, client, sheet, root, lambda _: None,
                                  find_refs, ["문"])
            self.assertEqual(lookups, [["문"]])
            self.assertEqual(result["object_reference_files"], ["older-job/door.png"])
            self.assertEqual(client.edit_images[0], ["object_ref_1.png", "sheet.jpg",
                                                      "샘플1.jpg", "샘플2.jpg", "샘플3.jpg"])
            self.assertEqual(client.edit_images[1], ["draft.png", "object_ref_1.png", "sheet.jpg",
                                                      "샘플1.jpg", "샘플2.jpg", "샘플3.jpg"])
            self.assertEqual(client.response_images["vocabulary_image_review"],
                             ["sheet.jpg", "draft.png", "object_ref_1.png",
                              "샘플1.jpg", "샘플2.jpg", "샘플3.jpg"])
            self.assertEqual(client.response_images["vocabulary_image_selection"],
                             ["sheet.jpg", "original.png", "revised.png", "object_ref_1.png",
                              "샘플1.jpg", "샘플2.jpg", "샘플3.jpg"])
            self.assertIn("Image 1 contains the established '문' design", client.image_prompts[0])
            self.assertIn("Image 2 contains the established '문' design", client.image_prompts[1])
            self.assertIn("Image 2, '다람쥐 캐릭터 시트3.jpg'", client.image_prompts[0])
            self.assertIn("Image 3, '다람쥐 캐릭터 시트3.jpg'", client.image_prompts[1])
            self.assertIn("Do not recolor a recurring object", client.image_prompts[0])
            self.assertIn("changed clock rim color", client.response_prompts["vocabulary_image_review"])

    def test_object_only_color_change_forces_revision_and_consistent_selection(self):
        import base64

        class RecoloredClockClient(FakeClient):
            def __init__(self):
                super().__init__("object")

            def response(self, prompt, schema_name, schema, images=None):
                result = super().response(prompt, schema_name, schema, images)
                data = json.loads(result["output_text"])
                if schema_name == "vocabulary_image_plan":
                    data["object_keys"] = ["시계"]
                if schema_name == "vocabulary_image_selection":
                    data["chosen_image"] = "original"
                result["output_text"] = json.dumps(data)
                return result

            def edit_image(self, prompt, images):
                self.edits += 1
                self.image_prompts.append(prompt)
                self.edit_images.append([name for name, _ in images])
                color = "#f08020" if self.edits == 1 else "#bf815a"
                return {"data": [{"b64_json": base64.b64encode(png(foreground=color)).decode()}]}

        item = parse_csv("word,meaning_ko\nclock,시계\n".encode())[0]
        client = RecoloredClockClient()
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            root = Path(directory)
            sheet = root / "sheet.jpg"
            sheet.write_bytes(b"fake reference")
            result = process_item(item, client, sheet, root, lambda _: None,
                                  lambda keys: [("시계", "older/clock.png", png())])
            self.assertEqual(client.edits, 2)
            self.assertEqual(client.edit_images[0][0], "object_ref_1.png")
            self.assertEqual(client.edit_images[1][0], "draft.png")
            self.assertEqual(result["status"], "revised")
            self.assertIn("기존 사물의 대표 색상이 크게 달라짐", result["review_notes"])
            self.assertIn("기존 사물 색상 일치 후보 우선 선택", result["final_reason"])

    def test_filename_preserves_word_and_avoids_collisions(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            root = Path(directory)
            self.assertEqual(image_filename("驚く", root), "驚く.png")
            (root / "驚く.png").write_bytes(b"image")
            self.assertEqual(image_filename("驚く", root), "驚く (2).png")
            self.assertEqual(image_filename("a/b?c", root), "a_b_c.png")
            self.assertEqual(image_filename("CON", root), "_CON.png")

    def test_image_contract_requires_800_pixel_pure_white_png(self):
        import base64
        def response(data):
            return {"data": [{"b64_json": base64.b64encode(data).decode()}]}
        with Image.open(BytesIO(_image(response(png())))) as delivered:
            self.assertEqual(delivered.size, (800, 800))
            self.assertEqual(delivered.mode, "RGB")
            self.assertEqual(delivered.getpixel((0, 0)), (255, 255, 255))
            self.assertEqual(delivered.getpixel((799, 799)), (255, 255, 255))
            self.assertEqual(delivered.getpixel((400, 400)), (255, 255, 255))
        with self.assertRaisesRegex(APIError, "1024×1024"):
            _image(response(png(width=800, height=800)))
        with self.assertRaisesRegex(APIError, "순백색 배경을 확인할 수 없어"):
            _image(response(png(background="black")))

        class CaptureClient(OpenAIClient):
            def _request(self, endpoint, body, content_type):
                self.body = json.loads(body)
                return {}

        client = CaptureClient("test-key")
        client.edit_image("test", [("sheet.jpg", b"reference")])
        self.assertEqual(client.body["size"], "1024x1024")
        self.assertEqual(client.body["background"], "opaque")
        self.assertEqual(client.body["output_format"], "png")

    def test_report_reconciles_details(self):
        items = [self.run_case(mode) for mode in ("skip", "pass", "fix", "discard")]
        job = {"id": "samplejob123", "status": "completed", "created_at": "2026-10-16",
               "updated_at": "2026-10-16"}
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            path = Path(directory) / "work_report.xlsx"
            write_report(path, job, items)
            with ZipFile(path) as workbook:
                summary = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
                detail = ET.fromstring(workbook.read("xl/worksheets/sheet2.xml"))
                ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                summary_rows = summary.findall(".//x:sheetData/x:row", ns)
                detail_rows = detail.findall(".//x:sheetData/x:row", ns)
                self.assertEqual(len(detail_rows), len(items) + 1)
                def summary_number(label):
                    for row in summary_rows:
                        cells = row.findall("x:c", ns)
                        if cells[0].find(".//x:t", ns).text == label:
                            return int(cells[1].find("x:v", ns).text)
                    self.fail(f"요약 항목 누락: {label}")
                self.assertEqual(summary_number("납품 PNG 건수"), 3)
                self.assertEqual(summary_number("수정 시도 건수"), 2)
                self.assertEqual(summary_number("이전 작업 폐기 건수"), 0)
                self.assertEqual(len(workbook.testzip() or ""), 0)


if __name__ == "__main__":
    unittest.main()
