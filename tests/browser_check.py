"""Optional headless Chrome regression. Runtime app itself does not require Selenium.

Run: python -m unittest discover -s tests -p browser_check.py -v
Uses an isolated database/browser profile. Set ESG_TEST_CHROMEDRIVER to a local driver.
"""
import io
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

from review_workbench.contracts import loads
from review_workbench.server import LocalServer
from review_workbench.service import Application
from review_workbench.store import Store


class BrowserRegression(unittest.TestCase):
    def test_demo_review_export_upload_and_responsive_layout(self):
        with tempfile.TemporaryDirectory(prefix="esg-browser-test-") as directory:
            app = Application(Store(Path(directory) / "database"))
            server = LocalServer(("127.0.0.1", 0), app)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            options = webdriver.ChromeOptions()
            for arg in ("--headless=new", "--disable-gpu", "--disable-background-networking", "--no-first-run", "--no-default-browser-check", "--disable-sync"):
                options.add_argument(arg)
            options.add_argument("--user-data-dir=" + str(Path(directory) / "browser"))
            options.add_argument("--window-size=1440,1100")
            options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
            explicit = os.environ.get("ESG_TEST_CHROMEDRIVER")
            cached = list((Path.home() / ".cache" / "selenium" / "chromedriver").glob("**/chromedriver.exe"))
            executable = explicit or (str(cached[-1]) if cached else None)
            # Optional local-test compatibility flag; never changes the application or browser security.
            service_args = ["--disable-build-check"] if os.environ.get("ESG_TEST_ALLOW_DRIVER_MISMATCH") == "1" else []
            service = Service(executable_path=executable, service_args=service_args,
                              creation_flags=0x08000000 if os.name == "nt" else 0)
            driver = None
            try:
                driver = webdriver.Chrome(service=service, options=options)
                wait = WebDriverWait(driver, 20)
                driver.get(base)
                wait.until(lambda d: d.find_element(By.ID, "demo").is_enabled())
                driver.find_element(By.ID, "demo").click()
                wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, ".task")) == 6)
                wait.until(lambda d: d.find_element(By.ID, "demo").is_enabled())
                self.assertIn("合成演示", driver.find_element(By.ID, "notice").text)
                self.assertEqual(driver.find_element(By.ID, "record-count").text, "6")
                self.assertEqual(driver.find_element(By.ID, "block-count").text, "6")
                self.assertTrue(driver.find_elements(By.CSS_SELECTOR, ".source-content table"))
                artifacts = Path(__file__).parent.parent / "review_workbench" / "validation"
                artifacts.mkdir(exist_ok=True)
                driver.save_screenshot(str(artifacts / "desktop.png"))
                # Actual form interaction, including a real zero correction and native confirmation.
                driver.find_element(By.ID, "reviewer").send_keys("浏览器测试研究员")
                Select(driver.find_element(By.ID, "action")).select_by_value("CORRECT_VALUE")
                driver.find_element(By.ID, "new-value").send_keys("0")
                driver.find_element(By.ID, "reason").send_keys("合成测试：核对原文后提出零值修订，用于验证重审流程。")
                driver.find_element(By.ID, "save").click()
                wait.until(lambda d: d.switch_to.alert)
                driver.switch_to.alert.accept()
                wait.until(lambda d: "已形成下游处理请求" in d.find_element(By.ID, "form-state").text)
                self.assertIn("候选值：0", driver.find_element(By.ID, "history").text)
                self.assertFalse(driver.find_element(By.ID, "save").is_enabled())
                with urlopen(driver.find_element(By.ID, "export").get_attribute("href")) as response:
                    with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                        handoff = loads(archive.read("s5_handoff.json").decode())
                        self.assertIn("REAUDIT_REQUIRED", {r["state"] for r in handoff["records"]})
                # Refresh preserves decisions; filters and historical navigation remain available.
                driver.refresh()
                wait.until(lambda d: "已形成下游处理请求" in d.find_element(By.ID, "form-state").text)
                Select(driver.find_element(By.ID, "filter")).select_by_value("REAUDIT_REQUIRED")
                self.assertEqual(len(driver.find_elements(By.CSS_SELECTOR, ".task")), 1)
                for width in (1440, 1024, 768, 375):
                    driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width": width, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
                    self.assertLessEqual(driver.execute_script("return document.documentElement.scrollWidth"), width)
                driver.execute_script("window.scrollTo(0,0)")
                driver.save_screenshot(str(artifacts / "mobile.png"))
                self.assertTrue(driver.find_element(By.ID, "run-picker").is_displayed())
                # Untrusted evidence is inert; uploaded data is treated as real/offline, never replayed.
                from review_workbench.make_demo import build
                bundle, _ = build()
                bundle["evidence"][0]["content"] += '<img src=x onerror="window.S4_XSS=true"><script>window.S4_XSS=true</script>'
                filename = Path(directory) / "upload.json"
                import json
                filename.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
                driver.find_element(By.ID, "upload").send_keys(str(filename))
                wait.until(lambda d: "离线审查" in d.find_element(By.ID, "notice").text and d.find_element(By.ID, "demo").is_enabled())
                self.assertIsNone(driver.execute_script("return window.S4_XSS"))
                self.assertFalse(driver.find_elements(By.CSS_SELECTOR, ".source-content img, .source-content script"))
                self.assertIn("信息不足", driver.find_element(By.ID, "findings").text)
                # Browser runtime errors are release blockers.
                errors = [e for e in driver.get_log("browser") if e["level"] == "SEVERE" and "favicon.ico" not in e["message"]]
                self.assertEqual(errors, [])
                print("Browser E2E: demo, review zero, persistence, export, upload, XSS, 4 viewport widths passed.")
            except Exception:
                if driver:
                    print("BROWSER LOG:", driver.get_log("browser"))
                    print("UI CHECK:", driver.execute_script("return {mode:document.getElementById('mode').outerHTML,buttons:['demo','upload','audit','save'].map(id=>[id,!!document.getElementById(id)])}"))
                raise
            finally:
                if driver:
                    driver.quit()
                server.shutdown(); server.server_close(); app.close()


if __name__ == "__main__":
    unittest.main()
