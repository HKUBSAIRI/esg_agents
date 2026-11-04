from pathlib import Path

import fitz
import pytest

from builders import case
from fake_client import FakeClient
from esg_semantic.audit.common import resources
from esg_semantic.audit.visual import (bbox_on_page, compare_visual, page_bounds, read_region_with_vlm,
                                      render_pdf_region, should_visual_check, valid_bbox)
from test_auditor_checks import run_case, codes


def pdf_fixture(path, *, rotation=0, crop=False):
    # Synthetic test asset, not a report deliverable. Never touches any user's PDF.
    with fitz.open() as pdf:
        page=pdf.new_page(width=300,height=200)
        page.insert_text((45,65),"Water withdrawal 1,234 m3 / 2025",fontsize=10)
        if crop: page.set_cropbox(fitz.Rect(10,10,290,190))
        page.set_rotation(rotation)
        pdf.save(path)
    return path


def v2_case(path, value=1234):
    data=case(); data["document"].update(pdf_path=str(path),source_format="v2",page_count=1)
    data["records"][0].update(source_page=1,raw_value=str(value),value=value,normalized_value=value)
    data["evidence"][0].update(page_no=1,source_format="v2",bbox=[20,30,280,90],source_ref={"table_id":"table1"},
                              content=f"2025集团总取水量 {value} m3",numbers=[value])
    return data


@pytest.mark.parametrize("rotation,crop",[(0,False),(90,False),(0,True),(90,True)])
def test_real_pdf_crop_unrotated_coordinates_and_source_immutable(tmp_path,rotation,crop):
    source=pdf_fixture(tmp_path/"test.pdf",rotation=rotation,crop=crop)
    before=source.read_bytes()
    box=[20,30,260,90]
    image=render_pdf_region(source,1,box,dpi=72,out_dir=tmp_path)
    assert image and image.is_file()
    pix=fitz.Pixmap(image)
    assert (pix.width,pix.height)==(240,60)
    assert source.read_bytes()==before
    assert page_bounds(source,1)==(0,0,280,180) if crop else page_bounds(source,1)==(0,0,300,200)


def test_TC_A_22_visual_value_mismatch(tmp_path):
    source=pdf_fixture(tmp_path/"test.pdf")
    data=v2_case(source,value=12345)
    observations=[]
    def reader(image,*,record,max_attempts):
        assert image.is_file(); observations.append(record["record_id"])
        return {"value":1234,"unit":"m3","year":2025}
    result=run_case(data,updates={"visual_sample_rate":1,"max_model_calls":20},visual_reader=reader)
    assert "A-EXTRACT-VALUE" in codes(result)
    assert result["stats"]["visual_checked"]==1
    assert result["stats"]["model_calls"]==2
    assert observations==["rec_b_1"]
    finding=next(f for f in result["findings"] if f["code"]=="A-EXTRACT-VALUE")
    assert finding["bbox"]==[20,30,280,90]
    assert finding["page_no"]==1


def test_visual_agrees_and_v1_never_calls_tool(tmp_path):
    source=pdf_fixture(tmp_path/"test.pdf")
    calls=[]
    def reader(image,*,record,max_attempts):
        calls.append(1); return {"value":1234,"unit":"m³","year":2025}
    result=run_case(v2_case(source),updates={"visual_sample_rate":1,"max_model_calls":20},visual_reader=reader)
    assert "A-EXTRACT-VALUE" not in codes(result)
    assert result["record_results"][0]["review_status"]=="CONFIRMED"
    run_case(case(),updates={"visual_sample_rate":1,"max_model_calls":20},visual_reader=reader)
    assert len(calls)==1


def test_visual_missing_client_does_not_claim_visual_success(tmp_path):
    result=run_case(v2_case(pdf_fixture(tmp_path/"test.pdf")),updates={"visual_sample_rate":1,"max_model_calls":20})
    assert result["stats"]["visual_checked"]==0
    assert any(p["error_code"]=="VISUAL_CLIENT_UNAVAILABLE" for p in result["problems"])


def test_visual_budget_reserved_and_failures(tmp_path):
    data=v2_case(pdf_fixture(tmp_path/"test.pdf"))
    def reader(*args,**kwargs): raise RuntimeError("do not log this secret")
    result=run_case(data,updates={"visual_sample_rate":1,"max_model_calls":1},visual_reader=reader)
    assert result["stats"]["model_calls"]==1
    assert any(p["error_code"]=="VISUAL_BUDGET_RESERVED" for p in result["problems"])
    result=run_case(data,updates={"visual_sample_rate":1,"max_model_calls":20},visual_reader=reader)
    assert any(p["error_code"]=="VISUAL_CHECK_FAILED" for p in result["problems"])
    assert "do not log this secret" not in str(result)
    result=run_case(data,updates={"visual_sample_rate":1,"max_model_calls":20},visual_reader=reader,renderer=lambda *a,**k:None)
    assert result["stats"]["visual_checked"]==0


@pytest.mark.parametrize("box",[None,[],[0,0,0,1],[-1,0,1,1],[0,0,float("inf"),1],[True,0,10,10]])
def test_invalid_bbox(box,tmp_path):
    assert not valid_bbox(box)
    assert render_pdf_region("missing.pdf",1,box,out_dir=tmp_path) is None


def test_pdf_tools_fail_closed(tmp_path):
    source=pdf_fixture(tmp_path/"test.pdf")
    assert page_bounds(source,99) is None
    assert page_bounds(tmp_path/"missing.pdf",1) is None
    assert render_pdf_region(source,99,[0,0,100,100],out_dir=tmp_path) is None
    assert render_pdf_region(source,1,[0,0,400,400],out_dir=tmp_path) is None
    assert render_pdf_region(source,1,[0,0,100,100],dpi=1000,out_dir=tmp_path) is None
    assert render_pdf_region(tmp_path/"missing.pdf",1,[0,0,100,100],out_dir=tmp_path) is None
    assert not bbox_on_page([0,0,10,10],None)


def test_low_level_vlm_reader_injection(tmp_path):
    class Client:
        def read_region(self,*args,**kwargs): return {"value":1}
    class Failing:
        def read_region(self,*args,**kwargs): raise TimeoutError()
    assert read_region_with_vlm(tmp_path/"x.png","test",llm=Client())=={"value":1}
    assert read_region_with_vlm(tmp_path/"x.png","test",llm=Failing()) is None
    assert read_region_with_vlm(tmp_path/"x.png","test",llm=None) is None


@pytest.mark.parametrize("value",[None,"unknown",{}, {"value":None,"unit":"m3","year":2025},{"value":1234,"unit":None,"year":2025}])
def test_unusable_visual_observation_is_unknown(value):
    assert compare_visual(value,case()["records"][0]) is None


def test_visual_empty_invalid_pdf_and_bbox_bounds(tmp_path):
    invalid=tmp_path/"not.pdf"; invalid.write_bytes(b"%PDF-invalid")
    data=v2_case(invalid)
    result=run_case(data)
    assert result["record_results"][0]["review_status"]=="INSUFFICIENT"
    source=pdf_fixture(tmp_path/"good.pdf")
    data=v2_case(source); data["evidence"][0]["bbox"]=[0,0,400,400]
    assert "A-EXTRACT-PAGE" in codes(run_case(data))
