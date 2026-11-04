"""Synthetic client only. Does not implement A filter/classifier or call a network."""
from copy import deepcopy
import json


class FakeClient:
    def __init__(self, findings=None, *, error=None, response=None):
        self.findings = findings or []
        self.error = error
        self.response = response
        self.calls = []

    def __call__(self, prompt, *, max_attempts):
        envelope = json.loads(prompt.rsplit("\n\n", 1)[1])
        data = envelope["INPUT"]
        self.calls.append({"context": data, "max_attempts": max_attempts})
        if self.error:
            raise self.error
        if self.response is not None:
            return deepcopy(self.response)
        ids = {r["record_id"] for r in data["RECORDS"]}
        findings = [f for f in self.findings if f.get("record_id") in ids or f.get("record_id") is None]
        return {"text": json.dumps({"findings": findings}, ensure_ascii=False), "attempts": 1, "tokens": 10}
