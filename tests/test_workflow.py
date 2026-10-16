import json
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from zipfile import ZipFile

from app import parse_csv
from pipeline import image_filename, process_item
from report_xlsx import write_report

TEST_TEMP = Path(__file__).resolve().parents[1] / "output" / "test_tmp"
TEST_TEMP.mkdir(parents=True, exist_ok=True)


def png(width=1024, height=1024):
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
    pixels = b"\x00" + b"\xff\xff\xff" * width
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels * height, 1)) + chunk(b"IEND", b""))


class FakeClient:
    def __init__(self, mode):
        self.mode = mode
        self.edits = 0
        self.reviews = 0
        self.image_prompts = []

    def response(self, prompt, schema_name, schema, images=None):
        if schema_name == "vocabulary_image_plan":
            content = {"draw": self.mode != "skip", "reason": "연상에 도움" if self.mode != "skip" else "시각화 이득 없음",
                       "scene": "문 앞에서 망설이는 모습", "memory_cue": "멈춘 동작",
                       "use_squirrel": self.mode not in ("object", "text"),
                       "use_text": self.mode == "text", "allowed_text": "A" if self.mode == "text" else ""}
        else:
            self.reviews += 1
            passing = self.mode in ("pass", "minor", "object", "text") or (self.mode == "fix" and self.reviews == 2)
            content = {"decision": "keep" if passing else "revise",
                       "issues": "약한 그라데이션" if self.mode == "minor" else ("" if passing else "손가락이 보임"),
                       "fix_instruction": "" if passing else "손가락을 없애라"}
        return {"output_text": json.dumps(content), "usage": {"input_tokens": 3, "output_tokens": 2}}

    def edit_image(self, prompt, images):
        import base64
        self.edits += 1
        self.image_prompts.append(prompt)
        return {"data": [{"b64_json": base64.b64encode(png()).decode()}],
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
            self.assertEqual(client.edits, {"skip": 0, "pass": 1, "minor": 1, "fix": 2, "discard": 2}[mode])
            self.assertEqual(result["status"], {"skip": "skipped", "pass": "generated",
                                                "minor": "generated", "fix": "revised", "discard": "discarded"}[mode])
            self.assertEqual(len(list(root.glob("*.png"))), 1 if mode in ("pass", "minor", "fix") else 0)
            if mode == "fix":
                self.assertEqual(result["revision_count"], 1)
            return result.copy()

    def test_decisions_and_single_revision(self):
        for mode in ("skip", "pass", "minor", "fix", "discard"):
            with self.subTest(mode=mode):
                self.run_case(mode)

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

    def test_filename_preserves_word_and_avoids_collisions(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            root = Path(directory)
            self.assertEqual(image_filename("驚く", root), "驚く.png")
            (root / "驚く.png").write_bytes(b"image")
            self.assertEqual(image_filename("驚く", root), "驚く (2).png")
            self.assertEqual(image_filename("a/b?c", root), "a_b_c.png")
            self.assertEqual(image_filename("CON", root), "_CON.png")

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
                self.assertEqual(int(summary_rows[12].findall("x:c", ns)[1].find("x:v", ns).text), 2)
                self.assertEqual(len(workbook.testzip() or ""), 0)


if __name__ == "__main__":
    unittest.main()
