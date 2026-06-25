"""Spare-parts Goods-Received / Goods-Issued views — clean, pluggable, cached.

Data is PLUGGABLE BY IMPORT: drop a matching file into data/ (or
data/spare_parts_imports/) and it is picked up automatically — newest file wins,
env vars override. Nothing is hardcoded to one filename.

Sources
  * Goods Received (Gen PO / GRN):
      Stage 1 sheet "Gen PO in D365 Rev.01"   (e.g. po_list.xlsx / Gen PO D365 Rev.03*.xlsx)
      Stage 2 sheet "Gen PO Stage 2 Rev.00"   (e.g. Gen PO Stage 2 D365 Rev.00.xlsx)
  * Goods Issued (consumption):
      reuses spare_parts_service.build_project_transactions_payload (already parses the
      "Project actual transactions" export: Description "1.00 / Item", Name "WRKO: ASSET",
      Excel serial dates, asset resolution + machine_group).

Category (Production Equipment / Utilities / Unclassified) uses the shared
asset_mapping classifier — authoritative for Goods Issued (via asset_id /
machine_group); best-effort keyword mapping for Goods Received (PO lines have no
asset id, so cost-group / description keywords are used, else Unclassified).

Everything is cached by source-file signature so the Excel files are parsed once.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from asset_mapping import group_to_category

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IMPORT_DIR = DATA_DIR / "spare_parts_imports"
# Records which imported file belongs to which stage. This makes stage tagging
# AUTHORITATIVE (the user imports via a Stage 1 / Stage 2 slot) rather than
# guessing from the filename.
MANIFEST_PATH = IMPORT_DIR / "_gen_po_manifest.json"

# ── Pluggable Gen PO file discovery ───────────────────────────────────────────
_GEN_PO_SHEETS = {
    "Stage 1": ["Gen PO in D365 Rev.01", "Gen PO in D365", "Gen PO"],
    "Stage 2": ["Gen PO Stage 2 Rev.00", "Gen PO Stage 2", "Gen PO"],
}
# Filename patterns per stage (lowercased match). Stage 2 is matched first so a
# "Gen PO Stage 2" file is never mistaken for the Stage 1 source.
_GEN_PO_PATTERNS = {
    "Stage 2": [r"gen\s*po\s*stage\s*2", r"po[_ ]?list[_ ]?stage[_ ]?2", r"stage\s*2.*gen\s*po"],
    "Stage 1": [r"^po[_ ]?list", r"gen\s*po\s*d365", r"gen\s*po(?!\s*stage)", r"gen[_ ]po[_ ]translated"],
}
_GEN_PO_ENV = {"Stage 1": "SPARE_PARTS_PO_STAGE1_PATH", "Stage 2": "SPARE_PARTS_PO_STAGE2_PATH"}
_WORKBOOK_STAGE_CACHE: dict[str, tuple[tuple[int, int], str | None]] = {}

_VIEW_CACHE: dict = {}


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for d in (DATA_DIR, IMPORT_DIR):
        if d.exists():
            files.extend(p for p in d.glob("*.xls*") if p.is_file() and not p.name.startswith("~$"))
    return files


# ── Import manifest (authoritative stage tagging) ─────────────────────────────
def _load_manifest() -> dict:
    try:
        if MANIFEST_PATH.exists():
            return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_manifest(man: dict) -> None:
    try:
        IMPORT_DIR.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(json.dumps(man, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def resolve_gen_po_file(stage: str) -> Path | None:
    """Find the Gen PO source for a stage.

    Priority: explicit import manifest (set by the Stage 1 / Stage 2 import slot)
    → env override → newest filename-matching file in data/.
    """
    man = _load_manifest().get(stage) or {}
    stored = man.get("stored_path")
    if stored and Path(stored).exists():
        return Path(stored)
    env = os.environ.get(_GEN_PO_ENV.get(stage, ""))
    if env and Path(env).exists():
        return Path(env)
    pats = [re.compile(p, re.IGNORECASE) for p in _GEN_PO_PATTERNS.get(stage, [])]
    # Exclude the other stage's pattern so we don't cross-match.
    other = "Stage 1" if stage == "Stage 2" else "Stage 2"
    other_pats = [re.compile(p, re.IGNORECASE) for p in _GEN_PO_PATTERNS.get(other, [])]
    matches = []
    for f in _candidate_files():
        name = f.stem
        inferred_stage = _infer_stage_from_workbook(f)
        if inferred_stage and inferred_stage != stage:
            continue
        if inferred_stage == stage:
            matches.append(f)
            continue
        if any(op.search(name) for op in other_pats) and not any(p.search(name) for p in pats):
            continue
        if any(p.search(name) for p in pats):
            matches.append(f)
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def _infer_stage_from_workbook(path: Path) -> str | None:
    try:
        st = path.stat()
    except OSError:
        return None
    cache_key = str(path.resolve())
    sig = (st.st_mtime_ns, st.st_size)
    cached = _WORKBOOK_STAGE_CACHE.get(cache_key)
    if cached and cached[0] == sig:
        return cached[1]

    def _sheet_norm(value) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

    inferred = None
    try:
        xl = pd.ExcelFile(path)
        sheet_names = {_sheet_norm(name) for name in xl.sheet_names}
        stage2_names = {_sheet_norm(name) for name in _GEN_PO_SHEETS.get("Stage 2", [])}
        stage1_names = {_sheet_norm(name) for name in _GEN_PO_SHEETS.get("Stage 1", [])}
        if sheet_names & stage2_names:
            inferred = "Stage 2"
        elif sheet_names & stage1_names:
            inferred = "Stage 1"
    except Exception:
        inferred = None
    _WORKBOOK_STAGE_CACHE[cache_key] = (sig, inferred)
    return inferred


def _read_gen_po_sheet(path: Path, stage: str) -> pd.DataFrame:
    try:
        xl = pd.ExcelFile(path)
    except Exception:
        return pd.DataFrame()
    for sheet in _GEN_PO_SHEETS.get(stage, []):
        if sheet in xl.sheet_names:
            try:
                return pd.read_excel(path, sheet_name=sheet, header=0)
            except Exception:
                continue
    # Fall back to the first sheet that has a PO-No-like column.
    for sheet in xl.sheet_names:
        try:
            df = pd.read_excel(path, sheet_name=sheet, header=0)
        except Exception:
            continue
        if any("po no" in str(c).lower() for c in df.columns):
            return df
    return pd.DataFrame()


# ── tolerant column access ────────────────────────────────────────────────────
def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _col(df: pd.DataFrame, *candidates) -> str | None:
    cols = {_norm(c): c for c in df.columns}
    for cand in candidates:
        n = _norm(cand)
        if n in cols:
            return cols[n]
    # partial contains
    for cand in candidates:
        n = _norm(cand)
        for k, original in cols.items():
            if n and n in k:
                return original
    return None


def _clean(v) -> str:
    try:
        if v is None or pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return re.sub(r"\s+", " ", str(v)).strip()


def _num(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _date(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        d = pd.to_datetime(v, errors="coerce")
        return d.to_pydatetime() if not pd.isna(d) else None
    except Exception:
        return None


# ── GRN status normalisation ──────────────────────────────────────────────────
def _is_grn_completed(raw_status: str) -> bool:
    s = (raw_status or "").lower()
    return "recive bill and grn" in s or "receive bill and grn" in s or "grn already" in s


def _grn_status_clean(raw_status: str) -> str:
    s = (raw_status or "").lower().strip()
    if not s or s == "nat" or s == "none":
        return "Unknown"
    if _is_grn_completed(s):
        return "Received / GRN Completed"
    if "waitting" in s or "waiting" in s:
        if "transport" in s:
            return "Pending — Awaiting Supplier Delivery"
        if "approve" in s:
            return "Pending — Awaiting PR Approval"
        return "Pending"
    return raw_status.strip()


# ── PO best-effort category (no asset id on PO lines) ─────────────────────────
_PO_CATEGORY_KEYWORDS = [
    ("Utilities", r"cool|refriger|chiller|condens|evapor|boiler|compressor|\bpump\b|water|air ?compress|utilit|hvac|steam|lpg|\bgas\b|electrical|cooling"),
    ("Production Equipment", r"production|\bmachine|conveyor|oven|fryer|bratt|sealer|vacuum|x-?ray|weigh|packing|robot|dicer|slicer|mixer|grinder|chopper|former|filler"),
]

FINANCIAL_TYPE_OPEX = "OPEX"
FINANCIAL_TYPE_CAPEX = "CAPEX"
FINANCIAL_TYPE_EXCLUDED = "Non-engineering / Excluded"
FINANCIAL_TYPE_UNCLASSIFIED = "Unclassified"

FINANCIAL_VIEW_OPEX = "engineering_opex"
FINANCIAL_VIEW_CAPEX = "engineering_capex"
FINANCIAL_VIEW_ALL_ENGINEERING = "all_engineering_po"
FINANCIAL_VIEW_PROCUREMENT = "procurement_reference"

_FINANCIAL_VIEW_LABELS = {
    FINANCIAL_VIEW_OPEX: "Engineering Operational Spend excluding CAPEX",
    FINANCIAL_VIEW_CAPEX: "Engineering CAPEX / Project Spend",
    FINANCIAL_VIEW_ALL_ENGINEERING: "All Engineering PO",
    FINANCIAL_VIEW_PROCUREMENT: "Procurement Reference / All Indirect PO",
}

_CAPEX_PATTERNS = [
    r"\bcapex\b",
    r"building|construction|civil work",
    r"new machine|new equipment|equipment purchase|machine purchase",
    r"major installation|installation project",
    r"facility expansion|expansion",
    r"project asset upgrade|asset upgrade",
    r"renovation|fabrication",
]

_OPEX_PATTERNS = [
    r"\bopex\b",
    r"spare ?parts?|maintenance|repair|service",
    r"labou?r|contractor",
    r"consumable|refrigerant|inspection|cleaning",
    r"chemical|oil|grease|filter|bearing|seal|gasket|belt",
]

_EXCLUDED_PATTERNS = [
    r"packaging|logistics|warehouse",
    r"corporate|general admin|administration|office",
    r"staff welfare|welfare",
    r"rental|lease",
]


def _po_category(group_of_cost: str, sub_cost: str, description: str) -> str:
    text = f"{group_of_cost} {sub_cost} {description}".lower()
    for cat, pat in _PO_CATEGORY_KEYWORDS:
        if re.search(pat, text):
            return cat
    return "Unclassified"


def normalize_financial_view(value) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": FINANCIAL_VIEW_OPEX,
        "all": FINANCIAL_VIEW_OPEX,
        "opex": FINANCIAL_VIEW_OPEX,
        "engineering_opex": FINANCIAL_VIEW_OPEX,
        "engineering_operational_spend_excluding_capex": FINANCIAL_VIEW_OPEX,
        "capex": FINANCIAL_VIEW_CAPEX,
        "engineering_capex": FINANCIAL_VIEW_CAPEX,
        "engineering_capex_project_spend": FINANCIAL_VIEW_CAPEX,
        "all_engineering": FINANCIAL_VIEW_ALL_ENGINEERING,
        "all_engineering_po": FINANCIAL_VIEW_ALL_ENGINEERING,
        "procurement": FINANCIAL_VIEW_PROCUREMENT,
        "procurement_reference": FINANCIAL_VIEW_PROCUREMENT,
        "procurement_reference_all_indirect_po": FINANCIAL_VIEW_PROCUREMENT,
        "all_indirect_po": FINANCIAL_VIEW_PROCUREMENT,
    }
    return aliases.get(raw, FINANCIAL_VIEW_OPEX)


def financial_scope_meta(financial_view=None) -> dict:
    view = normalize_financial_view(financial_view)
    return {
        "view": view,
        "label": _FINANCIAL_VIEW_LABELS.get(view, _FINANCIAL_VIEW_LABELS[FINANCIAL_VIEW_OPEX]),
        "engineering_source_note": "Source: Engineering PO files (Stage 1 and Stage 2 Gen PO)",
        "procurement_source_note": "Source: Indirect PO procurement file (reference only)",
        "consumption_source_note": "Source: Project Actual Transactions",
    }


def classify_financial_type(*values, source_file: str = "") -> str:
    text = " ".join(str(v or "") for v in values + (source_file,)).lower()
    if any(re.search(pat, text) for pat in _EXCLUDED_PATTERNS):
        return FINANCIAL_TYPE_EXCLUDED
    if any(re.search(pat, text) for pat in _CAPEX_PATTERNS):
        return FINANCIAL_TYPE_CAPEX
    if any(re.search(pat, text) for pat in _OPEX_PATTERNS):
        return FINANCIAL_TYPE_OPEX
    return FINANCIAL_TYPE_UNCLASSIFIED


def _financial_view_allows(row: dict, financial_view=None) -> bool:
    view = normalize_financial_view(financial_view)
    ftype = row.get("financial_type") or FINANCIAL_TYPE_UNCLASSIFIED
    if view == FINANCIAL_VIEW_OPEX:
        return ftype == FINANCIAL_TYPE_OPEX
    if view == FINANCIAL_VIEW_CAPEX:
        return ftype == FINANCIAL_TYPE_CAPEX
    if view in (FINANCIAL_VIEW_ALL_ENGINEERING, FINANCIAL_VIEW_PROCUREMENT):
        return ftype != FINANCIAL_TYPE_EXCLUDED
    return True


# ── Goods Received parser ─────────────────────────────────────────────────────
def _parse_goods_received_stage(path: Path, stage: str) -> tuple[list[dict], list[str]]:
    df = _read_gen_po_sheet(path, stage)
    issues: list[str] = []
    if df.empty:
        return [], [f"{stage}: could not read Gen PO sheet from {path.name}"]
    c_pr = _col(df, "PR No.", "PR No")
    c_po = _col(df, "PO No.", "PO No")
    c_dategen = _col(df, "Date Gen PO")
    c_lead = _col(df, "Lead time delivery (day)", "Lead time delivery")
    c_item = _col(df, "Item number", "Item")
    c_group = _col(df, "Group of cost")
    c_sub = _col(df, "Sub-Cost", "Sub Cost")
    c_type = _col(df, "Type of cost")
    c_desc = _col(df, "Description", "TRANSPORTATION/Description", "TRANSPORTATION Description")
    c_qty = _col(df, "Qty'", "Qty", "Quantity")
    c_unit = _col(df, "Unit")
    c_price = _col(df, "Price/Unit", "Price Unit")
    c_total = _col(df, "Total price", "Total Price")
    c_vendor = _col(df, "Vendor name", "Vendor")
    c_machine = _col(df, "PD Machine", "Machine", "Asset")
    c_status = _col(df, "PR PO GRN Status")
    c_grn = _col(df, "GRN No.", "GRN No")
    c_billdate = _col(df, "Date recive bill", "Date receive bill")
    c_grnpo = _col(df, "GRN-PO date (Day)", "GRN-PO date", "GRN PO date")
    c_kpi = _col(df, "KPI Status")
    c_cust = _col(df, "Customer_Group", "Customer Group")  # Stage 2 only
    c_note = _col(df, "Note")

    rows = []
    for _, r in df.iterrows():
        po = _clean(r.get(c_po)) if c_po else ""
        pr = _clean(r.get(c_pr)) if c_pr else ""
        if not po and not pr:
            continue  # skip blank / header artefact rows
        total = _num(r.get(c_total)) if c_total else None
        raw_status = _clean(r.get(c_status)) if c_status else ""
        completed = _is_grn_completed(raw_status)
        kpi = _clean(r.get(c_kpi)) if c_kpi else ""
        grn_po_days = _num(r.get(c_grnpo)) if c_grnpo else None
        dgen = _date(r.get(c_dategen)) if c_dategen else None
        group_of_cost = _clean(r.get(c_group)) if c_group else ""
        sub_cost = _clean(r.get(c_sub)) if c_sub else ""
        type_of_cost = _clean(r.get(c_type)) if c_type else ""
        desc = _clean(r.get(c_desc)) if c_desc else ""
        note = _clean(r.get(c_note)) if c_note else ""
        category = _po_category(group_of_cost, sub_cost, desc)
        financial_type = classify_financial_type(
            group_of_cost,
            sub_cost,
            type_of_cost,
            desc,
            category,
            note,
            source_file=f"{stage} Engineering PO",
        )
        rows.append({
            "stage": stage,
            "pr_no": pr,
            "po_no": po,
            "date_gen_po": dgen.isoformat() if dgen else "",
            "lead_time_days": _num(r.get(c_lead)) if c_lead else None,
            "item_number": _clean(r.get(c_item)) if c_item else "",
            "group_of_cost": group_of_cost,
            "sub_cost": sub_cost,
            "type_of_cost": type_of_cost,
            "financial_type": financial_type,
            "description": desc,
            "qty": _num(r.get(c_qty)) if c_qty else None,
            "unit": _clean(r.get(c_unit)) if c_unit else "",
            "price_unit": _num(r.get(c_price)) if c_price else None,
            "total_price": total,
            "vendor": _clean(r.get(c_vendor)) if c_vendor else "",
            "pd_machine": _clean(r.get(c_machine)) if c_machine else "",
            "grn_status_raw": raw_status,
            "grn_status": _grn_status_clean(raw_status),
            "completed": completed,
            "grn_no": _clean(r.get(c_grn)) if c_grn else "",
            "date_receive_bill": (_date(r.get(c_billdate)).isoformat() if c_billdate and _date(r.get(c_billdate)) else ""),
            "grn_po_days": grn_po_days,
            "kpi_status": kpi,
            "on_time": kpi.lower() == "ontime",
            "over_delivery": "over" in kpi.lower(),
            "customer_group": _clean(r.get(c_cust)) if c_cust else "",
            "note": note,
            "category": category,
            "year": str(dgen.year) if dgen else "",
            "month": f"{dgen.year}-{dgen.month:02d}" if dgen else "",
        })
    return rows, issues


def _gen_po_signature() -> tuple:
    sig = []
    for stage in ("Stage 1", "Stage 2"):
        p = resolve_gen_po_file(stage)
        if p:
            st = p.stat()
            sig.append((stage, p.name, st.st_mtime_ns, st.st_size))
        else:
            sig.append((stage, None))
    return tuple(sig)


def get_goods_received_rows() -> tuple[list[dict], dict]:
    """All Goods-Received rows across stages, cached by file signature."""
    sig = _gen_po_signature()
    cached = _VIEW_CACHE.get("gr")
    if cached and cached["sig"] == sig:
        return cached["rows"], cached["status"]
    rows: list[dict] = []
    status: dict = {}
    for stage in ("Stage 1", "Stage 2"):
        path = resolve_gen_po_file(stage)
        if not path:
            status[stage] = {"available": False, "file_name": None, "message": f"{stage} Gen PO file not found — drop it into data/ to enable."}
            continue
        stage_rows, issues = _parse_goods_received_stage(path, stage)
        rows.extend(stage_rows)
        status[stage] = {"available": True, "file_name": path.name, "row_count": len(stage_rows), "message": f"Loaded {len(stage_rows)} PO lines from {path.name}", "issues": issues}
    _VIEW_CACHE["gr"] = {"sig": sig, "rows": rows, "status": status}
    return rows, status


# ── Gen PO import (Stage 1 / Stage 2 slots) ───────────────────────────────────
# Required = must be present for the row to carry value; missing → import warning
# (page still loads). Recommended = used by KPIs/tables but optional.
_GEN_PO_REQUIRED = ["PO No.", "Total price"]
_GEN_PO_RECOMMENDED = [
    "PR No.", "Date Gen PO", "Vendor name", "PR PO GRN Status", "GRN No.",
    "Date recive bill", "GRN-PO date", "KPI Status", "Description", "Qty",
    "Unit", "Group of cost", "Sub-Cost",
]


def _safe_name(name) -> str:
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", str(name or "")).strip()
    return base or "gen_po_import.xlsx"


def import_gen_po(stage: str, file_storage) -> dict:
    """Import a Gen PO file into a stage slot. Saves it under data/spare_parts_imports/
    and records the manifest so the stage tag is authoritative (not filename-guessed)."""
    if stage not in ("Stage 1", "Stage 2"):
        return {"ok": False, "stage": stage, "message": f"Unknown stage '{stage}'. Use 'Stage 1' or 'Stage 2'."}
    if file_storage is None or not getattr(file_storage, "filename", ""):
        return {"ok": False, "stage": stage, "message": "No file uploaded."}
    orig = _safe_name(file_storage.filename)
    if not orig.lower().endswith((".xlsx", ".xls")):
        return {"ok": False, "stage": stage, "file_name": orig, "message": "Please upload an Excel (.xlsx / .xls) Gen PO file."}
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    slug = "stage1" if stage == "Stage 1" else "stage2"
    stored = IMPORT_DIR / f"gen_po_{slug}__{orig}"
    try:
        file_storage.save(str(stored))
    except Exception as exc:
        return {"ok": False, "stage": stage, "file_name": orig, "message": f"Could not save file: {exc}"}

    df = _read_gen_po_sheet(stored, stage)
    if df.empty:
        try:
            stored.unlink()
        except Exception:
            pass
        return {"ok": False, "stage": stage, "file_name": orig,
                "message": "Could not read a Gen PO sheet from the file. Check the sheet name / layout."}

    missing_required = [c for c in _GEN_PO_REQUIRED if _col(df, c) is None]
    missing_recommended = [c for c in _GEN_PO_RECOMMENDED if _col(df, c) is None]
    # Use df already in hand — avoids a second full Excel read inside _parse_goods_received_stage.
    # The view cache is cleared so the next GET rebuilds from the saved file.
    row_count = len(df)

    man = _load_manifest()
    prev = (man.get(stage) or {}).get("stored_path")
    if prev and Path(prev) != stored and Path(prev).exists() and IMPORT_DIR in Path(prev).parents:
        try:
            Path(prev).unlink()
        except Exception:
            pass
    man[stage] = {
        "file_name": orig,
        "stored_path": str(stored),
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "row_count": row_count,
        "missing_required": missing_required,
        "missing_recommended": missing_recommended,
        "issues": [],
    }
    _save_manifest(man)
    _VIEW_CACHE.pop("gr", None)  # force rebuild on next request

    ok = (not missing_required) and row_count > 0
    msg = f"Imported {row_count} {stage} PO lines from {orig}."
    if missing_required:
        msg += f" Missing required column(s): {', '.join(missing_required)}."
    if missing_recommended:
        msg += f" Optional column(s) not found: {', '.join(missing_recommended)}."
    return {"ok": ok, "stage": stage, "file_name": orig, "row_count": row_count,
            "missing_required": missing_required, "missing_recommended": missing_recommended,
            "imported_at": man[stage]["imported_at"], "message": msg}


def detect_stage_from_name(name: str) -> str | None:
    """For the optional multi-file import: infer stage from a filename."""
    n = (name or "").lower()
    if re.search(r"stage\s*2", n):
        return "Stage 2"
    if re.search(r"gen\s*po\s*d365|rev\.?0?3|po[_ ]?list|gen\s*po(?!\s*stage)", n):
        return "Stage 1"
    return None


def get_import_status() -> dict:
    """Per-source import status for the Spare Parts page import panel."""
    man = _load_manifest()
    out: dict = {}
    for stage in ("Stage 1", "Stage 2"):
        path = resolve_gen_po_file(stage)
        entry = man.get(stage) or {}
        from_import = bool(entry.get("stored_path") and Path(entry["stored_path"]).exists())
        out[stage] = {
            "uploaded": path is not None,
            "file_name": entry.get("file_name") or (path.name if path else None),
            "imported_at": entry.get("imported_at"),
            "row_count": entry.get("row_count"),
            "source": "import" if from_import else ("auto-discovered" if path else None),
            "missing_required": entry.get("missing_required") or [],
            "missing_recommended": entry.get("missing_recommended") or [],
            "issues": entry.get("issues") or [],
        }
    try:
        import spare_parts_service as sp
        gi_status = sp.get_project_transactions_import_status()
    except Exception as exc:  # pragma: no cover - defensive
        gi_status = {"uploaded": False, "message": str(exc)}
    out["Goods Issued"] = {
        "uploaded": bool(gi_status.get("uploaded")),
        "file_name": gi_status.get("file_name"),
        "imported_at": gi_status.get("imported_at"),
        "transaction_count": gi_status.get("transaction_count"),
        "message": gi_status.get("message"),
    }
    inv_path = DATA_DIR / "spare_parts_master.xlsx"
    out["Inventory"] = {
        "uploaded": inv_path.exists(),
        "file_name": inv_path.name if inv_path.exists() else None,
        "imported_at": datetime.fromtimestamp(inv_path.stat().st_mtime).isoformat(timespec="seconds") if inv_path.exists() else None,
        "message": "Inventory master present" if inv_path.exists() else "No inventory file imported yet.",
    }
    try:
        import indirect_po_service as ipo
        out["Indirect PO"] = ipo.get_indirect_po_import_status()
    except Exception:
        out["Indirect PO"] = {"uploaded": False, "message": "Indirect PO module not available."}
    return out


# ── Goods Issued (reuse the existing consumption parser) ──────────────────────
def get_goods_issued_rows() -> tuple[list[dict], dict]:
    import spare_parts_service as sp
    payload = sp.build_project_transactions_payload()
    txs = payload.get("transactions") or []
    rows = []
    for t in txs:
        asset_id = t.get("resolved_asset_id") or t.get("asset_id") or ""
        machine_group = t.get("machine_group") or ""
        category = group_to_category(machine_group) if machine_group else "Unclassified"
        pd_raw = t.get("project_date") or ""
        year = str(pd_raw)[:4] if pd_raw else ""
        month = str(pd_raw)[:7] if pd_raw else ""
        rows.append({
            "project": t.get("project") or "",
            "work_order_id": t.get("work_order_id") or "",
            "asset_id": asset_id,
            "asset_name": t.get("resolved_asset_name") or t.get("equipment_name") or "",
            "machine_group": machine_group,
            "category": category,
            "project_date": pd_raw,
            "transaction_id": t.get("transaction_id") or "",
            "line_property": t.get("line_property") or "",
            "issued_qty": t.get("quantity_used"),
            "item_description": t.get("part_name") or t.get("clean_description") or t.get("translated_description") or t.get("original_description") or "",
            "total_consumption": t.get("total_consumption"),
            "year": year,
            "month": month,
        })
    status = {"available": payload.get("status") == "ok", "transaction_count": len(rows), "message": payload.get("error") or f"Loaded {len(rows)} consumption transactions"}
    return rows, status


# ── filtering ─────────────────────────────────────────────────────────────────
def _apply_filters(rows: list[dict], stage=None, category=None, year=None, month=None, financial_view=None) -> list[dict]:
    def keep(r):
        if stage and stage not in ("", "all", "All Stages") and r.get("stage") and r["stage"] != stage:
            return False
        if category and category not in ("", "all", "All") and r.get("category") != category:
            return False
        if year and year not in ("", "all") and r.get("year") != str(year):
            return False
        if month and month not in ("", "all") and r.get("month") != str(month):
            return False
        if financial_view and not _financial_view_allows(r, financial_view):
            return False
        return True
    return [r for r in rows if keep(r)]


def _top(rows, key_fn, val_fn, n=10):
    agg = {}
    for r in rows:
        k = key_fn(r)
        if not k:
            continue
        agg[k] = agg.get(k, 0.0) + (val_fn(r) or 0.0)
    return [{"label": k, "value": round(v, 2)} for k, v in sorted(agg.items(), key=lambda x: -x[1])[:n]]


def _monthly(rows, val_fn):
    agg = {}
    for r in rows:
        m = r.get("month")
        if not m:
            continue
        agg[m] = agg.get(m, 0.0) + (val_fn(r) or 0.0)
    return [{"month": m, "value": round(v, 2)} for m, v in sorted(agg.items())]


# ── public builders (cached/efficient) ───────────────────────────────────────
def build_goods_received(stage=None, category=None, year=None, month=None, financial_view=None) -> dict:
    all_rows, status = get_goods_received_rows()
    rows = _apply_filters(all_rows, stage, category, year, month, financial_view)
    completed = [r for r in rows if r["completed"]]
    pending = [r for r in rows if not r["completed"]]
    valid_kpi = [r for r in rows if r["kpi_status"]]
    total_po = sum(r["total_price"] or 0 for r in rows)
    # GRN-PO days: only sane delivery gaps (0..730d). Negative / huge values are
    # source data errors (GRN dated before PO, serials, etc.) — exclude from the average.
    grn_days = [r["grn_po_days"] for r in completed if r["grn_po_days"] is not None and 0 <= r["grn_po_days"] <= 730]
    missing_grn_no = sum(1 for r in completed if not r["grn_no"])
    missing_date = sum(1 for r in rows if not r["date_gen_po"])
    missing_kpi = sum(1 for r in rows if not r["kpi_status"])
    missing_bill_date = sum(1 for r in completed if not r["date_receive_bill"])
    missing_total = sum(1 for r in rows if r["total_price"] is None)
    missing_stage = sum(1 for r in rows if not r.get("stage"))
    unclassified = sum(1 for r in rows if r.get("category") == "Unclassified")
    missing_vendor = sum(1 for r in rows if not r.get("vendor"))
    missing_item_number = sum(1 for r in rows if not r.get("item_number"))
    return {
        "kpis": {
            "total_po_value": round(total_po, 2),
            "received_value": round(sum(r["total_price"] or 0 for r in completed), 2),
            "pending_value": round(sum(r["total_price"] or 0 for r in pending), 2),
            "po_lines": len(rows),
            "received_lines": len(completed),
            "pending_lines": len(pending),
            "on_time_pct": round(100 * sum(1 for r in valid_kpi if r["on_time"]) / len(valid_kpi), 1) if valid_kpi else None,
            "over_delivery_count": sum(1 for r in rows if r["over_delivery"]),
            "avg_grn_po_days": round(sum(grn_days) / len(grn_days), 1) if grn_days else None,
        },
        "monthly_po_value": _monthly(rows, lambda r: r["total_price"]),
        "monthly_received_value": _monthly(completed, lambda r: r["total_price"]),
        "monthly_pending_value": _monthly(pending, lambda r: r["total_price"]),
        "status_breakdown": _top(rows, lambda r: r["grn_status"], lambda r: 1, n=12),
        "top_vendors": _top(rows, lambda r: r["vendor"], lambda r: r["total_price"]),
        "top_cost_groups": _top(rows, lambda r: r["group_of_cost"], lambda r: r["total_price"]),
        "rows": rows,
        "source_status": status,
        "financial_scope": financial_scope_meta(financial_view),
        "data_quality": {
            "missing_grn_number": missing_grn_no,
            "missing_date": missing_date,
            "missing_date_receive_bill": missing_bill_date,
            "missing_received_date": missing_bill_date,
            "missing_kpi_status": missing_kpi,
            "missing_total_price": missing_total,
            "missing_vendor": missing_vendor,
            "missing_item_number": missing_item_number,
            "missing_stage": missing_stage,
            "pending_grn_count": len(pending),
            "unclassified_count": unclassified,
        },
    }


def _is_non_billable(line_property) -> bool:
    """Non_Bill = internal / non-billable cost classification. It is NOT the
    definition of 'goods issued' — it only flags the transaction's billing type."""
    return "nonbill" in _norm(line_property)


