"""Optional real Chrome test of the B adapter UI; uses an isolated local database.

python -m unittest discover -s tests -p browser_b_check.py -v
Uses the same optional local-driver environment variables as browser_check.py.
"""
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import urlopen
import zipfile

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait

from review_workbench.server import LocalServer
from review_workbench.service import Application
from review_workbench.store import Store


class BBrowserRegression(unittest.TestCase):
    def test_b_demo_import_review_export_pdf_and_mobile(self):
        with tempfile.TemporaryDirectory(prefix="b-ui-") as folder:
            directory = Path(folder)
            app = Application(Store(directory / "db"))
            server = LocalServer(("127.0.0.1", 0), app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            options = webdriver.ChromeOptions()
            for arg in ("--headless=new", "--disable-gpu", "--disable-background-networking", "--no-first-run", "--no-default-browser-check", "--disable-sync"):
                options.add_argument(arg)
            options.add_argument("--user-data-dir=" + str(directory / "browser"))
            options.add_argument("--window-size=1440,1100")
            options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
            cached = list((Path.home() / ".cache/selenium/chromedriver").glob("**/chromedriver.exe"))
            executable = os.environ.get("ESG_TEST_CHROMEDRIVER") or (str(cached[-1]) if cached else None)
            service = Service(executable_path=executable,
                              service_args=["--disable-build-check"] if os.environ.get("ESG_TEST_ALLOW_DRIVER_MISMATCH") == "1" else [],
                              creation_flags=0x08000000 if os.name == "nt" else 0)
            driver = None
            try:
                driver = webdriver.Chrome(service=service, options=options)
                wait = WebDriverWait(driver, 25)
                driver.get(f"http://127.0.0.1:{server.server_port}")
                driver.find_element(By.CSS_SELECTOR, ".advanced-tools > summary").click()
                wait.until(lambda d: "正在检查" not in d.find_element(By.ID, "b-config").text)
                self.assertFalse(driver.find_element(By.ID, "b-run").is_enabled())
                driver.find_element(By.ID, "b-demo").click()
                wait.until(lambda d: "B合成演示" in d.find_element(By.ID, "report-meta").text and d.find_element(By.ID, "b-demo").is_enabled())
                self.assertEqual(driver.find_element(By.ID, "record-count").text, "3")
                self.assertFalse(driver.find_element(By.ID, "audit").is_enabled())
                self.assertIn("合成演示", driver.find_element(By.ID, "notice").text)
                self.assertIn("原始批次", driver.find_element(By.ID, "b-summary-text").text)
                unknown = next(b for b in driver.find_elements(By.CSS_SELECTOR, ".task") if "Plant B" in b.text or "信息不足" in b.text)
                unknown.click()
                self.assertIn("信息不足", driver.find_element(By.ID, "findings").text)
                # This source-free/trace-failure row is present even without an accepted finding.
                self.assertIn("证据不存在", driver.find_element(By.ID, "evidence").text)
                problem = next(b for b in driver.find_elements(By.CSS_SELECTOR, ".task") if "优先" not in b.text and "P0" in b.text)
                problem.click()
                driver.find_element(By.ID, "reviewer").send_keys("B浏览器复核员")
                Select(driver.find_element(By.ID, "action")).select_by_value("CORRECT_VALUE")
                driver.find_element(By.ID, "new-value").send_keys("0")
                driver.find_element(By.ID, "reason").send_keys("合成样例，验证零值修订留痕。")
                driver.find_element(By.ID, "save").click()
                wait.until(lambda d: d.switch_to.alert)
                driver.switch_to.alert.accept()
                wait.until(lambda d: "候选值：0" in d.find_element(By.ID, "history").text)
                self.assertFalse(driver.find_element(By.ID, "save").is_enabled())
                with urlopen(driver.find_element(By.ID, "export").get_attribute("href")) as response:
                    with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                        self.assertIn("REAUDIT_REQUIRED", archive.read("workbench/review_events.jsonl").decode())
                        originals = {Path(n).name: archive.read(n).decode("utf-8") for n in archive.namelist() if n.startswith("b_original/")}
                # Reimport from actual selected files. Untrusted evidence remains inert.
                evidence = [json.loads(line) for line in originals["evidence.jsonl"].splitlines() if line.strip()]
                evidence[0]["content"] += '<img src=x onerror="window.B_XSS=true"><script>window.B_XSS=true</script>'
                originals["evidence.jsonl"] = "\n".join(json.dumps(row, ensure_ascii=False) for row in evidence)
                selected = []
                for name, text in originals.items():
                    path = directory / name
                    path.write_text(text, encoding="utf-8")
                    selected.append(str(path))
                driver.find_element(By.ID, "b-files").send_keys("\n".join(selected))
                wait.until(lambda d: d.find_element(By.ID, "b-import").is_enabled())
                driver.find_element(By.ID, "b-import").click()
                wait.until(lambda d: "B结果已导入" in d.find_element(By.ID, "notice").text)
                self.assertIsNone(driver.execute_script("return window.B_XSS"))
                self.assertFalse(driver.find_elements(By.CSS_SELECTOR, ".source-content img,.source-content script"))
                self.assertIn("未核验A契约", driver.find_element(By.ID, "report-meta").text)
                pdf = directory / "source.pdf"
                import fitz
                document = fitz.open()
                for _ in range(20):
                    document.new_page()
                document.save(pdf)
                document.close()
                driver.find_element(By.ID, "b-pdf").send_keys(str(pdf))
                wait.until(lambda d: "PDF已附加" in d.find_element(By.ID, "notice").text)
                self.assertTrue(driver.find_elements(By.CSS_SELECTOR, ".pdf-link"))
                driver.refresh()
                wait.until(lambda d: "B结果已导入" in d.find_element(By.ID, "notice").text)
                self.assertTrue(driver.find_elements(By.CSS_SELECTOR, ".pdf-link"))
                artifacts = Path(__file__).resolve().parents[1] / "review_workbench/validation"
                artifacts.mkdir(exist_ok=True)
                driver.execute_script("window.scrollTo(0,0)")
                driver.save_screenshot(str(artifacts / "b-desktop.png"))
                for width in (1440, 1024, 768, 375):
                    driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width": width, "height": 1100, "deviceScaleFactor": 1, "mobile": False})
                    self.assertLessEqual(driver.execute_script("return document.documentElement.scrollWidth"), width)
                driver.execute_script("window.scrollTo(0,0)")
                driver.save_screenshot(str(artifacts / "b-mobile.png"))
                errors = [e for e in driver.get_log("browser") if e["level"] == "SEVERE" and "favicon.ico" not in e["message"]]
                self.assertEqual(errors, [])
            finally:
                if driver:
                    driver.quit()
                server.shutdown()
                server.server_close()
                app.close()


if __name__ == "__main__":
    unittest.main()
