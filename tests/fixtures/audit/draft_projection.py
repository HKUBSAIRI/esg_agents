"""TEST ONLY: B document output projection, not A's authoritative public contract."""
from jsonschema import Draft202012Validator


def object_schema(properties):
    return {"type":"object","properties":properties,"required":list(properties),"additionalProperties":False}


S={"type":"string"}; N={"type":["string","null"]}; I={"type":"integer","minimum":0}
BOX={"type":["array","null"],"items":{"type":"number"},"minItems":4,"maxItems":4}
LIST={"type":"array","items":S}
STATUS={"enum":["CONFIRMED","SUSPECT","REJECTED","INSUFFICIENT","NOT_REVIEWED"]}
FINDING=object_schema({
    "schema_version":{"const":"3.0"},"finding_id":S,"audit_run_id":S,"pid":S,"record_id":N,"logical_group_key":N,
    "indicator_id":N,"issue_domain":{"enum":["EXTRACTION","CLASSIFICATION","NORMALIZATION","COVERAGE","FILTER"]},
    "page_no":{"type":["integer","null"],"minimum":1},"bbox":BOX,"source_kind":{"enum":["text","table_cell","figure","page_ocr",None]},
    "code":S,"severity":{"enum":["BLOCK","WARN","INFO"]},"confidence":{"type":"number","minimum":0,"maximum":1},
    "evidence_version_ids":{"type":"array","minItems":1,"items":S},"evidence_span":N,"observed":N,"expected":N,
    "proposed_action":{"enum":["KEEP","REMAP","CORRECT_VALUE","MARK_MISSING","MERGE_DUPLICATE","ESCALATE"]},
    "proposed_value":{"type":["number","null"]},"proposed_variable":N,"reason":{"type":"string","minLength":1,"maxLength":512},
    "model_call_id":N,"review_policy_version":S,"created_at":S})
RESULT=object_schema({"schema_version":{"const":"3.0"},"audit_run_id":S,"pid":S,"record_id":S,"logical_group_key":S,
                      "review_status":STATUS,"finding_ids":LIST,"reviewed_at":S})
OUTCOME=object_schema({"schema_version":{"const":"3.0"},"audit_run_id":S,"pid":S,"review_policy_version":S,
                       "record_results":{"type":"array","items":RESULT},"findings":{"type":"array","items":FINDING},
                       "coverage":{"type":"object"},"stats":{"type":"object"},"status":{"enum":["SUCCEEDED","PARTIAL","FAILED"]},
                       "problems":{"type":"array","items":{"type":"object"}},"created_at":S,
                       "precheck_counts":{"type":"object"},"truncated":{"type":"boolean"}})
REVIEW=object_schema({
    **{k:v for k,v in FINDING["properties"].items() if k in ("schema_version","audit_run_id","pid","finding_id","record_id","indicator_id","page_no","bbox","source_kind","issue_domain","code","severity","observed","expected","evidence_span","proposed_action","proposed_value","proposed_variable","evidence_version_ids")},
    "item_id":S,"evidence_version_id":S,"source_format":{"enum":["v2","v1_numeric_blocks"]},"priority":{"enum":["P0","P1","P2"]},
    "review_status":STATUS,"variable":N,"value":{"type":["number","null"]},"unit":N,"year":{"type":["integer","null"]},"breakdown":N})
SCHEMAS={"AuditFinding":FINDING,"AuditRecordResult":RESULT,"AuditOutcome":OUTCOME,"ReviewItem":REVIEW}


def validate_projection(name,value):
    if name in SCHEMAS:
        Draft202012Validator(SCHEMAS[name]).validate(value)
