"""Incremental ESG PDF classification; Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import sys
import time
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
RID = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
STATE_NAME = '.classification-state.json'


def stock_code(value: str) -> str:
    value = str(value).strip()
    if re.fullmatch(r'\d{1,5}(?:\.0)?', value):
        return value.split('.')[0].zfill(5)
    raise ValueError(f'无效的香港公司代码：{value!r}')


def xlsx_rows(path: Path) -> list[dict[str, str]]:
    """Follow first-sheet relationships; support shared, inline and numeric cells."""
    with zipfile.ZipFile(path) as z:
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            strings = [''.join(t.text or '' for t in si.findall('.//m:t', NS))
                       for si in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si', NS)]
        sheet = ET.fromstring(z.read('xl/workbook.xml')).find('m:sheets/m:sheet', NS)
        if sheet is None:
            raise ValueError('行业对照表没有工作表')
        relations = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        target = next((r.get('Target') for r in relations.findall(f'{{{REL}}}Relationship')
                       if r.get('Id') == sheet.get(RID)), None)
        if target is None:
            raise ValueError('无法解析首个工作表')
        name = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
        result = []
        for row in ET.fromstring(z.read(name)).findall('m:sheetData/m:row', NS):
            data = {}
            for cell in row.findall('m:c', NS):
                v = cell.find('m:v', NS)
                text = (v.text or '') if v is not None else ''
                if cell.get('t') == 's':
                    text = strings[int(text)]
                elif cell.get('t') == 'inlineStr':
                    text = ''.join(t.text or '' for t in cell.findall('.//m:t', NS))
                data[re.sub(r'\d', '', cell.get('r', ''))] = text.strip()
            if any(data.values()):
                result.append(data)
        return result


def add_company(mapping: dict, record: dict) -> None:
    previous = mapping.get(record['code'])
    if previous and (previous['sector'], previous['industry']) != (record['sector'], record['industry']):
        raise ValueError(f'代码 {record["code"]} 出现冲突行业：{previous["industry"]} / {record["industry"]}')
    mapping.setdefault(record['code'], record)


def load_mapping(table: Path, supplement: Path) -> dict:
    rows = xlsx_rows(table)
    if not rows or rows[0].get('E') != 'SASB Sector' or rows[0].get('F') != 'SASB Industry':
        raise ValueError('原表需保持A列股票代码、B列Symbol、D列公司、E列SASB Sector、F列SASB Industry')
    mapping = {}
    for row in rows[1:]:
        if not row.get('A'):
            continue
        code = stock_code(row['A'])
        if row.get('B') and stock_code(row['B'].split('.')[0]) != code:
            raise ValueError(f'原表股票代码与Symbol不一致：{code} / {row["B"]}')
        if not row.get('E') or not row.get('F'):
            raise ValueError(f'原表代码 {code} 缺少行业，请先补全')
        add_company(mapping, dict(code=code, canonical_code=code, company=row.get('D') or row.get('C', ''),
                                 sector=row['E'], industry=row['F'], method='原公司行业对照表代码匹配'))
    with supplement.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        required = {'股票代码', '公司', 'SASB Sector', 'SASB Industry'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f'补充表缺少列：{required - set(reader.fieldnames or [])}')
        supplements = {}
        for raw in reader:
            r = {k: (v or '').strip() for k, v in raw.items() if k}
            if not r.get('股票代码'):
                continue
            code = stock_code(r['股票代码'])
            if not all(r.get(k) for k in required):
                raise ValueError(f'补充表代码 {code} 缺少公司或行业')
            record = dict(code=code, canonical_code=stock_code(r.get('报告股票代码') or code), company=r['公司'],
                          sector=r['SASB Sector'], industry=r['SASB Industry'], method=r.get('分类方式') or '人工补充分类',
                          reason=r.get('分类理由', ''), evidence_file=r.get('证据报告文件', ''),
                          evidence_location=r.get('证据位置', ''), evidence=r.get('业务披露摘录', ''),
                          url=r.get('官方网页', ''), notes=r.get('备注', ''))
            add_company(supplements, record)
    # Updated original table wins; supplements fill only missing codes.
    for code, record in supplements.items():
        mapping.setdefault(code, record)
    return mapping


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip().rstrip(' .')
    if not value:
        raise ValueError('目录名称为空')
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value):
        value = '_' + value
    return value


def identify_code(relative: Path) -> tuple[str, str]:
    directory = relative.parts[0] if len(relative.parts) > 1 and re.fullmatch(r'\d{5}', relative.parts[0]) else ''
    match = re.match(r'^(\d{5})(?:[ _-]|\.HK[ _-])', relative.name, re.I)
    filename = match[1] if match else ''
    if directory and filename and directory != filename:
        return directory, f'目录代码{directory}与文件名代码{filename}不一致'
    return directory or filename, '' if directory or filename else '无法从目录或文件名识别公司代码'


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def fingerprint(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def within(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError(f'路径超出分类目录：{relative}')
    return path


def save_if_changed(path: Path, content: bytes) -> None:
    if path.exists() and path.read_bytes() == content:
        return
    temp = path.with_name(path.name + '.tmp')
    try:
        temp.write_bytes(content)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    save_if_changed(path, stream.getvalue().encode('utf-8-sig'))


def file_note(path: Path, sha: str, known: dict[str, str]) -> str:
    if sha in known:
        return known[sha]
    with path.open('rb') as f:
        start = f.read(1024)
        f.seek(max(0, path.stat().st_size - 4096))
        end = f.read()
    if b'%PDF-' not in start:
        return '未检测到PDF文件头，请检查下载内容'
    if b'%%EOF' not in end:
        return 'PDF尾部缺少EOF标记，可能下载不完整；仅分类，未修复原件'
    return ''


def make_plan(source: Path, mapping: dict) -> list[dict]:
    plan, groups = [], defaultdict(list)
    for p in sorted(source.rglob('*')):
        if not p.is_file() or p.suffix.lower() != '.pdf':
            continue
        if p.is_symlink() or not p.resolve().is_relative_to(source):
            raise ValueError(f'源目录不接受指向外部的链接：{p}')
        relative = p.relative_to(source)
        code, issue = identify_code(relative)
        company = mapping.get(code) if not issue else None
        if not company and not issue:
            issue = '对照表及补充表均无此代码'
        folder = safe_name(company['industry']) if company else '待核对'
        name = p.name if code and re.match(rf'^{code}[_ -]', p.name) else f'{code}_{p.name}' if code else p.name
        item = dict(source_key=relative.as_posix(), source=p, code=code, company=company,
                    issue=issue, target=Path(folder)/name)
        plan.append(item)
        groups[str(item['target']).casefold()].append(item)
    for group in groups.values():
        if len(group) > 1:
            for item in group:
                suffix = hashlib.sha256(item['source_key'].encode()).hexdigest()[:12]
                target = item['target']
                item['target'] = target.with_name(f'{target.stem}__{suffix}{target.suffix}')
    if len({str(p['target']).casefold() for p in plan}) != len(plan):
        raise ValueError('目标路径重名，已停止，请检查文件名和行业名')
    return plan


def export_reports(output: Path, entries: dict, source: Path, counts: Counter) -> dict:
    rows = sorted((entry['row'] for entry in entries.values()), key=lambda r: r['source'])
    fields = ['code', 'canonical_code', 'company', 'sector', 'industry', 'status', 'issue',
              'source', 'destination', 'size', 'sha256', 'classification_method', 'reason',
              'evidence_file', 'evidence_location', 'evidence', 'url', 'notes', 'file_note', 'source_present']
    write_csv(output/'分类明细.csv', rows, fields)
    pending = [r for r in rows if r['issue']]
    write_csv(output/'待核对清单.csv', pending, fields)
    write_csv(output/'补充核对明细.csv', [r for r in rows if r['industry'] and r['classification_method'] != '原公司行业对照表代码匹配'], fields)
    write_csv(output/'原始PDF读取异常.csv', [r for r in rows if r['file_note']], fields)
    write_csv(output/'多元业务及代码变更说明.csv', [r for r in rows if r['notes']], fields)
    groups = defaultdict(list)
    for row in rows:
        groups[row['industry'] or '待核对'].append(row)
    stats = []
    for industry, group in sorted(groups.items()):
        stats.append({'行业': industry, 'PDF数量': len(group),
                      '公司数量': len({r['canonical_code'] for r in group if r['canonical_code']}),
                      '原表匹配PDF': sum(r['classification_method'] == '原公司行业对照表代码匹配' for r in group),
                      '补充表匹配PDF': sum(bool(r['industry']) and r['classification_method'] != '原公司行业对照表代码匹配' for r in group)})
    write_csv(output/'行业统计.csv', stats, ['行业', 'PDF数量', '公司数量', '原表匹配PDF', '补充表匹配PDF'])
    managed = {within(output, e['target']) for e in entries.values()}
    extra = sorted(str(p.relative_to(output)) for p in output.rglob('*')
                   if p.is_file() and p.suffix.lower() == '.pdf' and p.resolve() not in managed)
    summary = dict(source_pdf=counts['source_pdf'], total_managed=len(rows),
                   industry_count=len({r['industry'] for r in rows if r['industry']}), pending=len(pending),
                   missing_source=sum(not r['source_present'] for r in rows), copied=counts['copied'],
                   moved=counts['moved'], reused=counts['reused'], cached=counts['cached'],
                   file_notes=sum(bool(r['file_note']) for r in rows), unmanaged_pdf=len(extra))
    save_if_changed(output/'未纳入管理的PDF.json', json.dumps(extra, ensure_ascii=False, indent=2).encode('utf-8'))
    explanation = (
        'ESG报告行业分类\n\n原始报告目录：'+str(source)+'\n'
        '匹配顺序：公司行业对照.xlsx精确代码优先；data/补充公司行业.csv补充缺失代码。\n'
        '按SASB Industry创建目录；补充判断不等同于官方SICS公司分类。分类明细记录来源与依据。\n'
        '保留源PDF；复制时核验SHA256；已管理文件按大小和修改时间缓存，--verify强制重新计算SHA256。\n'
        '文件情况包括已知结构异常及PDF头尾基本检查，不代表完整的PDF可读性检测。\n'
        '未知公司或代码不一致进入待核对，补充行业表后重跑即可迁移；不猜测行业。\n'
        '源文件移除时保留已有副本；不自动删除未管理的文件、用户压缩包或其他资料。\n'
        f'当前管理PDF：{len(rows)}；行业数：{summary["industry_count"]}；待核对：{len(pending)}；'
        f'源文件缺失：{summary["missing_source"]}；未管理PDF：{len(extra)}。\n')
    save_if_changed(output/'分类说明.txt', explanation.encode('utf-8'))
    return summary


def copy_verified(source: Path, destination: Path, sha: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + '.copying')
    if temp.exists():
        raise ValueError(f'存在未完成的复制临时文件，请核对后移除：{temp}')
    try:
        shutil.copy2(source, temp)
        if digest(temp) != sha:
            raise ValueError(f'源文件复制时发生变化或校验失败：{source}')
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)


def run(source: Path, output: Path, table: Path, supplement: Path,
        known_file: Path | None = None, dry_run: bool = False, verify: bool = False) -> dict:
    source, output = source.resolve(), output.resolve()
    if not source.is_dir():
        raise ValueError(f'源报告目录不存在：{source}')
    if source == output or source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError('源目录与分类目录不能相同或互相包含')
    plan = make_plan(source, load_mapping(table, supplement))
    if not plan:
        raise ValueError('源目录没有PDF，未修改任何分类记录')
    if dry_run:
        return dict(source_pdf=len(plan), pending=sum(bool(p['issue']) for p in plan),
                    industry_count=len({p['company']['industry'] for p in plan if p['company']}), dry_run=True)
    output.mkdir(parents=True, exist_ok=True)
    lock = output/'.classification.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as e:
        raise ValueError('已有分类任务运行或遗留.classification.lock；确认无任务后才可删除锁文件') from e
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(str(os.getpid()))
        state_path = output/STATE_NAME
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'version': 1, 'source': str(source), 'entries': {}}
        if state.get('version') != 1 or state.get('source') != str(source):
            raise ValueError('现有状态属于不同源目录或版本，请选择新的分类目录')
        entries = state['entries']
        known = {}
        if known_file and known_file.exists():
            known = {r['sha256']: r['原文件情况'] for r in json.loads(known_file.read_text(encoding='utf-8'))}
        counts = Counter(source_pdf=len(plan))
        active = {p['source_key'] for p in plan}
        for key, previous in entries.items():
            previous['row']['source_present'] = key in active
        for index, item in enumerate(plan, 1):
            src, key = item['source'], item['source_key']
            source_fp = fingerprint(src)
            dst = within(output, str(item['target']))
            previous = entries.get(key)
            old_path = within(output, previous['target']) if previous else None
            cached = (not verify and previous and previous['source_fp'] == source_fp
                      and old_path.is_file() and previous['target_fp'] == fingerprint(old_path))
            sha = previous['row']['sha256'] if cached else digest(src)
            note = previous['row']['file_note'] if cached else file_note(src, sha, known)
            if cached and dst == old_path:
                counts['cached'] += 1
            elif dst.exists():
                target_sha = digest(dst)
                if target_sha == sha:
                    counts['reused'] += 1
                elif previous and old_path == dst and target_sha == previous['row']['sha256']:
                    copy_verified(src, dst, sha)
                    counts['copied'] += 1
                else:
                    raise ValueError(f'目标有不同内容，未覆盖：{dst}')
            elif cached:
                dst.parent.mkdir(parents=True, exist_ok=True)
                old_path.rename(dst)
                counts['moved'] += 1
            else:
                copy_verified(src, dst, sha)
                counts['copied'] += 1
            if old_path and old_path != dst and old_path.exists():
                # Only remove an old managed copy after establishing the new copy.
                if digest(old_path) != previous['row']['sha256']:
                    raise ValueError(f'旧分类副本被修改，已保留，未删除：{old_path}')
                old_path.unlink()
            values = item['company'] or {}
            row = dict(code=item['code'], canonical_code=values.get('canonical_code', item['code']),
                       company=values.get('company', ''), sector=values.get('sector', ''), industry=values.get('industry', ''),
                       status='已分类' if values else '待核对', issue=item['issue'], source=str(src), destination=str(dst),
                       size=source_fp[0], sha256=sha, classification_method=values.get('method', ''),
                       reason=values.get('reason', ''), evidence_file=values.get('evidence_file', ''),
                       evidence_location=values.get('evidence_location', ''), evidence=values.get('evidence', ''),
                       url=values.get('url', ''), notes=values.get('notes', ''), file_note=note, source_present=True)
            entries[key] = dict(target=str(item['target']), source_fp=source_fp, target_fp=fingerprint(dst), row=row)
            if index % 100 == 0:
                save_if_changed(state_path, json.dumps(state, ensure_ascii=False, sort_keys=True).encode('utf-8'))
                if not cached:
                    print(f'已核验 {index}/{len(plan)}', flush=True)
        for previous in entries.values():
            if not within(output, previous['target']).is_file():
                raise ValueError(f'已管理分类副本丢失：{previous["target"]}')
        summary = export_reports(output, entries, source, counts)
        save_if_changed(state_path, json.dumps(state, ensure_ascii=False, sort_keys=True).encode('utf-8'))
        return summary
    finally:
        lock.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description='按原表及补充表增量分类ESG报告，保留所有源PDF')
    parser.add_argument('--source', type=Path, default=ROOT/'document')
    parser.add_argument('--output', type=Path, default=ROOT/'ESG报告_按行业分类')
    parser.add_argument('--mapping', type=Path, default=ROOT/'公司行业对照.xlsx')
    parser.add_argument('--supplement', type=Path, default=ROOT/'data/补充公司行业.csv')
    parser.add_argument('--dry-run', action='store_true', help='只预览，不写文件')
    parser.add_argument('--verify', action='store_true', help='忽略缓存，重新核验所有源文件和分类副本')
    parser.add_argument('--watch', type=float, metavar='秒', help='持续检测新增报告及行业表；Ctrl+C停止')
    args = parser.parse_args()
    if args.watch is not None and (args.watch < 1 or args.dry_run or args.verify):
        parser.error('--watch至少1秒，且不与--dry-run或--verify同时使用')
    last_display = None
    try:
        while True:
            try:
                summary = run(args.source, args.output, args.mapping, args.supplement,
                              ROOT/'data/已知文件情况.json', args.dry_run, args.verify)
                display = json.dumps(summary, ensure_ascii=False, sort_keys=True)
                if display != last_display:
                    print(display, flush=True)
                    last_display = display
            except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError) as e:
                message = f'分类未完成：{e}'
                if message != last_display:
                    print(message, file=sys.stderr, flush=True)
                    last_display = message
                if args.watch is None:
                    return 1
            if args.watch is None:
                return 0
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print('\n已停止监测，源PDF及已分类文件均已保留。')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
