"""Real browser: multi-upload, cache miss continuation, dedupe, refresh and XLSX."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from urllib.request import urlopen
from unittest.mock import patch

import fitz
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select,WebDriverWait

from review_workbench.service import Application
from review_workbench.store import Store
from review_workbench.server import LocalServer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


class BatchBrowserCheck(unittest.TestCase):
    def test_batch_in_browser_without_paid_calls(self):
        artifacts=ROOT/'data/maintenance/batch_queue';artifacts.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='esg-batch-') as temporary:
            folder=Path(temporary)
            from esg_pipeline.esgagent_core import delivered_reports
            supplied=delivered_reports()[0]
            files=[folder/'00807_uncached.pdf',folder/'02460_cached.pdf',folder/'02460_duplicate.pdf']
            with fitz.open() as pdf:
                pdf.new_page().insert_text((20,30),'A new uncached report');pdf.save(files[0])
            for path in files[1:]:path.write_bytes(Path(supplied['_source']).read_bytes())
            app=Application(Store(folder/'db'));server=LocalServer(('127.0.0.1',0),app)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            browser=None
            try:
                options=webdriver.ChromeOptions()
                for flag in ('--headless=new','--disable-gpu','--disable-background-networking','--disable-sync','--no-first-run','--no-default-browser-check'):options.add_argument(flag)
                options.add_argument('--user-data-dir='+str(folder/'chrome'));options.add_argument('--window-size=1440,1100')
                options.set_capability('goog:loggingPrefs',{'browser':'ALL'})
                drivers=list((Path.home()/'.cache/selenium/chromedriver').glob('**/chromedriver.exe'))
                service=Service(str(drivers[-1]),service_args=['--disable-build-check'],creation_flags=0x08000000 if os.name=='nt' else 0)
                browser=webdriver.Chrome(service=service,options=options);wait=WebDriverWait(browser,60)
                base=f'http://127.0.0.1:{server.server_port}'
                browser.get(base+'/')
                wait.until(lambda d:len(d.find_elements(By.CSS_SELECTOR,'#batch-existing option'))>=3)
                browser.find_element(By.ID,'batch-upload').send_keys('\n'.join(map(str,files)))
                wait.until(lambda d:'本机已添加3份' in d.find_element(By.ID,'batch-upload-status').text)
                self.assertEqual(len(browser.find_elements(By.CSS_SELECTOR,'#batch-draft-body tr')),3)
                Select(browser.find_element(By.ID,'batch-mode')).select_by_value('model')
                browser.find_element(By.ID,'batch-start').click()
                wait.until(lambda d:'外发范围' in d.find_element(By.ID,'batch-error').text)
                self.assertEqual(app.pipeline.batches.list(),[])
                Select(browser.find_element(By.ID,'batch-mode')).select_by_value('rules_only')
                browser.find_element(By.ID,'batch-start').click()
                wait.until(lambda d:'队列完成' in d.find_element(By.ID,'batch-state').text)
                batch=app.pipeline.batches.list()[0]
                self.assertEqual([x['status'] for x in batch['items']],['FAILED','PARTIAL','SKIPPED'])
                self.assertEqual(batch['usage']['calls'],0)
                self.assertFalse(browser.find_element(By.ID,'batch-export').get_attribute('hidden'))
                browser.find_element(By.ID,'batch-result').location_once_scrolled_into_view
                browser.save_screenshot(str(artifacts/'desktop.png'))
                for width in (375,768,1440):
                    browser.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',{'width':width,'height':1100,'deviceScaleFactor':1,'mobile':False})
                    self.assertLessEqual(browser.execute_script('return document.documentElement.scrollWidth'),width)
                    if width==375:browser.save_screenshot(str(artifacts/'mobile.png'))
                # The download endpoint must contain only this batch's completed report.
                from esg_pipeline.workbook_export import export_workbook
                with patch('esg_pipeline.workbook_export.export_workbook',side_effect=lambda c,p:export_workbook(c,p,preview=True)):
                    with urlopen(browser.find_element(By.ID,'batch-export').get_attribute('href'),timeout=180) as response:
                        self.assertEqual(response.read(2),b'PK')
                exported=app.pipeline.batches.directory/batch['batch_id']/'water-2025.xlsx'
                shutil.copy2(exported,artifacts/'batch-water-2025.xlsx')
                for suffix in ('.water.png','.identity.png','.inspect.json'):
                    p=Path(str(exported)+suffix)
                    if p.exists():shutil.copy2(p,artifacts/p.name)
                with urlopen(browser.find_element(By.ID,'batch-receipt').get_attribute('href')) as response:
                    receipt=json.load(response)
                    self.assertEqual(receipt['counts']['FAILED'],1)
                browser.refresh();wait.until(lambda d:'队列完成' in d.find_element(By.ID,'batch-state').text)
                self.assertIn('成功执行1份',browser.find_element(By.ID,'batch-state').text)
                buttons=browser.find_elements(By.CSS_SELECTOR,'#batch-results-body button')
                self.assertEqual(len(buttons),1);buttons[0].click()
                wait.until(lambda d:not d.find_element(By.ID,'pipeline-data').get_attribute('hidden'))
                errors=[e for e in browser.get_log('browser') if e['level']=='SEVERE' and 'favicon.ico' not in e['message']]
                self.assertEqual(errors,[])
                (artifacts/'verification.json').write_text(json.dumps({'multi_upload':True,'failure_continues':True,'duplicate_skipped':True,'refresh_restored':True,'batch_export':True,'scoped_company':supplied['company_code'],'new_api_calls':0,'widths':[375,768,1440],'console_errors':0},indent=2),encoding='utf8')
            finally:
                if browser:browser.quit()
                server.shutdown();server.server_close();app.close()


if __name__=='__main__':unittest.main()