def build_goods_issued(stage=None, category=None, year=None, month=None) -> dict:
    all_rows, status = get_goods_issued_rows()
    import spare_parts_service as sp
    pt_payload = sp.build_project_transactions_payload()
    rows = _apply_filters(all_rows, None, category, year, month)  # consumption has no stage dimension
    total_val = sum(r["total_consumption"] or 0 for r in rows)
    total_qty = sum(r["issued_qty"] or 0 for r in rows)
    non_billable_val = sum(r["total_consumption"] or 0 for r in rows if _is_non_billable(r.get("line_property")))
    no_qty = sum(1 for r in rows if r["issued_qty"] is None)
    no_asset = sum(1 for r in rows if not r["asset_id"])
    no_date = sum(1 for r in rows if not r.get("project_date"))
    no_total = sum(1 for r in rows if r["total_consumption"] is None)
    unclassified = sum(1 for r in rows if r.get("category") == "Unclassified")
    payload_summary = pt_payload.get("summary") or {}
    cat_val = {}
    for r in rows:
        cat_val[r["category"]] = cat_val.get(r["category"], 0.0) + (r["total_consumption"] or 0)
    top_assets = _top(rows, lambda r: r["asset_name"] or r["asset_id"], lambda r: r["total_consumption"])
    top_groups = _top(rows, lambda r: r["machine_group"], lambda r: r["total_consumption"])
    return {
        "kpis": {
            "total_issued_value": round(total_val, 2),
            "issue_transactions": len(rows),
            "total_issued_qty": round(total_qty, 2),
            "internal_non_billable_value": round(non_billable_val, 2),
            "top_consuming_asset": top_assets[0]["label"] if top_assets else None,
            "top_consuming_machine_group": top_groups[0]["label"] if top_groups else None,
        },
        "monthly_issued_value": _monthly(rows, lambda r: r["total_consumption"]),
        "top_items_by_value": _top(rows, lambda r: r["item_description"], lambda r: r["total_consumption"]),
        "top_items_by_qty": _top(rows, lambda r: r["item_description"], lambda r: r["issued_qty"]),
        "top_assets": top_assets,
        "top_machine_groups": top_groups,
        "consumption_by_category": [{"label": k, "value": round(v, 2)} for k, v in sorted(cat_val.items(), key=lambda x: -x[1])],
        "rows": rows,
        "source_status": status,
        "data_quality": {
            "rows_without_qty": no_qty,
            "rows_without_asset": no_asset,
            "rows_without_date": no_date,
            "rows_without_total": no_total,
            "rows_without_work_order": payload_summary.get("missing_work_order_reference_rows", 0),
            "non_item_rows": payload_summary.get("non_item_rows", 0),
            "unclassified_asset_count": unclassified,
        },
    }


