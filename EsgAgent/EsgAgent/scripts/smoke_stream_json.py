"""冒烟2：验证 streaming + response_format=json_object + include_usage。"""
import base64, json, os, time
from dotenv import load_dotenv
from openai import OpenAI
load_dotenv('config/.env')
client = OpenAI(api_key=os.environ['DASHSCOPE_API_KEY'], base_url=os.environ['DASHSCOPE_BASE_URL'])
b64 = base64.b64encode(open('outputs/_smoke.pdf','rb').read()).decode()
t0=time.time()
try:
    stream = client.chat.completions.create(
        model=os.environ.get('QWEN_MODEL','qwen3.8-flash'),
        stream=True, stream_options={'include_usage': True},
        response_format={'type':'json_object'},
        messages=[{'role':'user','content':[
            {'type':'file','file':{'file_data':f'data:application/pdf;base64,{b64}','filename':'smoke.pdf'}},
            {'type':'text','text':'读取 PDF，输出 JSON {"pages":[...]}，包含页码与逐字证据。'}]}],
        max_tokens=800, temperature=0)
    chunks=[]; usage=None; finish=None
    for ev in stream:
        if ev.usage: usage=ev.usage
        if ev.choices:
            d=ev.choices[0].delta
            if d and d.content: chunks.append(d.content)
            if ev.choices[0].finish_reason: finish=ev.choices[0].finish_reason
    content=''.join(chunks)
    print(f'STREAM_OK latency={time.time()-t0:.1f}s finish={finish} usage={usage}')
    print('len=',len(content),'json=', end='')
    try: json.loads(content); print('PARSE_OK')
    except Exception as e: print('PARSE_FAIL',e)
    print(content[:600])
except Exception as e:
    print('STREAM_FAIL', type(e).__name__, str(e)[:800])
