import csv
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import classify_esg as app


def workbook(path, records):
    rows = [['股票代码', 'Symbol', '简称', '公司', 'SASB Sector', 'SASB Industry']] + records
    xml = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
    for n, row in enumerate(rows, 1):
        xml += f'<row r="{n}">'
        for col, value in zip('ABCDEF', row):
            if isinstance(value, int):
                xml += f'<c r="{col}{n}"><v>{value}</v></c>'
            else:
                xml += f'<c r="{col}{n}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
        xml += '</row>'
    xml += '</sheetData></worksheet>'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="行业" sheetId="9" r:id="rId9"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId9" Target="worksheets/custom.xml"/></Relationships>')
        z.writestr('xl/worksheets/custom.xml', xml)


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.output = self.root/'document', self.root/'classified'
        self.source.mkdir()
        self.table, self.extra = self.root/'mapping.xlsx', self.root/'extra.csv'
        self.records = [[1, '0001.HK', '测试', '测试公司', 'Financials', 'Commercial Banks']]
        workbook(self.table, self.records)
        self.supplement([])

    def supplement(self, rows):
        app.write_csv(self.extra, rows, ['股票代码', '报告股票代码', '公司', 'SASB Sector', 'SASB Industry'])

    def pdf(self, relative, data=b'one'):
        p = self.source/relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'%PDF-1.7\n'+data+b'\n%%EOF\n')
        return p

    def run_app(self, **options):
        return app.run(self.source, self.output, self.table, self.extra, **options)

    def rows(self):
        with (self.output/'分类明细.csv').open(encoding='utf-8-sig') as f:
            return list(csv.DictReader(f))

    def test_repeat_is_noop_and_new_known_report_is_incremental(self):
        src = self.pdf('00001/reports/a.pdf')
        before = src.read_bytes()
        self.assertEqual(self.run_app()['copied'], 1)
        state = self.output/app.STATE_NAME
        state_time = state.stat().st_mtime_ns
        self.assertEqual(self.run_app()['cached'], 1)
        self.assertEqual(state.stat().st_mtime_ns, state_time)
        self.pdf('00001/reports/b.PDF', b'two')
        result = self.run_app()
        self.assertEqual((result['copied'], result['cached']), (1, 1))
        self.assertEqual(src.read_bytes(), before)

    def test_unknown_becomes_classified_after_supplement_change(self):
        self.pdf('99999/report.pdf')
        self.assertEqual(self.run_app()['pending'], 1)
        old = Path(self.rows()[0]['destination'])
        self.supplement([{'股票代码': '99999', '公司': '新增公司', 'SASB Sector': 'Technology & Communications', 'SASB Industry': 'Software & IT Services'}])
        result = self.run_app()
        self.assertEqual((result['pending'], result['moved']), (0, 1))
        self.assertFalse(old.exists())
        self.assertTrue(Path(self.rows()[0]['destination']).is_file())

    def test_numeric_codes_and_relationships(self):
        self.assertEqual(app.load_mapping(self.table, self.extra)['00001']['industry'], 'Commercial Banks')

    def test_original_table_wins_over_supplement(self):
        self.supplement([{'股票代码': '00001', '公司': '补充名称', 'SASB Sector': 'Other', 'SASB Industry': 'Other'}])
        self.assertEqual(app.load_mapping(self.table, self.extra)['00001']['industry'], 'Commercial Banks')

    def test_alias_keeps_source_code_and_canonical_code(self):
        self.supplement([{'股票代码': '02572', '报告股票代码': '06676', '公司': 'ZG Group', 'SASB Sector': 'Technology & Communications', 'SASB Industry': 'Software & IT Services'}])
        self.pdf('02572/report.pdf')
        self.run_app()
        self.assertEqual((self.rows()[0]['code'], self.rows()[0]['canonical_code']), ('02572', '06676'))

    def test_conflicting_mapping_fails_before_any_output(self):
        workbook(self.table, self.records + [[1, '0001.HK', '', '冲突公司', 'Financials', 'Insurance']])
        self.pdf('00001/a.pdf')
        with self.assertRaisesRegex(ValueError, '冲突行业'):
            self.run_app()
        self.assertFalse(self.output.exists())

    def test_filename_code_and_directory_mismatch_are_pending(self):
        self.pdf('00001/00002_report.pdf')
        self.pdf('00001_root.pdf')
        self.pdf('no_code.pdf')
        self.assertEqual(self.run_app()['pending'], 2)
        self.assertIn('不一致', next(r['issue'] for r in self.rows() if '00002' in r['source']))

    def test_duplicate_basenames_preserve_both_files(self):
        self.pdf('00001/a/report.pdf', b'first')
        self.run_app()
        self.pdf('00001/b/report.pdf', b'second')
        self.assertEqual(self.run_app()['total_managed'], 2)
        files = list(self.output.rglob('*.pdf'))
        self.assertEqual(len(files), 2)
        self.assertEqual(len({p.read_bytes() for p in files}), 2)
        self.assertEqual(self.run_app()['cached'], 2)

    def test_preexisting_different_output_is_not_overwritten(self):
        self.pdf('00001/report.pdf')
        dest = self.output/'Commercial Banks/00001_report.pdf'
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b'keep this')
        with self.assertRaisesRegex(ValueError, '未覆盖'):
            self.run_app()
        self.assertEqual(dest.read_bytes(), b'keep this')

    def test_source_update_updates_only_managed_copy(self):
        src = self.pdf('00001/report.pdf')
        self.run_app()
        src.write_bytes(b'%PDF-1.7\nupdated report\n%%EOF')
        self.assertEqual(self.run_app()['copied'], 1)
        self.assertEqual(Path(self.rows()[0]['destination']).read_bytes(), src.read_bytes())

    def test_missing_source_keeps_classified_copy(self):
        src = self.pdf('00001/a.pdf')
        self.pdf('00001/b.pdf')
        self.run_app()
        src.unlink()
        self.assertEqual(self.run_app()['missing_source'], 1)
        self.assertEqual(len(list(self.output.rglob('*.pdf'))), 2)

    def test_modified_classified_file_is_detected(self):
        self.pdf('00001/a.pdf')
        self.run_app()
        dest = Path(self.rows()[0]['destination'])
        dest.write_bytes(b'changed by user')
        with self.assertRaisesRegex(ValueError, '未覆盖'):
            self.run_app(verify=True)
        self.assertEqual(dest.read_bytes(), b'changed by user')

    def test_dry_run_and_overlapping_roots(self):
        self.pdf('00001/a.pdf')
        self.assertTrue(self.run_app(dry_run=True)['dry_run'])
        self.assertFalse(self.output.exists())
        with self.assertRaisesRegex(ValueError, '互相包含'):
            app.run(self.source, self.source/'classified', self.table, self.extra)

    def test_lock_prevents_concurrent_task(self):
        self.pdf('00001/a.pdf')
        self.output.mkdir()
        lock = self.output/'.classification.lock'
        lock.write_text('other process')
        with self.assertRaisesRegex(ValueError, '已有分类任务'):
            self.run_app()
        self.assertEqual(lock.read_text(), 'other process')

    def test_basic_pdf_and_known_file_notes(self):
        src = self.pdf('00001/a.pdf')
        src.write_bytes(b'%PDF-1.7\ntruncated')
        self.run_app()
        self.assertIn('EOF', self.rows()[0]['file_note'])

    def test_unmanaged_files_are_preserved(self):
        self.pdf('00001/a.pdf')
        self.output.mkdir()
        external = self.output/'user.pdf'
        external.write_bytes(b'user file')
        result = self.run_app()
        self.assertEqual(result['unmanaged_pdf'], 1)
        self.assertEqual(external.read_bytes(), b'user file')

    def test_watch_detects_new_report_and_mapping_change(self):
        self.pdf('00001/a.pdf')
        process = subprocess.Popen(
            [sys.executable, str(Path(app.__file__).resolve()), '--source', str(self.source),
             '--output', str(self.output), '--mapping', str(self.table), '--supplement', str(self.extra), '--watch', '1'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        def wait_for(path):
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if path.is_file():
                    return
                if process.poll() is not None:
                    self.fail(f'watch exited: {process.communicate()}')
                time.sleep(0.1)
            self.fail(f'watch did not create {path}')
        try:
            wait_for(self.output/'Commercial Banks/00001_a.pdf')
            self.pdf('99999/new.pdf')
            pending = self.output/'待核对/99999_new.pdf'
            wait_for(pending)
            self.supplement([{'股票代码': '99999', '公司': '新增', 'SASB Sector': 'Financials', 'SASB Industry': 'Insurance'}])
            wait_for(self.output/'Insurance/99999_new.pdf')
            self.assertFalse(pending.exists())
        finally:
            process.terminate()
            process.communicate(timeout=10)


if __name__ == '__main__':
    unittest.main()