def build_overview(stage=None, category=None, year=None, month=None, financial_view=None) -> dict:
    gr = build_goods_received(stage, category, year, month, financial_view)
    gi = build_goods_issued(stage, category, year, month)
    import spare_parts_service as sp
    spare_payload = sp.build_spare_parts_payload()
    inventory_rows = (spare_payload.get("inventory") or {}).get("records") or []
    inventory_missing_qty = sum(1 for row in inventory_rows if row.get("current_quantity") is None)
    inventory_missing_value = sum(1 for row in inventory_rows if row.get("stock_value") is None and row.get("unit_cost") is None)
    unclassified_po_rows = ((spare_payload.get("po_classification") or {}).get("summary") or {}).get("other_unclassified_po_count", 0)
    comp_summary = (spare_payload.get("comparison") or {}).get("summary") or {}
    po_class_summary = (spare_payload.get("po_classification") or {}).get("summary") or {}
    item_group_counts: dict[str, int] = {}
    for _row in inventory_rows:
        if float(_row.get("current_quantity") or 0) > 0:
            _grp = str(_row.get("item_group") or "Unclassified").strip()
            item_group_counts[_grp] = item_group_counts.get(_grp, 0) + 1
    top_vendor = gr["top_vendors"][0] if gr["top_vendors"] else None
    top_cost = gr["top_cost_groups"][0] if gr["top_cost_groups"] else None
    top_item = gi["top_items_by_value"][0] if gi["top_items_by_value"] else None

    # Indirect PO procurement reconciliation KPIs (lightweight — uses cached reconciliation)
    try:
        import indirect_po_service as ipo
        procurement_kpis = ipo.get_procurement_kpis_for_overview(
            year=year,
            month=month,
            stage=stage,
            category=category,
            financial_view=financial_view,
        )
    except Exception:
        procurement_kpis = {"available": False}

    return {
        # Section A — Goods Received / Purchasing (Gen PO Stage 1 & 2)
        "goods_received_kpis": {
            "total_po_value": gr["kpis"]["total_po_value"],
            "received_value": gr["kpis"]["received_value"],
            "pending_value": gr["kpis"]["pending_value"],
            "po_lines": gr["kpis"]["po_lines"],
            "received_lines": gr["kpis"]["received_lines"],
            "pending_lines": gr["kpis"]["pending_lines"],
            "avg_grn_po_days": gr["kpis"]["avg_grn_po_days"],
            "on_time_pct": gr["kpis"]["on_time_pct"],
            "top_vendor": top_vendor["label"] if top_vendor else None,
            "top_cost_group": top_cost["label"] if top_cost else None,
        },
        # Section B — Goods Issued / Consumption (Project Actual Transactions)
        "goods_issued_kpis": {
            "total_issued_value": gi["kpis"]["total_issued_value"],
            "issue_transactions": gi["kpis"]["issue_transactions"],
            "total_issued_qty": gi["kpis"]["total_issued_qty"],
            "internal_non_billable_value": gi["kpis"]["internal_non_billable_value"],
            "top_consumed_part": top_item["label"] if top_item else None,
            "top_consuming_asset": gi["kpis"]["top_consuming_asset"],
            "top_consuming_machine_group": gi["kpis"]["top_consuming_machine_group"],
        },
        # Flat KPI bag kept for backward compatibility with the older Overview grid.
        "kpis": {
            **gr["kpis"],
            "total_issued_value": gi["kpis"]["total_issued_value"],
            "issue_transactions": gi["kpis"]["issue_transactions"],
            "internal_non_billable_value": gi["kpis"]["internal_non_billable_value"],
            "top_consumed_part": top_item["label"] if top_item else None,
            "top_consuming_asset": gi["kpis"]["top_consuming_asset"],
            "top_consuming_machine_group": gi["kpis"]["top_consuming_machine_group"],
            "top_vendor": top_vendor["label"] if top_vendor else None,
            "top_cost_group": top_cost["label"] if top_cost else None,
        },
        "goods_received_status": gr["source_status"],
        "goods_issued_status": gi["source_status"],
        "import_status": get_import_status(),
        "data_quality": {
            **gr["data_quality"],
            **gi["data_quality"],
            "on_hand_missing_quantity": inventory_missing_qty,
            "on_hand_missing_unit_cost_or_value": inventory_missing_value,
            "unclassified_po_rows": unclassified_po_rows,
        },
        "filters_applied": {
            "stage": stage or "all",
            "category": category or "all",
            "year": year or "all",
            "month": month or "all",
            "financial_view": normalize_financial_view(financial_view),
        },
        "financial_scope": financial_scope_meta(financial_view),
        "procurement_kpis": procurement_kpis,
        # Section C — Current inventory KPIs (On-hand list; not stage-filtered)
        "inventory_kpis": {
            "in_stock_items": comp_summary.get("in_stock_items"),
            "current_inventory_value": comp_summary.get("current_inventory_value"),
            "unvalued_in_stock_items": comp_summary.get("unvalued_in_stock_items"),
            "valued_in_stock_items": comp_summary.get("valued_in_stock_items"),
            "inventory_valuation_coverage_pct": comp_summary.get("inventory_valuation_coverage_pct"),
            "item_group_breakdown": item_group_counts,
        },
        # Section D — Gen PO category split (Stock spare / Non-stock / Services)
        "po_category_kpis": {
            "stocked_spare_part_po_value": po_class_summary.get("stocked_spare_part_po_value"),
            "stocked_spare_part_po_count": po_class_summary.get("stocked_spare_part_po_count"),
            "non_stock_spare_part_po_value": po_class_summary.get("non_stock_spare_part_po_value"),
            "non_stock_spare_part_po_count": po_class_summary.get("non_stock_spare_part_po_count"),
            "service_labour_repair_po_value": po_class_summary.get("service_labour_repair_po_value"),
            "service_labour_repair_po_count": po_class_summary.get("service_labour_repair_po_count"),
        },
    }


