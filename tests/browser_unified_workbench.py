"""Browser acceptance: upload PDF -> extraction -> A -> B -> review, zero API."""
import io
import json
import os
import sys
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import fitz
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait

from review_workbench.service import Application
from review_workbench.store import Store
from review_workbench.server import LocalServer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


class UnifiedBrowserCheck(unittest.TestCase):
    def test_upload_all_stages_review_and_original_page_in_one_ui(self):
        artifacts=ROOT/'data/maintenance/unified_workbench'
        artifacts.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='esg-unified-') as temporary:
            directory=Path(temporary)
            source=directory/'browser-report.pdf'
            from esg_pipeline.esgagent_core import delivered_reports
            supplied=delivered_reports()[0]
            source.write_bytes(Path(supplied['_source']).read_bytes())
            app=Application(Store(directory/'db'))
            server=LocalServer(('127.0.0.1',0),app)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            browser=None
            try:
                options=webdriver.ChromeOptions()
                for flag in ('--headless=new','--disable-gpu','--disable-background-networking','--disable-sync','--no-first-run','--no-default-browser-check'):
                    options.add_argument(flag)
                options.add_argument('--user-data-dir='+str(directory/'chrome'))
                options.add_argument('--window-size=1440,1100')
                options.set_capability('goog:loggingPrefs',{'browser':'ALL'})
                drivers=list((Path.home()/'.cache/selenium/chromedriver').glob('**/chromedriver.exe'))
                service=Service(str(drivers[-1]),service_args=['--disable-build-check'],creation_flags=0x08000000 if os.name=='nt' else 0)
                browser=webdriver.Chrome(service=service,options=options)
                wait=WebDriverWait(browser,45)
                browser.get(f'http://127.0.0.1:{server.server_port}/')
                wait.until(lambda d:len(d.find_elements(By.CSS_SELECTOR,'#pipeline-source option'))==14)
                self.assertEqual(Select(browser.find_element(By.ID,'pipeline-mode')).first_selected_option.get_attribute('value'),'rules_only')
                for width in (1440,768,375):
                    browser.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',{'width':width,'height':1100,'deviceScaleFactor':1,'mobile':False})
                    self.assertLessEqual(browser.execute_script('return document.documentElement.scrollWidth'),width)
                browser.save_screenshot(str(artifacts/'mobile.png'))
                browser.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',{'width':1440,'height':1100,'deviceScaleFactor':1,'mobile':False})
                browser.find_element(By.ID,'pipeline-upload').send_keys(str(source))
                wait.until(lambda d:d.find_element(By.ID,'pipeline-start').is_enabled())
                with patch('esg_pipeline.runner.JsonClient',side_effect=AssertionError('Local UI must not call models')):
                    browser.find_element(By.ID,'pipeline-start').click()
                    wait.until(lambda d:'已执行' in d.find_element(By.ID,'pipeline-job-state').text)
                    wait.until(lambda d:not d.find_element(By.ID,'pipeline-data').get_attribute('hidden'))
                    wait.until(lambda d:len(d.find_elements(By.CSS_SELECTOR,'#pipeline-table-body tr'))>0)
                self.assertIn('新增请求 0',browser.find_element(By.ID,'pipeline-job-usage').text)
                self.assertEqual(len(browser.find_elements(By.CSS_SELECTOR,'#pipeline-steps .done')),3)
                self.assertEqual(len(browser.find_elements(By.CSS_SELECTOR,'#pipeline-steps .current')),1)
                browser.save_screenshot(str(artifacts/'desktop.png'))
                browser.find_element(By.CSS_SELECTOR,'#pipeline-table-body button').click()
                wait.until(lambda d:d.find_element(By.ID,'pipeline-image').get_property('naturalWidth')>0)
                self.assertTrue(browser.find_element(By.ID,'pipeline-image-dialog').is_displayed())
                browser.save_screenshot(str(artifacts/'source-page.png'))
                browser.find_element(By.ID,'pipeline-image-close').click()
                browser.find_element(By.ID,'pipeline-tab-classify').click()
                wait.until(lambda d:'A归类记录' in d.find_element(By.ID,'pipeline-table-caption').text)
                run=app.pipeline.jobs()[0]['result_run_id']
                view=app.detail(run)
                self.assertEqual(view['tasks'],[])
                self.assertTrue(view['review_routing']['unverified_records'])
                # Inject one concrete mock discrepancy into this temporary UI
                # fixture; real cached extraction has no proven discrepancy.
                record=next(r for r in view['bundle']['records'] if r.get('value') is not None)
                result=app.store.detail(run)['result']
                finding={'finding_id':'test_concrete','record_uid':record['record_uid'],'record_id':record['record_uid'],
                    'indicator_id':record['indicator_id'],'code':'A-EXTRACT-VALUE','severity':'BLOCK',
                    'reason':'测试模拟：记录与原表数值不同，需确认数量级。','observed':'5600','expected':'560',
                    'evidence_span':'560.00','evidence_ids':[record['evidence_id']],'confidence':1}
                result['findings'].append(finding)
                task={'task_id':'test_review_task','run_id':run,'record_uid':record['record_uid'],
                    'indicator_id':record['indicator_id'],'finding_ids':['test_concrete'],'review_status':'SUSPECT','priority':'P0'}
                with app.store.connect() as db:
                    db.execute('UPDATE runs SET result=? WHERE id=?',(json.dumps(result),run))
                    db.execute('INSERT INTO tasks(id,run_id,payload) VALUES(?,?,?)',(task['task_id'],run,json.dumps(task)))
                browser.refresh()
                wait.until(lambda d:d.find_element(By.ID,'review-form').is_displayed())
                self.assertIn('需要确认',browser.find_element(By.ID,'findings').text)
                browser.find_element(By.ID,'reviewer').send_keys('统一工作台测试员')
                browser.find_element(By.ID,'reason').send_keys('合成PDF验证，暂缓并保留原文。')
                browser.find_element(By.ID,'save').click()
                wait.until(lambda d:'复核意见已保存' in d.find_element(By.ID,'notice').text)
                browser.refresh()
                wait.until(lambda d:supplied['_company']['name'] in d.find_element(By.ID,'company').text)
                jobs=app.pipeline.jobs()
                self.assertEqual(len(jobs),1)
                detail=app.detail(jobs[0]['result_run_id'])
                self.assertEqual(len(detail['events']),1)
                self.assertTrue(detail['has_pdf'])
                browser.find_element(By.ID,'water-preview').click()
                wait.until(lambda d:'已检查' in d.find_element(By.ID,'water-status').text)
                self.assertFalse(browser.find_element(By.ID,'water-preview-wrap').get_attribute('hidden'))
                self.assertIn('已检查1份报告',browser.find_element(By.ID,'water-status').text)
                browser.find_element(By.ID,'water-preview-all').click()
                wait.until(lambda d:d.find_element(By.ID,'water-preview-all').is_enabled())
                baseline=len(json.loads((ROOT/'data/esgagent_pipeline/latest_run.json').read_text(encoding='utf8'))['reports'])
                self.assertIn(f'已检查{baseline+1}份报告',browser.find_element(By.ID,'water-status').text)
                self.assertGreater(len(browser.find_elements(By.CSS_SELECTOR,'#water-preview-body tr')),0)
                model_rows=[r for r in browser.find_elements(By.CSS_SELECTOR,'#water-preview-body tr') if '00807' in r.text and '循环利用水量' in r.text and '79651697.9' in r.text]
                if model_rows:
                    self.assertIn('近似换算',model_rows[0].text)
                self.assertFalse(browser.find_element(By.ID,'water-legacy-tools').is_displayed())
                self.assertEqual(len(app.pipeline.jobs()),1)
                browser.find_element(By.ID,'water-title').location_once_scrolled_into_view
                browser.save_screenshot(str(artifacts/'annual-preview.png'))
                # Choosing model mode without consent must not submit another job.
                Select(browser.find_element(By.ID,'pipeline-source')).select_by_index(1)
                Select(browser.find_element(By.ID,'pipeline-mode')).select_by_value('model')
                browser.find_element(By.ID,'pipeline-start').click()
                wait.until(lambda d:'API调用范围' in d.find_element(By.ID,'pipeline-error').text)
                self.assertEqual(len(app.pipeline.jobs()),1)
                errors=[e for e in browser.get_log('browser') if e['level']=='SEVERE' and 'favicon.ico' not in e['message']]
                self.assertEqual(errors,[])
                (artifacts/'verification.json').write_text(json.dumps({'uploaded_pdf':True,'all_stages':True,'page_in_same_ui':True,'review_saved':True,'refresh_restored':True,'annual_selected_preview':True,'annual_all_reports_preview':True,'tested_widths':[1440,768,375],'new_api_calls':0,'console_errors':0},indent=2),encoding='utf-8')
            finally:
                if browser:browser.quit()
                server.shutdown();server.server_close();app.close()


if __name__=='__main__':unittest.main()
