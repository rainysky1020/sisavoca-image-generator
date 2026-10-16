import json
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
from io import BytesIO

from PIL import Image

import app
from test_workflow import FakeClient, TEST_TEMP


class HttpWorkflowTests(unittest.TestCase):
    def test_upload_progress_and_downloads(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            output = Path(directory)
            with patch.object(app, "OUTPUT", output), patch.object(app, "DB_PATH", output / "jobs.db"), \
                 patch.object(app, "OpenAIClient", lambda key: FakeClient("pass")), \
                 patch.object(app, "load_api_key", lambda path: "test-key"):
                app.init_db()
                server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = f"http://127.0.0.1:{server.server_port}"
                try:
                    body = ("word,meaning_ko\nhesitate,망설이다\n"
                            "驚く,놀라다\n,사과\n").encode()
                    req = urllib.request.Request(base + "/api/jobs", body, method="POST",
                                                 headers={"Content-Type": "text/csv", "X-Filename": "sample.csv"})
                    with urllib.request.urlopen(req) as response:
                        job_id = json.load(response)["job_id"]
                    for _ in range(100):
                        with urllib.request.urlopen(base + f"/api/jobs/{job_id}") as response:
                            result = json.load(response)
                        if result["job"]["status"] == "completed":
                            break
                        time.sleep(.05)
                    self.assertEqual(result["job"]["status"], "completed")
                    self.assertEqual([x["status"] for x in result["items"]],
                                     ["generated", "generated", "error"])
                    self.assertEqual([x["image_file"] for x in result["items"][:2]],
                                     ["hesitate.png", "驚く.png"])
                    with urllib.request.urlopen(base + f"/api/jobs/{job_id}/report") as response:
                        with ZipFile(BytesIO(response.read())) as report:
                            self.assertIn("xl/worksheets/sheet2.xml", report.namelist())
                    with urllib.request.urlopen(base + f"/api/jobs/{job_id}/images.zip") as response:
                        with ZipFile(BytesIO(response.read())) as images:
                            self.assertEqual(images.namelist(), ["hesitate.png", "驚く.png"])
                    with urllib.request.urlopen(base + f"/api/jobs/{job_id}/images/%E9%A9%9A%E3%81%8F.png") as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn("filename*=UTF-8''", response.headers["Content-Disposition"])
                        with Image.open(BytesIO(response.read())) as delivered:
                            self.assertEqual(delivered.size, (800, 800))
                            self.assertEqual(delivered.mode, "RGB")
                            self.assertEqual(delivered.getpixel((0, 0)), (255, 255, 255))
                finally:
                    server.shutdown()
                    server.server_close()


if __name__ == "__main__":
    unittest.main()