# ── Item & Vendor analysis ────────────────────────────────────────────────────
def _norm_item(s) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def build_item_vendor_analysis(stage=None, category=None, year=None, month=None, financial_view=None) -> dict:
    """Purchased vs issued item view + vendor + critical-spare signals.

    Matching is conservative: descriptions are lowercased / de-punctuated and only
    matched on an exact normalised key. Low-confidence pairs stay unmatched.
    """
    gr = build_goods_received(stage, category, year, month, financial_view)
    gi = build_goods_issued(stage, category, year, month)

    purchased: dict = {}
    for r in gr["rows"]:
        key = _norm_item(r.get("description"))
        if len(key) < 4:
            continue
        b = purchased.setdefault(key, {"label": r.get("description") or key, "value": 0.0, "lines": 0})
        b["value"] += r.get("total_price") or 0
        b["lines"] += 1

    issued: dict = {}
    for r in gi["rows"]:
        key = _norm_item(r.get("item_description"))
        if len(key) < 4:
            continue
        b = issued.setdefault(key, {"label": r.get("item_description") or key, "value": 0.0, "qty": 0.0, "count": 0})
        b["value"] += r.get("total_consumption") or 0
        b["qty"] += r.get("issued_qty") or 0
        b["count"] += 1

    top_purchased = sorted(purchased.values(), key=lambda x: -x["value"])[:15]
    top_issued = sorted(issued.values(), key=lambda x: -x["value"])[:15]

    matched = []
    for key, p in purchased.items():
        if key in issued:
            i = issued[key]
            matched.append({"item": p["label"], "purchased_value": round(p["value"], 2), "issued_value": round(i["value"], 2)})
    matched = sorted(matched, key=lambda x: -(x["purchased_value"] + x["issued_value"]))[:20]

    high_low = []
    for key, p in purchased.items():
        iv = issued.get(key, {}).get("value", 0.0)
        if p["value"] > 50000 and iv < p["value"] * 0.1:
            high_low.append({"item": p["label"], "purchased_value": round(p["value"], 2), "issued_value": round(iv, 2)})
    high_low = sorted(high_low, key=lambda x: -x["purchased_value"])[:15]

    repeated = [{"item": b["label"], "transactions": b["count"], "issued_value": round(b["value"], 2)}
                for b in issued.values() if b["count"] >= 3]
    repeated = sorted(repeated, key=lambda x: (-x["transactions"], -x["issued_value"]))[:15]

    crit: dict = {}
    for key, b in issued.items():
        reasons = []
        if b["count"] >= 3:
            reasons.append("repeated usage")
        if b["value"] >= 100000:
            reasons.append("high consumption value")
        pv = purchased.get(key, {}).get("value", 0.0)
        if pv >= 200000:
            reasons.append("high purchase value")
        if reasons:
            crit[key] = {"item": b["label"], "issued_value": round(b["value"], 2), "purchased_value": round(pv, 2),
                         "transactions": b["count"], "reasons": ", ".join(reasons)}
    for key, p in purchased.items():
        if key in crit:
            continue
        if p["value"] >= 300000:
            i = issued.get(key, {})
            crit[key] = {"item": p["label"], "issued_value": round(i.get("value", 0.0), 2),
                         "purchased_value": round(p["value"], 2), "transactions": i.get("count", 0),
                         "reasons": "high purchase value"}
    critical = sorted(crit.values(), key=lambda x: -(x["purchased_value"] + x["issued_value"]))[:20]

    return {
        "top_purchased_items": [{"label": x["label"], "value": round(x["value"], 2)} for x in top_purchased],
        "top_issued_items": [{"label": x["label"], "value": round(x["value"], 2)} for x in top_issued],
        "top_vendors": gr["top_vendors"],
        "purchased_vs_issued": matched,
        "high_purchase_low_issue": high_low,
        "repeated_consumption": repeated,
        "possible_critical_spares": critical,
        "filters_applied": {
            "stage": stage or "all",
            "category": category or "all",
            "year": year or "all",
            "month": month or "all",
            "financial_view": normalize_financial_view(financial_view),
        },
        "financial_scope": financial_scope_meta(financial_view),
    }
