"""Real-browser checks of the completed user batch; never calls external models."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from review_workbench.service import Application
from review_workbench.server import LocalServer
from review_workbench.store import Store

ROOT = Path(__file__).resolve().parents[1]


class PipelineBrowserCheck(unittest.TestCase):
    def test_ten_report_index_and_real_a_b_pdf_trace(self):
        directory = ROOT / "data/esg_pipeline"
        current = json.loads((directory / "latest_run.json").read_text(encoding="utf-8"))
        self.assertEqual(len(current["reports"]), 10)
        self.assertFalse(current["failures"])
        selected = next(r for r in current["reports"] if r["source_pdf"].startswith("00807"))
        artifacts = directory / "browser_validation"
        artifacts.mkdir(exist_ok=True)
        app = Application(Store(ROOT / "data/review_workbench"))
        server = LocalServer(("127.0.0.1", 0), app)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        driver = None
        try:
            with tempfile.TemporaryDirectory(prefix="pipeline-browser-") as profile:
                options = webdriver.ChromeOptions()
                for arg in ("--headless=new", "--disable-gpu", "--disable-background-networking", "--disable-sync", "--no-first-run", "--no-default-browser-check"):
                    options.add_argument(arg)
                options.add_argument("--user-data-dir=" + profile)
                options.add_argument("--window-size=1440,1100")
                options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
                cached = list((Path.home() / ".cache/selenium/chromedriver").glob("**/chromedriver.exe"))
                service = Service(str(cached[-1]), service_args=["--disable-build-check"], creation_flags=0x08000000 if os.name == "nt" else 0)
                driver = webdriver.Chrome(service=service, options=options)
                driver.get((directory / "全流程结果.html").as_uri())
                self.assertEqual(len(driver.find_elements(By.CSS_SELECTOR, "tr")), 11)
                driver.save_screenshot(str(artifacts / "index-desktop.png"))
                for width in (1440, 768, 375):
                    driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width": width, "height": 1100, "deviceScaleFactor": 1, "mobile": False})
                    self.assertLessEqual(driver.execute_script("return document.documentElement.scrollWidth"), width)
                driver.get((Path(selected["output_directory"]) / "结果预览.html").as_uri())
                self.assertIn("00807", driver.find_element(By.TAG_NAME, "h1").text)
                self.assertGreater(len(driver.find_elements(By.CSS_SELECTOR, "a[href$='.png']")), 0)
                self.assertLessEqual(driver.execute_script("return document.documentElement.scrollWidth"), 375)
                driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 1100, "deviceScaleFactor": 1, "mobile": False})
                driver.save_screenshot(str(artifacts / "record-preview.png"))
                driver.get(f'http://127.0.0.1:{server.server_port}/#{selected["workbench_run_id"]}')
                wait = WebDriverWait(driver, 30)
                wait.until(lambda d: d.find_element(By.ID, "record-count").text == str(selected["record_count"]))
                self.assertEqual(driver.find_element(By.ID, "evidence-count").text, str(selected["evidence_count"]))
                self.assertIn("00807", driver.find_element(By.ID, "company").text)
                self.assertGreater(len(driver.find_elements(By.CSS_SELECTOR, ".task")), 0)
                driver.save_screenshot(str(artifacts / "workbench-desktop.png"))
                for width in (1440, 768, 375):
                    driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width": width, "height": 1100, "deviceScaleFactor": 1, "mobile": False})
                    self.assertLessEqual(driver.execute_script("return document.documentElement.scrollWidth"), width)
                errors = [r for r in driver.get_log("browser") if r["level"] == "SEVERE" and "favicon.ico" not in r["message"]]
                self.assertEqual(errors, [])
                detail = app.detail(selected["workbench_run_id"])
                self.assertTrue(detail["has_pdf"])
                self.assertEqual(detail["pdf_sha256"], selected["source_sha256"])
                (artifacts / "browser_check.json").write_text(json.dumps({"reports": 10, "record_count": selected["record_count"],
                    "evidence_count": selected["evidence_count"], "pdf_attached": True, "tested_widths": [1440, 768, 375], "console_errors": 0}, indent=2), encoding="utf-8")
                driver.quit()
                driver = None
        finally:
            if driver:
                driver.quit()
            server.shutdown()
            server.server_close()
            app.close()


if __name__ == "__main__":
    unittest.main()
