"""Export adapter: artifact-tool where installed, portable OOXML on plain Python.

The portable backend only replaces data rows in the supplied template's 2025
sheet. It retains every other ZIP part byte-for-byte and needs no Excel/Node.
"""
from decimal import Decimal
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
from zipfile import ZipFile,ZIP_DEFLATED
import xml.etree.ElementTree as ET

from .water_template import FIELDS,NS,ROOT,contract,sheets


def portable_bytes(collection, year=2025):
    if year!=2025:raise ValueError("TEMPLATE_SUPPORTS_2025_ONLY")
    source=Path(collection["contract"]["template_path"])
    spec=contract(source)
    if spec["template_sha256"]!=collection["contract"]["template_sha256"]:raise ValueError("TEMPLATE_CHANGED")
    part=sheets(source)["2025"]["part"]
    with ZipFile(source) as archive:
        source_xml=archive.read(part).decode("utf-8")
        root=ET.fromstring(source_xml); data=root.find("m:sheetData",NS)
        for row in list(data):
            if int(row.attrib["r"])>1:data.remove(row)
        selected=[r for r in collection["rows"] if r["year"]==year]
        tag=lambda x:'{'+NS['m']+'}'+x
        for n,row in enumerate(selected,2):
            element=ET.SubElement(data,tag("row"),{"r":str(n)})
            for i,field in enumerate(FIELDS):
                value=row["values"].get(field)
                if value is None:continue
                cell=ET.SubElement(element,tag("c"),{"r":chr(65+i)+str(n)})
                if i<4 or field=="revenue_currency":
                    cell.set("t","inlineStr"); text=ET.SubElement(ET.SubElement(cell,tag("is")),tag("t"));text.text=str(value)
                else:
                    number=Decimal(str(value))
                    if not number.is_finite():raise ValueError("NONFINITE_CELL")
                    ET.SubElement(cell,tag("v")).text=format(number,"f")
        # Preserve the original namespace declarations, including prefixes named
        # only inside mc:Ignorable, which ElementTree would otherwise discard.
        payload=ET.tostring(data,encoding="unicode")
        sheet_xml,count=re.subn(r"<sheetData\b[^>]*(?:/>|>.*?</sheetData>)",lambda _:payload,source_xml,count=1,flags=re.S)
        if count!=1:raise ValueError("TEMPLATE_SHEET_DATA_UNSUPPORTED")
        sheet_xml=re.sub(r'(<dimension\b[^>]*\bref=")[^"]*(")',lambda m:m[1]+f"A1:O{len(selected)+1}"+m[2],sheet_xml,count=1)
        stream=io.BytesIO()
        policy_xml=None;policy_part=None
        policy=collection.get("research_policy")
        if policy:
            policy_part=next(value["part"] for name,value in sheets(source).items() if name!="2025")
            rules_source=archive.read(policy_part).decode("utf-8")
            rules_root=ET.fromstring(rules_source);rules_data=rules_root.find("m:sheetData",NS)
            for n,(label,text) in enumerate(policy["notes"],18):
                row=ET.SubElement(rules_data,tag("row"),{"r":str(n),"ht":"48","customHeight":"1"})
                for col,value in (("A",label),("F",text)):
                    cell=ET.SubElement(row,tag("c"),{"r":col+str(n),"t":"inlineStr"})
                    ET.SubElement(ET.SubElement(cell,tag("is")),tag("t")).text=value
            rule_payload=ET.tostring(rules_data,encoding="unicode")
            policy_xml=re.sub(r"<sheetData\b[^>]*(?:/>|>.*?</sheetData>)",lambda _:rule_payload,rules_source,count=1,flags=re.S)
            policy_xml=re.sub(r'(<dimension\b[^>]*\bref=")[^"]*(")',lambda m:m[1]+f"A1:F{17+len(policy['notes'])}"+m[2],policy_xml,count=1)
        with ZipFile(stream,"w",ZIP_DEFLATED) as out:
            for item in archive.infolist():
                payload=sheet_xml.encode("utf-8") if item.filename==part else policy_xml.encode("utf-8") if item.filename==policy_part else archive.read(item.filename)
                out.writestr(item,payload)
        return stream.getvalue()


def artifact_environment():
    runtime=Path.home()/".cache/codex-runtimes/codex-primary-runtime/dependencies/node"
    node=shutil.which("node") or (str(runtime/"bin/node.exe") if (runtime/"bin/node.exe").is_file() else None)
    env=os.environ.copy()
    if (runtime/"node_modules/@oai/artifact-tool/package.json").is_file():env["NODE_PATH"]=str(runtime/"node_modules")
    return node,env


def export_workbook(collection, output, *, preview=False, backend="auto"):
    output=Path(output).resolve()
    if output==Path(collection["contract"]["template_path"]).resolve():raise ValueError("REFUSE_OVERWRITE_TEMPLATE")
    output.parent.mkdir(parents=True,exist_ok=True)
    node,env=artifact_environment()
    if backend not in ("auto","artifact","portable"):raise ValueError("INVALID_EXPORT_BACKEND")
    available=False
    if node and backend!="portable":
        check=subprocess.run([node,"-e","require.resolve('@oai/artifact-tool')"],env=env,capture_output=True,timeout=15)
        available=check.returncode==0
    if backend=="artifact" and not available:raise RuntimeError("ARTIFACT_TOOL_UNAVAILABLE")
    if available:
        with tempfile.TemporaryDirectory(prefix="esg_water_") as temp:
            source=Path(temp)/"collection.json";source.write_text(json.dumps(collection,ensure_ascii=False),encoding="utf-8")
            result=subprocess.run([node,str(ROOT/"scripts/export_water_workbook.mjs"),str(source),str(output),"2025","preview" if preview else ""],env=env,capture_output=True,timeout=180)
            if result.returncode:raise RuntimeError("ARTIFACT_EXPORT_FAILED: "+result.stderr.decode("utf-8",errors="replace")[-1400:])
        return output
    output.write_bytes(portable_bytes(collection))
    return output
