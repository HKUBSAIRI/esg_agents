"""Phase-1 冒烟：验证 qwen3.8-flash 是否能通过 file_data 直读 PDF 并返回 JSON。"""
from __future__ import annotations
import base64, json, os, sys, time, pathlib

import pymupdf
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv('config/.env')

def build_test_pdf(path: pathlib.Path) -> None:
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((60, 80), "Test ESG Report 2025", fontsize=18)
    p1.insert_text((60, 130), "Water performance table (Group level, 2025)", fontsize=11)
    rows = [("Total water withdrawal", "1,234,567", "m3"),
            ("Total water consumption", "456,789", "m3"),
            ("Water recycling rate", "32.5", "%")]
    y = 170
    for r in rows:
        p1.insert_text((60, y), r[0]); p1.insert_text((260, y), r[1]); p1.insert_text((380, y), r[2]); y += 22
    p2 = doc.new_page()
    p2.insert_text((60, 80), "Emission performance (2025)", fontsize=14)
    p2.insert_text((60, 120), "COD discharge: 12.3 tonnes; Ammonia nitrogen: 1.2 tonnes", fontsize=11)
    doc.save(path)

def main() -> int:
    pdf_path = pathlib.Path('outputs/_smoke.pdf')
    pdf_path.parent.mkdir(exist_ok=True)
    build_test_pdf(pdf_path)
    b64 = base64.b64encode(pdf_path.read_bytes()).decode()
    client = OpenAI(api_key=os.environ['DASHSCOPE_API_KEY'], base_url=os.environ['DASHSCOPE_BASE_URL'])
    model = os.environ.get('QWEN_MODEL', 'qwen3.8-flash')
    prompt = ("你是 ESG 数据提取器。读取所附 PDF，逐页找出水资源与排放数据。"
              "只输出 JSON：{\"pages\":[{\"page\":<物理页码>,\"items\":[{\"metric\":\"...\",\"value\":\"...\",\"unit\":\"...\",\"evidence\":\"逐字原文\"}]}]}")
    t0 = time.time()
    print(f'model={model} pdf={pdf_path.stat().st_size}B b64={len(b64)}B')
    resp = client.chat.completions.create(
        model=model,
        messages=[{'role': 'user', 'content': [
            {'type': 'file', 'file': {'file_data': f'data:application/pdf;base64,{b64}', 'filename': 'smoke.pdf'}},
            {'type': 'text', 'text': prompt},
        ]}],
        max_tokens=2000,
        temperature=0,
    )
    dt = time.time() - t0
    msg = resp.choices[0].message
    print(f'OK latency={dt:.1f}s finish={resp.choices[0].finish_reason} usage={resp.usage}')
    print('content_len=', len(msg.content or ''))
    print('--- content head ---')
    print((msg.content or '')[:1500])
    try:
        json.loads((msg.content or '').strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
        print('JSON_PARSE_OK')
    except Exception as e:
        print('JSON_PARSE_FAIL', e)
    return 0

if __name__ == '__main__':
    sys.exit(main())
