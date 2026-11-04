"""Read-only PDF tools; only this module imports PyMuPDF, and only when needed."""
from pathlib import Path
import math
import tempfile

from .common import number, unit
from .sampling import mandatory, sampled


def valid_bbox(box):
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(type(x) in (float, int) and math.isfinite(x) for x in box)
            and 0 <= box[0] < box[2] and 0 <= box[1] < box[3])


def page_bounds(pdf_path, page_no):
    try:
        import fitz
        with fitz.open(pdf_path) as pdf:
            if type(page_no) is not int or not 1 <= page_no <= len(pdf):
                return None
            page = pdf[page_no - 1]
            # PDF coordinates are unrotated; keep this space consistent with get_pixmap clip.
            return (0.0, 0.0, page.cropbox.width, page.cropbox.height)
    except Exception:
        return None


def bbox_on_page(box, bounds):
    return valid_bbox(box) and bounds is not None and bounds[0] <= box[0] < box[2] <= bounds[2] and bounds[1] <= box[1] < box[3] <= bounds[3]


def render_pdf_region(pdf_path, page_no, bbox, *, dpi=200, out_dir):
    if not valid_bbox(bbox) or type(dpi) is not int or not 36 <= dpi <= 300:
        return None
    try:
        import fitz
        with fitz.open(pdf_path) as pdf:
            if type(page_no) is not int or not 1 <= page_no <= len(pdf):
                return None
            page = pdf[page_no - 1]
            if not bbox_on_page(bbox, (0.0, 0.0, page.cropbox.width, page.cropbox.height)):
                return None
            page.set_rotation(0)  # In-memory only. Never save or modify the source PDF.
            rect = fitz.Rect(bbox)
            # Bound allocation, even for unusual page dimensions.
            if rect.width * rect.height * (dpi / 72) ** 2 > 16_000_000:
                return None
            pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), clip=rect, alpha=False)
            folder = Path(out_dir)
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix="audit-region-", suffix=".png", dir=folder, delete=False) as handle:
                target = Path(handle.name)
            pixmap.save(target)
            return target
    except Exception:
        return None


def read_region_with_vlm(image_path, prompt, *, llm, timeout_s=60):
    # Client bridge owns network calls/retries; this function never retries.
    if not callable(getattr(llm, "read_region", None)):
        return None
    try:
        return llm.read_region(Path(image_path), prompt, timeout_s=timeout_s)
    except Exception:
        return None


def should_visual_check(record, policy):
    return mandatory(record, policy) and sampled(record["record_id"] + "|visual", policy["visual_sample_rate"])


def compare_visual(observation, record):
    if not isinstance(observation, dict):
        return None
    if not {"value", "unit", "year"}.issubset(observation):
        return None
    observed, expected = number(observation["value"]), number(record.get("value"))
    if observed is None or expected is None or unit(observation["unit"]) is None:
        return None
    return observed == expected and unit(observation["unit"]) == unit(record.get("raw_unit")) and observation["year"] == record.get("year")
