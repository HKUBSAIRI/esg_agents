"""Opt-in vision transport and strict parsing. No calls in local mode."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from .numbers import tokens

ROOT = Path(__file__).resolve().parents[2]
PROMPT = ROOT / "skills/extract_all/prompt.md"


def validate_response(value):
    if not isinstance(value, dict) or set(value) != {"page_status", "regions", "warnings"}:
        raise ValueError("VISION_ROOT_SHAPE")
    if value["page_status"] not in ("EXTRACTED", "NO_QUANTITATIVE_FOUND", "PARTIAL"):
        raise ValueError("VISION_STATUS")
    if not isinstance(value["warnings"], list) or any(not isinstance(w,str) for w in value["warnings"]):
        raise ValueError("VISION_WARNINGS")
    if not isinstance(value["regions"], list) or len(value["regions"]) > 100:
        raise ValueError("VISION_REGIONS")
    if value["page_status"] == "NO_QUANTITATIVE_FOUND" and value["regions"]:
        raise ValueError("VISION_CONTRADICTORY_STATUS")
    for region in value["regions"]:
        if not isinstance(region,dict) or region.get("kind") not in ("table", "chart", "infographic"):
            raise ValueError("VISION_REGION_KIND")
        box=region.get("bbox")
        if not isinstance(box,list) or len(box)!=4 or any(type(x) not in (int,float) or not 0<=x<=1000 for x in box) or box[0]>=box[2] or box[1]>=box[3]:
            raise ValueError("VISION_BBOX")
        observations=region.get("observations")
        if not isinstance(observations,list) or len(observations)>3000:
            raise ValueError("VISION_OBSERVATIONS")
        for row in observations:
            if not isinstance(row,dict) or row.get("value_kind") not in ("LABELLED","ESTIMATED","TEXTUAL_ZERO","UNCERTAIN"):
                raise ValueError("VISION_VALUE_KIND")
            for key in ("metric_label","value_text","unit","scope","series","category","evidence_quote"):
                if row.get(key) is not None and (not isinstance(row[key],str) or len(row[key])>4000):
                    raise ValueError("VISION_TEXT_FIELD")
            if row.get("year") is not None and (type(row["year"]) is not int or not 1900<=row["year"]<=2200):
                raise ValueError("VISION_YEAR")
            if row["value_kind"] in ("LABELLED","ESTIMATED") and not tokens(row.get("value_text") or ""):
                raise ValueError("VISION_NON_NUMERIC_VALUE")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


class AliVisionClient:
    endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

    def __init__(self, *, model="qwen3.7-plus", key_file=None, max_calls=0, timeout=120, ledger_path=None):
        if max_calls<1: raise ValueError("Explicit positive API budget required")
        if key_file:
            content=Path(key_file).read_text(encoding="utf-8-sig")
            candidates=set(re.findall(r"sk-[A-Za-z0-9_-]{12,}",content)) or set(re.findall(r"[A-Za-z0-9_.+-]{24,}",content))
            if len(candidates)!=1: raise ValueError("Key file must contain one key")
            self._key=candidates.pop()
        else: self._key=os.environ.get("DASHSCOPE_API_KEY","")
        if not self._key: raise ValueError("Model key is missing")
        self.model,self.max_calls,self.timeout=model,max_calls,timeout
        self.calls,self.total_tokens=0,0
        self.blocked_code=None
        self.ledger_path=Path(ledger_path) if ledger_path else None
        self.context={}
        self.usage_ledger=[]

    def _record(self,value):
        self.usage_ledger.append(dict(value))
        if self.ledger_path:
            self.ledger_path.parent.mkdir(parents=True,exist_ok=True)
            with self.ledger_path.open("a",encoding="utf-8") as stream:
                stream.write(json.dumps(value,ensure_ascii=False,allow_nan=False)+"\n")
                stream.flush();os.fsync(stream.fileno())

    def __call__(self,image_path):
        if self.blocked_code:raise RuntimeError(self.blocked_code)
        if self.calls>=self.max_calls: raise RuntimeError("VISION_BUDGET_EXHAUSTED")
        raw=Path(image_path).read_bytes()
        if len(raw)>8*1024*1024: raise ValueError("Page image exceeds 8 MB")
        prompt=PROMPT.read_text(encoding="utf-8")
        payload={"model":self.model,"messages":[{"role":"user","content":[
            {"type":"image_url","image_url":{"url":"data:image/png;base64,"+base64.b64encode(raw).decode("ascii")}},
            {"type":"text","text":prompt}]}],"stream":False,"temperature":0,"enable_thinking":False,"max_tokens":8192,"response_format":{"type":"json_object"}}
        request=Request(self.endpoint,data=json.dumps(payload,ensure_ascii=False).encode("utf-8"),headers={"Content-Type":"application/json","Authorization":"Bearer "+self._key})
        self.calls+=1
        started=time.monotonic()
        accounting={"attempt_id":uuid.uuid4().hex,"phase":"start","model":self.model,
                    "created_at":datetime.now(timezone.utc).isoformat(),"context":dict(self.context),
                    "image_sha256":hashlib.sha256(raw).hexdigest(),"usage":None,"status":"STARTED"}
        self._record(accounting)
        try:
            with build_opener(NoRedirect).open(request,timeout=self.timeout) as response: result=response.read(8_000_001)
            if len(result)>8_000_000: raise ValueError("VISION_RESPONSE_TOO_LARGE")
            envelope=json.loads(result)
            raw_usage=envelope.get("usage")
            safe_usage={k:v for k,v in (raw_usage.items() if isinstance(raw_usage,dict) else []) if k in ("prompt_tokens","completion_tokens","total_tokens","input_tokens","output_tokens","prompt_tokens_details","completion_tokens_details")}
            accounting.update(usage=safe_usage or None,provider_request_id=envelope.get("id"),status="RESPONSE_RECEIVED")
            usage=safe_usage.get("total_tokens")
            if type(usage) is int and usage>=0:self.total_tokens+=usage
            choice=envelope["choices"][0]
            accounting["finish_reason"]=choice.get("finish_reason")
            if choice.get("finish_reason")!="stop":raise ValueError("VISION_INCOMPLETE_RESPONSE")
            text=choice["message"]["content"].strip()
            if self.ledger_path:
                response_dir=self.ledger_path.parent.parent/"responses"/self.ledger_path.stem
                response_dir.mkdir(parents=True,exist_ok=True)
                (response_dir/(accounting["attempt_id"]+".json")).write_text(json.dumps({"attempt_id":accounting["attempt_id"],"response_text":text,"usage":safe_usage},ensure_ascii=False),encoding="utf-8")
            if text.startswith("```json") and text.endswith("```"):text=text[7:-3]
            parsed=validate_response(json.loads(text))
            accounting["status"]="VALIDATED"
            return {"data":parsed,"usage_tokens":usage,"usage":safe_usage,"attempt_id":accounting["attempt_id"],"request_fingerprint":hashlib.sha256(json.dumps({"image":hashlib.sha256(raw).hexdigest(),"prompt":hashlib.sha256(prompt.encode()).hexdigest(),"model":self.model,"max_tokens":8192},sort_keys=True).encode()).hexdigest()}
        except HTTPError as exc:
            accounting.update(status="HTTP_ERROR",http_status=exc.code)
            if exc.code in (401,403):self.blocked_code="VISION_HTTP_"+str(exc.code)
            raise RuntimeError("VISION_HTTP_"+str(exc.code)) from None
        except (URLError,OSError,TimeoutError):
            accounting["status"]="CONNECTION_FAILED"
            raise RuntimeError("VISION_CONNECTION_FAILED") from None
        except (KeyError,TypeError,IndexError,json.JSONDecodeError):
            accounting["status"]="RESPONSE_INVALID"
            raise ValueError("VISION_RESPONSE_INVALID") from None
        except ValueError as exc:
            accounting["status"]="VALIDATION_FAILED"
            if re.fullmatch(r"VISION_[A-Z_]+",str(exc)):accounting["validation_error_code"]=str(exc)
            raise
        finally:
            accounting.update(phase="end",elapsed_ms=round((time.monotonic()-started)*1000))
            self._record(accounting)
