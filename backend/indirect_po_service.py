"""Indirect PO service — reconciliation layer between official Procurement records
(Indirect PO D365 export) and Engineering classification copies (Gen PO Stage 1/2).

The Indirect PO is treated as the OFFICIAL purchasing reference for:
  * PO number, PR number, vendor, quantity, unit price, net amount

Engineering Gen PO files are treated as the ENGINEERING CLASSIFICATION layer:
  * dashboard grouping, spare-parts/service/equipment categorisation

Reconciliation compares the two sources and flags differences without
auto-correcting the Engineering classification.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IMPORT_DIR = DATA_DIR / "spare_parts_imports"
_MANIFEST_PATH = IMPORT_DIR / "_indirect_po_manifest.json"
_INDIRECT_PO_ENV = "INDIRECT_PO_PATH"

_INDIRECT_PO_PATTERNS = [
    r"indirect\s*po",
    r"indirect[_ ]purchase",
    r"procurement[_ ]indirect",
]

_ROWS_CACHE: dict = {}
_RECON_CACHE: dict = {}

FINANCIAL_TYPE_OPEX = "OPEX"
FINANCIAL_TYPE_CAPEX = "CAPEX"
FINANCIAL_TYPE_EXCLUDED = "Non-engineering / Excluded"
FINANCIAL_TYPE_UNCLASSIFIED = "Unclassified"

FINANCIAL_VIEW_OPEX = "engineering_opex"
FINANCIAL_VIEW_CAPEX = "engineering_capex"
FINANCIAL_VIEW_ALL_ENGINEERING = "all_engineering_po"
FINANCIAL_VIEW_PROCUREMENT = "procurement_reference"

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


# ── File discovery ────────────────────────────────────────────────────────────

def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for d in (DATA_DIR, IMPORT_DIR):
        if d.exists():
            files.extend(p for p in d.glob("*.xls*") if p.is_file() and not p.name.startswith("~$"))
    return files


def find_indirect_po_file() -> Path | None:
    man = _load_manifest()
    stored = man.get("stored_path")
    if stored and Path(stored).exists():
        return Path(stored)
    env = os.environ.get(_INDIRECT_PO_ENV, "")
    if env and Path(env).exists():
        return Path(env)
    pats = [re.compile(p, re.IGNORECASE) for p in _INDIRECT_PO_PATTERNS]
    matches = [f for f in _candidate_files() if any(p.search(f.stem) for p in pats)]
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def _load_manifest() -> dict:
    try:
        if _MANIFEST_PATH.exists():
            return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_manifest(man: dict) -> None:
    try:
        IMPORT_DIR.mkdir(parents=True, exist_ok=True)
        _MANIFEST_PATH.write_text(json.dumps(man, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


# ── Tolerant column access ────────────────────────────────────────────────────

def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _col(df: pd.DataFrame, *candidates) -> str | None:
    cols = {_norm(c): c for c in df.columns}
    for cand in candidates:
        n = _norm(cand)
        if n in cols:
            return cols[n]
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


def _normalize_financial_view(value) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": FINANCIAL_VIEW_OPEX,
        "all": FINANCIAL_VIEW_OPEX,
        "opex": FINANCIAL_VIEW_OPEX,
        "engineering_opex": FINANCIAL_VIEW_OPEX,
        "capex": FINANCIAL_VIEW_CAPEX,
        "engineering_capex": FINANCIAL_VIEW_CAPEX,
        "all_engineering": FINANCIAL_VIEW_ALL_ENGINEERING,
        "all_engineering_po": FINANCIAL_VIEW_ALL_ENGINEERING,
        "procurement": FINANCIAL_VIEW_PROCUREMENT,
        "procurement_reference": FINANCIAL_VIEW_PROCUREMENT,
        "all_indirect_po": FINANCIAL_VIEW_PROCUREMENT,
        "procurement_reference_all_indirect_po": FINANCIAL_VIEW_PROCUREMENT,
    }
    return aliases.get(raw, FINANCIAL_VIEW_OPEX)


def _classify_financial_type(*values, source_file: str = "") -> str:
    text = " ".join(str(v or "") for v in values + (source_file,)).lower()
    if any(re.search(pat, text) for pat in _EXCLUDED_PATTERNS):
        return FINANCIAL_TYPE_EXCLUDED
    if any(re.search(pat, text) for pat in _CAPEX_PATTERNS):
        return FINANCIAL_TYPE_CAPEX
    if any(re.search(pat, text) for pat in _OPEX_PATTERNS):
        return FINANCIAL_TYPE_OPEX
    return FINANCIAL_TYPE_UNCLASSIFIED


def _financial_view_allows(row: dict, financial_view=None) -> bool:
    view = _normalize_financial_view(financial_view)
    ftype = row.get("financial_type") or FINANCIAL_TYPE_UNCLASSIFIED
    if view == FINANCIAL_VIEW_OPEX:
        return ftype == FINANCIAL_TYPE_OPEX
    if view == FINANCIAL_VIEW_CAPEX:
        return ftype == FINANCIAL_TYPE_CAPEX
    if view in (FINANCIAL_VIEW_ALL_ENGINEERING, FINANCIAL_VIEW_PROCUREMENT):
        return ftype != FINANCIAL_TYPE_EXCLUDED
    return True


# ── Parsing ───────────────────────────────────────────────────────────────────

def _parse_indirect_po(path: Path) -> list[dict]:
    try:
        df = pd.read_excel(path, sheet_name=0, header=0)
    except Exception:
        return []

    c_po = _col(df, "Purchase order", "PO No", "PO No.", "PO number")
    c_pr = _col(df, "Purchase requisition", "PR No", "PR No.", "PR number")
    c_line = _col(df, "Line number", "Line")
    c_date = _col(df, "Created date and time", "Created date", "Date")
    c_pool = _col(df, "Purch Pool Id", "Pool Id", "Pool")
    c_category = _col(df, "Procurement category", "Category")
    c_item = _col(df, "Item number", "Item")
    c_name = _col(df, "Product name", "Description", "Item description")
    c_vendor_id = _col(df, "Vendor account", "Vendor Id", "Vendor code")
    c_vendor = _col(df, "Name", "Vendor name", "Vendor")
    c_qty = _col(df, "Quantity", "Qty")
    c_unit = _col(df, "Unit")
    c_status = _col(df, "Line status", "Status")
    c_price = _col(df, "Unit price", "Price/Unit", "Price")
    c_net = _col(df, "Net amount", "Net Amount", "Total")
    c_currency = _col(df, "Currency")
    c_flag = _col(df, "Column1", "Flag", "Type flag")

    rows = []
    for _, r in df.iterrows():
        po = _clean(r.get(c_po)) if c_po else ""
        pr = _clean(r.get(c_pr)) if c_pr else ""
        if not po and not pr:
            continue
        dval = _date(r.get(c_date)) if c_date else None
        procurement_category = _clean(r.get(c_category)) if c_category else ""
        item_description = _clean(r.get(c_name)) if c_name else ""
        flag = _clean(r.get(c_flag)) if c_flag else ""
        pool_id = _clean(r.get(c_pool)) if c_pool else ""
        rows.append({
            "source": "Indirect PO",
            "po_no": po,
            "pr_no": pr,
            "line_no": str(int(_num(r.get(c_line)) or 0)) if c_line and _num(r.get(c_line)) is not None else "",
            "date": dval.isoformat()[:10] if dval else "",
            "year": str(dval.year) if dval else "",
            "month": f"{dval.year}-{dval.month:02d}" if dval else "",
            "procurement_category": procurement_category,
            "item_code": _clean(r.get(c_item)) if c_item else "",
            "item_description": item_description,
            "vendor_id": _clean(r.get(c_vendor_id)) if c_vendor_id else "",
            "vendor_name": _clean(r.get(c_vendor)) if c_vendor else "",
            "qty": _num(r.get(c_qty)) if c_qty else None,
            "unit": _clean(r.get(c_unit)) if c_unit else "",
            "unit_price": _num(r.get(c_price)) if c_price else None,
            "net_amount": _num(r.get(c_net)) if c_net else None,
            "currency": _clean(r.get(c_currency)) if c_currency else "THB",
            "line_status": _clean(r.get(c_status)) if c_status else "",
            "flag": flag,
            "pool_id": pool_id,
            "financial_type": _classify_financial_type(
                procurement_category,
                item_description,
                flag,
                pool_id,
                source_file="Indirect PO",
            ),
        })
    return rows


def _file_sig() -> tuple | None:
    path = find_indirect_po_file()
    if not path:
        return None
    try:
        st = path.stat()
        return (path.name, st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def get_indirect_po_rows() -> tuple[list[dict], dict]:
    """All Indirect PO rows, cached by file signature."""
    sig = _file_sig()
    cached = _ROWS_CACHE.get("rows")
    if cached and cached["sig"] == sig:
        return cached["rows"], cached["status"]

    path = find_indirect_po_file()
    if not path:
        status = {"available": False, "file_name": None, "message": "Indirect PO file not found — import via the Manage Imports panel."}
        return [], status

    rows = _parse_indirect_po(path)
    status = {
        "available": True,
        "file_name": path.name,
        "row_count": len(rows),
        "message": f"Loaded {len(rows)} Indirect PO lines from {path.name}",
    }
    _ROWS_CACHE["rows"] = {"sig": sig, "rows": rows, "status": status}
    return rows, status


# ── Normalisation helpers ────────────────────────────────────────────────────

def _norm_po(v: str) -> str:
    return re.sub(r"\s+", "", (v or "").upper())


def _norm_desc(v: str) -> str:
    return re.sub(r"[^a-z0-9]", " ", (v or "").lower()).strip()


def _norm_vendor(v: str) -> str:
    return re.sub(r"[^a-z0-9]", " ", (v or "").lower()).strip()


def _amounts_match(a: float | None, b: float | None, tol: float = 0.02) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    base = max(abs(a), abs(b), 1)
    return abs(a - b) / base <= tol


# ── Reconciliation ────────────────────────────────────────────────────────────

def _recon_status(ind: dict, eng: dict) -> str:
    mismatches = []
    ind_qty = ind.get("qty")
    eng_qty = eng.get("qty")
    if ind_qty is not None and eng_qty is not None:
        if abs((ind_qty or 0) - (eng_qty or 0)) > 0.001:
            mismatches.append("qty")
    ind_price = ind.get("unit_price")
    eng_price = eng.get("price_unit")
    if ind_price is not None and eng_price is not None:
        if not _amounts_match(ind_price, eng_price):
            mismatches.append("price")
    ind_net = ind.get("net_amount")
    eng_total = eng.get("total_price")
    if ind_net is not None and eng_total is not None:
        if not _amounts_match(ind_net, eng_total):
            mismatches.append("amount")
    if not mismatches:
        return "Matched - Same Value"
    if len(mismatches) >= 2:
        return "Requires Review"
    if "qty" in mismatches:
        return "Matched - Qty Mismatch"
    if "price" in mismatches:
        return "Matched - Price Mismatch"
    return "Matched - Amount Mismatch"


def _score_candidate(ind: dict, eng: dict) -> int:
    score = 0
    if ind.get("item_code") and eng.get("item_number") and _norm(ind["item_code"]) == _norm(eng["item_number"]):
        score += 4
    ind_desc = set(_norm_desc(ind.get("item_description", "")).split())
    eng_desc = set(_norm_desc(eng.get("description", "")).split())
    if ind_desc and eng_desc:
        overlap = len(ind_desc & eng_desc)
        if overlap >= 2:
            score += overlap
    if _norm_vendor(ind.get("vendor_name", ""))[:10] and _norm_vendor(eng.get("vendor", ""))[:10]:
        if _norm_vendor(ind.get("vendor_name", ""))[:10] == _norm_vendor(eng.get("vendor", ""))[:10]:
            score += 2
    if _amounts_match(ind.get("net_amount"), eng.get("total_price"), 0.05):
        score += 2
    return score


def reconcile_with_engineering(indirect_rows: list[dict], engineering_rows: list[dict]) -> list[dict]:
    """Match Indirect PO lines to Engineering Gen PO lines.

    Match priority:
      1. PO number (normalised, exact)
      2. PR number (if PO number missing or no match)
      3. Best candidate scored by item code / description / vendor / amount

    Engineering classification is NOT modified — differences are flagged only.
    """
    eng_by_po: dict[str, list[dict]] = {}
    eng_by_pr: dict[str, list[dict]] = {}
    for row in engineering_rows:
        po = _norm_po(row.get("po_no", ""))
        pr = _norm_po(row.get("pr_no", ""))
        if po:
            eng_by_po.setdefault(po, []).append(row)
        if pr:
            eng_by_pr.setdefault(pr, []).append(row)

    matched_eng_ids: set[int] = set()
    result: list[dict] = []

    for ind in indirect_rows:
        ind_po = _norm_po(ind.get("po_no", ""))
        ind_pr = _norm_po(ind.get("pr_no", ""))

        candidates: list[dict] = []
        match_key = ""

        if ind_po and ind_po in eng_by_po:
            candidates = eng_by_po[ind_po]
            match_key = "PO number"
        if not candidates and ind_pr and ind_pr in eng_by_pr:
            candidates = eng_by_pr[ind_pr]
            match_key = "PR number"

        best_eng = None
        if candidates:
            if len(candidates) == 1:
                best_eng = candidates[0]
            else:
                best_eng = max(candidates, key=lambda e: _score_candidate(ind, e))

        if best_eng is not None:
            matched_eng_ids.add(id(best_eng))
            status = _recon_status(ind, best_eng)
            ind_net = ind.get("net_amount")
            eng_total = best_eng.get("total_price")
            ind_qty = ind.get("qty")
            eng_qty = best_eng.get("qty")
            ind_price = ind.get("unit_price")
            eng_price = best_eng.get("price_unit")
            result.append({
                **ind,
                "matched": True,
                "match_key": match_key,
                "recon_status": status,
                "eng_po_no": best_eng.get("po_no", ""),
                "eng_pr_no": best_eng.get("pr_no", ""),
                "eng_item_number": best_eng.get("item_number", ""),
                "eng_description": best_eng.get("description", ""),
                "eng_vendor": best_eng.get("vendor", ""),
                "eng_qty": eng_qty,
                "eng_unit_price": eng_price,
                "eng_total_price": eng_total,
                "eng_group_of_cost": best_eng.get("group_of_cost", ""),
                "eng_stage": best_eng.get("stage", ""),
                "eng_category": best_eng.get("category", ""),
                "eng_financial_type": best_eng.get("financial_type", ""),
                "qty_diff": round((ind_qty or 0) - (eng_qty or 0), 4) if ind_qty is not None and eng_qty is not None else None,
                "price_diff": round((ind_price or 0) - (eng_price or 0), 4) if ind_price is not None and eng_price is not None else None,
                "amount_diff": round((ind_net or 0) - (eng_total or 0), 2) if ind_net is not None and eng_total is not None else None,
            })
        else:
            result.append({
                **ind,
                "matched": False,
                "match_key": "",
                "recon_status": "Indirect PO Only",
                "eng_po_no": "", "eng_pr_no": "", "eng_item_number": "",
                "eng_description": "", "eng_vendor": "",
                "eng_qty": None, "eng_unit_price": None, "eng_total_price": None,
                "eng_group_of_cost": "", "eng_stage": "", "eng_category": "", "eng_financial_type": "",
                "qty_diff": None, "price_diff": None, "amount_diff": None,
            })

    # Engineering-only rows (in Engineering but not matched to any Indirect PO line)
    for eng in engineering_rows:
        if id(eng) in matched_eng_ids:
            continue
        result.append({
            "source": "Engineering Copy Only",
            "po_no": eng.get("po_no", ""),
            "pr_no": eng.get("pr_no", ""),
            "line_no": "",
            "date": eng.get("date_gen_po", ""),
            "year": eng.get("year", ""),
            "month": eng.get("month", ""),
            "procurement_category": "",
            "item_code": eng.get("item_number", ""),
            "item_description": eng.get("description", ""),
            "vendor_id": "",
            "vendor_name": eng.get("vendor", ""),
            "qty": eng.get("qty"),
            "unit": eng.get("unit", ""),
            "unit_price": eng.get("price_unit"),
            "net_amount": eng.get("total_price"),
            "currency": "THB",
            "line_status": "",
            "flag": "",
            "pool_id": "",
            "financial_type": eng.get("financial_type", ""),
            "matched": False,
            "match_key": "",
            "recon_status": "Engineering Copy Only",
            "eng_po_no": eng.get("po_no", ""),
            "eng_pr_no": eng.get("pr_no", ""),
            "eng_item_number": eng.get("item_number", ""),
            "eng_description": eng.get("description", ""),
            "eng_vendor": eng.get("vendor", ""),
            "eng_qty": eng.get("qty"),
            "eng_unit_price": eng.get("price_unit"),
            "eng_total_price": eng.get("total_price"),
            "eng_group_of_cost": eng.get("group_of_cost", ""),
            "eng_stage": eng.get("stage", ""),
            "eng_category": eng.get("category", ""),
            "eng_financial_type": eng.get("financial_type", ""),
            "qty_diff": None,
            "price_diff": None,
            "amount_diff": None,
        })

    return result


# ── KPI + analytics builders ──────────────────────────────────────────────────

def _procurement_kpis(recon_rows: list[dict]) -> dict:
    indirect_rows = [r for r in recon_rows if r.get("source") == "Indirect PO"]
    matched = [r for r in indirect_rows if r.get("matched")]
    matched_in_scope = [r for r in matched if r.get("scope_match", True)]
    unmatched = [r for r in indirect_rows if not r.get("matched")]
    mismatch_statuses = {
        "Matched - Price Mismatch",
        "Matched - Qty Mismatch",
        "Matched - Amount Mismatch",
        "Requires Review",
    }
    total_val = sum(r.get("net_amount") or 0 for r in indirect_rows)
    matched_eng_val = sum(r.get("eng_total_price") or 0 for r in matched_in_scope)
    unmatched_val = sum(r.get("net_amount") or 0 for r in unmatched)
    non_eng_unmatched_val = max(total_val - matched_eng_val, 0)
    mismatch_count = sum(1 for r in matched_in_scope if r.get("recon_status") in mismatch_statuses)
    total = len(indirect_rows)
    match_rate = round(100 * len(matched_in_scope) / total, 1) if total > 0 else None

    status_breakdown: dict[str, int] = {}
    for r in recon_rows:
        s = r.get("recon_status") or "Unknown"
        status_breakdown[s] = status_breakdown.get(s, 0) + 1

    return {
        "total_indirect_po_value": round(total_val, 2),
        "matched_engineering_po_value": round(matched_eng_val, 2),
        "unmatched_procurement_value": round(unmatched_val, 2),
        "non_engineering_unmatched_procurement_value": round(non_eng_unmatched_val, 2),
        "price_qty_mismatch_count": mismatch_count,
        "match_rate_pct": match_rate,
        "total_indirect_lines": total,
        "matched_lines": len(matched_in_scope),
        "status_breakdown": [
            {"label": k, "count": v}
            for k, v in sorted(status_breakdown.items(), key=lambda x: -x[1])
        ],
    }


def _vendor_performance(indirect_rows: list[dict]) -> list[dict]:
    """Vendor KPIs from Indirect PO (official vendor source)."""
    agg: dict[str, dict] = {}
    for r in indirect_rows:
        vendor = r.get("vendor_name") or r.get("vendor_id") or "Unknown"
        po = r.get("po_no", "")
        v = agg.setdefault(vendor, {
            "vendor": vendor,
            "vendor_id": r.get("vendor_id", ""),
            "total_po_value": 0.0,
            "po_numbers": set(),
            "line_count": 0,
            "mismatch_count": 0,
            "procurement_only_count": 0,
        })
        v["total_po_value"] += r.get("net_amount") or 0
        if po:
            v["po_numbers"].add(po)
        v["line_count"] += 1
        status = r.get("recon_status", "")
        if status in {"Matched - Price Mismatch", "Matched - Qty Mismatch", "Matched - Amount Mismatch", "Requires Review"}:
            v["mismatch_count"] += 1
        if status == "Indirect PO Only":
            v["procurement_only_count"] += 1

    result = []
    for v in sorted(agg.values(), key=lambda x: -x["total_po_value"]):
        po_count = len(v["po_numbers"])
        result.append({
            "vendor": v["vendor"],
            "vendor_id": v["vendor_id"],
            "total_po_value": round(v["total_po_value"], 2),
            "po_count": po_count,
            "line_count": v["line_count"],
            "avg_po_value": round(v["total_po_value"] / po_count, 2) if po_count > 0 else None,
            "mismatch_count": v["mismatch_count"],
            "procurement_only_count": v["procurement_only_count"],
        })
    return result


def _engineering_with_recon(eng_rows: list[dict], recon_rows: list[dict]) -> list[dict]:
    """Add reconciliation fields to Engineering Gen PO rows without touching classification."""
    # Build index: norm eng PO → first matching indirect row
    ind_by_eng_po: dict[str, dict] = {}
    ind_by_eng_pr: dict[str, dict] = {}
    for r in recon_rows:
        if r.get("matched"):
            if r.get("eng_po_no") and _norm_po(r["eng_po_no"]) not in ind_by_eng_po:
                ind_by_eng_po[_norm_po(r["eng_po_no"])] = r
            if r.get("eng_pr_no") and _norm_po(r["eng_pr_no"]) not in ind_by_eng_pr:
                ind_by_eng_pr[_norm_po(r["eng_pr_no"])] = r

    result = []
    for eng in eng_rows:
        po_key = _norm_po(eng.get("po_no", ""))
        pr_key = _norm_po(eng.get("pr_no", ""))
        ind = ind_by_eng_po.get(po_key) or ind_by_eng_pr.get(pr_key)
        ind_value = ind.get("net_amount") if ind else None
        eng_value = eng.get("total_price")
        diff = round((ind_value or 0) - (eng_value or 0), 2) if ind_value is not None and eng_value is not None else None
        result.append({
            **eng,
            "official_indirect_po_value": ind_value,
            "engineering_copy_value": eng_value,
            "value_difference": diff,
            "recon_status": ind.get("recon_status", "Engineering Copy Only") if ind else "Engineering Copy Only",
            "official_vendor": ind.get("vendor_name", "") if ind else "",
            "official_item_code": ind.get("item_code", "") if ind else "",
        })
    return result


# ── Cached reconciliation (keyed by file signatures) ─────────────────────────

def _eng_sig(eng_rows: list[dict]) -> int:
    return len(eng_rows)


def _get_recon(indirect_rows: list[dict], engineering_rows: list[dict]) -> list[dict]:
    """Return reconciliation rows, re-running only when inputs change."""
    ind_sig = _file_sig()
    eng_sig = _eng_sig(engineering_rows)
    cached = _RECON_CACHE.get("recon")
    if cached and cached["ind_sig"] == ind_sig and cached["eng_sig"] == eng_sig:
        return cached["rows"]
    rows = reconcile_with_engineering(indirect_rows, engineering_rows)
    _RECON_CACHE["recon"] = {"ind_sig": ind_sig, "eng_sig": eng_sig, "rows": rows}
    return rows


# ── Public payload builder ────────────────────────────────────────────────────

def build_procurement_reconciliation(stage=None, category=None, year=None, month=None, financial_view=None) -> dict:
    """Full procurement reconciliation payload including KPIs, vendor performance,
    and Engineering lines annotated with reconciliation status."""
    import spare_parts_views as spv  # avoid circular import at module level

    indirect_rows, indirect_status = get_indirect_po_rows()
    engineering_rows, _ = spv.get_goods_received_rows()

    # Apply year/month filter to indirect rows
    def _ind_filter(r):
        if year and year not in ("", "all") and r.get("year") != str(year):
            return False
        if month and month not in ("", "all") and r.get("month") != str(month):
            return False
        return True

    filtered_indirect = [r for r in indirect_rows if _ind_filter(r)]

    # Apply stage/category/year/month/financial filter to engineering rows.
    eng_filtered = engineering_rows
    if stage and stage not in ("", "all", "All Stages"):
        eng_filtered = [r for r in eng_filtered if r.get("stage") == stage]
    if category and category not in ("", "all", "All"):
        eng_filtered = [r for r in eng_filtered if r.get("category") == category]
    if year and year not in ("", "all"):
        eng_filtered = [r for r in eng_filtered if r.get("year") == str(year)]
    if month and month not in ("", "all"):
        eng_filtered = [r for r in eng_filtered if r.get("month") == str(month)]
    if financial_view:
        eng_filtered = [r for r in eng_filtered if _financial_view_allows(r, financial_view)]

    recon_rows = _get_recon(indirect_rows, engineering_rows)

    eng_po_scope = {_norm_po(e.get("po_no", "")) for e in eng_filtered if e.get("po_no")}
    eng_pr_scope = {_norm_po(e.get("pr_no", "")) for e in eng_filtered if e.get("pr_no")}

    def _scope_match(row: dict) -> bool:
        if not row.get("matched"):
            return False
        po = _norm_po(row.get("eng_po_no", ""))
        pr = _norm_po(row.get("eng_pr_no", ""))
        return (po and po in eng_po_scope) or (pr and pr in eng_pr_scope)

    # Indirect rows remain company-wide reference rows. The scope_match flag
    # controls Engineering matched value KPIs without rewriting source data.
    filtered_recon: list[dict] = []
    for r in recon_rows:
        if r.get("source") == "Indirect PO" and any(
            r.get("po_no") == fi.get("po_no") and r.get("pr_no") == fi.get("pr_no")
            and r.get("line_no") == fi.get("line_no")
            for fi in filtered_indirect
        ):
            filtered_recon.append({**r, "scope_match": _scope_match(r)})
        elif r.get("source") == "Engineering Copy Only":
            eng_po = _norm_po(r.get("po_no", ""))
            eng_pr = _norm_po(r.get("pr_no", ""))
            if (eng_po and eng_po in eng_po_scope) or (eng_pr and eng_pr in eng_pr_scope):
                filtered_recon.append({**r, "scope_match": False})

    kpis = _procurement_kpis(filtered_recon)
    vendor_perf = _vendor_performance(
        [r for r in filtered_recon if r.get("source") == "Indirect PO"]
    )
    eng_recon = _engineering_with_recon(eng_filtered, filtered_recon)

    # Procurement category and flag breakdowns
    proc_cats: dict[str, float] = {}
    flags: dict[str, float] = {}
    for r in [x for x in filtered_recon if x.get("source") == "Indirect PO"]:
        cat = r.get("procurement_category") or "Uncategorised"
        proc_cats[cat] = proc_cats.get(cat, 0.0) + (r.get("net_amount") or 0)
        flag = r.get("flag") or "Unknown"
        flags[flag] = flags.get(flag, 0.0) + (r.get("net_amount") or 0)

    return {
        "kpis": kpis,
        "vendor_performance": vendor_perf[:25],
        "engineering_with_reconciliation": eng_recon,
        "reconciliation_rows": filtered_recon[:300],
        "source_status": {"indirect_po": indirect_status},
        "procurement_categories": [
            {"label": k, "value": round(v, 2)}
            for k, v in sorted(proc_cats.items(), key=lambda x: -x[1])
        ],
        "flags": [
            {"label": k, "value": round(v, 2)}
            for k, v in sorted(flags.items(), key=lambda x: -x[1])
        ],
        "filters_applied": {
            "stage": stage or "all",
            "category": category or "all",
            "year": year or "all",
            "month": month or "all",
            "financial_view": _normalize_financial_view(financial_view),
        },
    }


def get_procurement_kpis_for_overview(year=None, month=None, stage=None, category=None, financial_view=None) -> dict:
    """Lightweight KPI summary for embedding in the overview payload.
    Uses cached reconciliation rows — does not re-parse if already warm."""
    payload = build_procurement_reconciliation(stage, category, year, month, financial_view)
    indirect_status = (payload.get("source_status") or {}).get("indirect_po") or {}
    if not indirect_status.get("available"):
        return {"available": False, "message": indirect_status.get("message", "")}
    kpis = payload.get("kpis") or {}
    kpis["available"] = True
    return kpis


# ── Import handler ────────────────────────────────────────────────────────────

def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]", "_", str(name or "")).strip() or "indirect_po.xlsx"


def import_indirect_po(file_storage) -> dict:
    """Save an Indirect PO upload to the import slot and update the manifest."""
    if file_storage is None or not getattr(file_storage, "filename", ""):
        return {"ok": False, "message": "No file uploaded."}
    orig = _safe_name(file_storage.filename)
    if not orig.lower().endswith((".xlsx", ".xls")):
        return {"ok": False, "file_name": orig, "message": "Please upload an Excel (.xlsx / .xls) Indirect PO file."}
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    stored = IMPORT_DIR / f"indirect_po__{orig}"
    try:
        file_storage.save(str(stored))
    except Exception as exc:
        return {"ok": False, "file_name": orig, "message": f"Could not save file: {exc}"}

    rows = _parse_indirect_po(stored)
    if not rows:
        try:
            stored.unlink()
        except Exception:
            pass
        return {"ok": False, "file_name": orig, "message": "No Indirect PO rows found. Check the sheet layout — expected columns: Purchase order, Purchase requisition, Net amount, Vendor account, Name, Quantity, Unit price."}

    man = _load_manifest()
    prev = man.get("stored_path")
    if prev and Path(prev) != stored and Path(prev).exists() and IMPORT_DIR in Path(prev).parents:
        try:
            Path(prev).unlink()
        except Exception:
            pass

    man["stored_path"] = str(stored)
    man["file_name"] = orig
    man["imported_at"] = datetime.now().isoformat(timespec="seconds")
    man["row_count"] = len(rows)
    _save_manifest(man)
    _ROWS_CACHE.pop("rows", None)
    _RECON_CACHE.pop("recon", None)

    return {
        "ok": True,
        "file_name": orig,
        "row_count": len(rows),
        "imported_at": man["imported_at"],
        "message": f"Imported {len(rows)} Indirect PO lines from {orig}.",
    }


def get_indirect_po_import_status() -> dict:
    man = _load_manifest()
    path = find_indirect_po_file()
    from_import = bool(man.get("stored_path") and Path(man.get("stored_path", "")).exists())
    return {
        "uploaded": path is not None,
        "file_name": man.get("file_name") or (path.name if path else None),
        "imported_at": man.get("imported_at"),
        "row_count": man.get("row_count"),
        "source": "import" if from_import else ("auto-discovered" if path else None),
        "message": (
            f"Loaded {man.get('row_count', 0)} lines from {man.get('file_name', path.name if path else '')}"
            if path else "No Indirect PO file imported yet."
        ),
    }
