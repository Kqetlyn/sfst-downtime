from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import threading
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

from asset_resolver import (
    build_all_asset_profiles,
    build_asset_profile,
    match_record_to_asset_profiles,
    normalize_text as normalize_asset_text,
)
from downtime_management import load_grouped_machine_mapping
from maintenance_service import MONTH_LABELS, build_equipment_dataset, clean_text


DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ASSET_MASTER_PATH = DEFAULT_DATA_DIR / "master" / "Asset_Master.xlsx"
D365_SPARE_PARTS_PATH = Path(
    os.environ.get(
        "SPARE_PARTS_D365_PATH",
        str(DEFAULT_DATA_DIR / "DynamicsExport_complete_final.xlsx"),
    )
)
GEN_PO_SPARE_PARTS_PATH = Path(
    os.environ.get(
        "SPARE_PARTS_GEN_PO_PATH",
        str(DEFAULT_DATA_DIR / "Gen PO D365 Rev.03.xlsx"),
    )
)

PART_INCLUDE_KEYWORDS = (
    "actuator",
    "bearing",
    "belt",
    "blade",
    "brass",
    "cable",
    "cartridge",
    "chain",
    "compressor",
    "connector",
    "coupling",
    "cylinder",
    "electrical",
    "fan",
    "filter",
    "fitting",
    "gasket",
    "gear",
    "hose",
    "lamp",
    "led",
    "lubricant",
    "mechanical",
    "motor",
    "pipe",
    "pvc",
    "pulley",
    "pump",
    "part",
    "refrigerant",
    "relay",
    "roller",
    "r507",
    "r507a",
    "oil",
    "seal",
    "sensor",
    "filter",
    "spare",
    "sprocket",
    "switch",
    "thermostat",
    "transmitter",
    "tube",
    "valve",
    "dryer",
    "axial",
    "pressure",
    "copper",
)
PART_EXCLUDE_KEYWORDS = (
    "labour",
    "labor",
    "service charge",
    "rag",
    "sign",
    "sticker",
    "cleaning",
    "civil",
    "cevil",
)
SPARE_PART_KEYWORDS = tuple(sorted(set(PART_INCLUDE_KEYWORDS + (
    "bearing housing",
    "electrical part",
    "mechanical part",
    "terminal",
    "welding wire",
))))
NON_SPARE_PART_KEYWORDS = tuple(sorted(set(PART_EXCLUDE_KEYWORDS + (
    "calibration",
    "certificate",
    "cleaning",
    "consultation",
    "contractor",
    "civil work",
    "food",
    "inspection",
    "installation service",
    "labor",
    "license",
    "office",
    "painting",
    "permit",
    "rental",
    "repair service",
    "service",
    "safety",
    "stationery",
    "stationery",
    "training",
    "transport",
    "uniform",
    "wall",
))))
THAI_TEXT_RE = re.compile(r"[\u0E00-\u0E7F]")
CODE_LIKE_VALUE_RE = re.compile(r"^[A-Z]{2,}[A-Z0-9]*\d{4,}$")
FUTURE_SOURCE_SPECS = {
    "spare_parts_master": {
        "base": "DynamicsExport_complete_final",
        "preferred_aliases": ["spare_parts_master"],
        "aliases": [
            "spare_parts_master",
            "inventory_spare_parts",
            "spare_parts_inventory",
            "inventory_spare_parts_list",
            "Item_list_for_keep_spare_part_TRANSLATED",
        ],
        "env": "SPARE_PARTS_MASTER_PATH",
        "label": "Current Inventory",
        "missing": "Current inventory file not uploaded",
        "fallback": lambda: D365_SPARE_PARTS_PATH,
    },
    "inventory_movement": {
        "base": "inventory_movement",
        "env": "SPARE_PARTS_INVENTORY_MOVEMENT_PATH",
        "label": "Inventory Movement",
        "missing": "Inventory movement data not uploaded",
        "fallback": lambda: None,
    },
    "po_list": {
        "base": "Gen PO D365 Rev.03",
        "preferred_aliases": ["po_list"],
        "aliases": [
            "po_list",
            "Gen PO D365 Rev.03",
            "Gen_PO_translated_fully_clean",
        ],
        "env": "SPARE_PARTS_PO_LIST_PATH",
        "label": "Gen PO",
        "missing": "Gen PO file not uploaded",
        "fallback": lambda: GEN_PO_SPARE_PARTS_PATH,
    },
    "work_orders": {
        "base": "work_orders",
        "env": "SPARE_PARTS_WORK_ORDERS_PATH",
        "label": "Work Order Data",
        "missing": "Work order data not uploaded",
        "fallback": lambda: None,
    },
    "equipment_master": {
        "base": "equipment_master",
        "env": "SPARE_PARTS_EQUIPMENT_MASTER_PATH",
        "label": "Equipment Master",
        "missing": "Equipment master not uploaded",
        "fallback": lambda: ASSET_MASTER_PATH,
    },
}
FLEXIBLE_COLUMN_ALIASES = {
    "asset_id": ["Asset ID", "AssetID", "Asset", "Equipment ID", "Machine ID", "PD Machine", "Equipment Code", "Linked Equipment"],
    "available_physical": ["Available physical", "Available", "On-hand", "Current stock", "Available Quantity", "Current Quantity"],
    "category": ["Category", "Item Group", "Part Group", "Part group", "Spare Part Type", "Product Category", "Item Category", "Group of cost", "Type of cost", "Procurement Category"],
    "classification": ["Classification", "PO Classification", "Item Classification"],
    "code": ["Item Number", "Item ID", "Part Number", "Spare Part ID", "Product Number", "Spare Part Code", "Item Code", "Part Code", "Item number", "Product identification", "Product ID"],
    "criticality": ["Equipment Criticality", "Criticality", "CriticalityGroup"],
    "current_quantity": ["Current Stock", "Stock Balance", "On Hand", "On-hand Inventory", "Available Quantity", "Quantity Available", "Stock Qty", "Inventory Quantity", "Current Quantity", "Available physical", "Qty On Hand", "Stock Quantity"],
    "department": ["Department", "Cost Centre", "Cost Center"],
    "description": ["Item Description", "Description", "Item Name", "Product Name", "Product name", "Search name", "Spare Part Name"],
    "equipment_name": ["Equipment", "Machine", "Asset", "Asset ID", "AssetID", "Linked Equipment", "Equipment Name", "Machine Name", "Asset Name", "PD Machine"],
    "equipment_type": ["Equipment Type", "Machine Type", "Equipment Group", "Machine Group", "Main Asset Group", "Sub Asset Group"],
    "gl_account": ["GL Account", "Procurement Category"],
    "goods_received_date": ["Goods Received Date", "Received Date", "GR Date"],
    "grn_no": ["GRN No.", "GRN No"],
    "grn_status": ["GRN Status", "PR PO GRN Status"],
    "group_of_cost": ["Group of Cost", "Group of cost"],
    "grn_po_days": ["GRN-PO date (Day)", "GRN-PO date", "GRN PO date"],
    "inventory_value": ["Inventory value", "Inventory Value", "Stock Value", "Total Value"],
    "issue_date": ["Issue Date", "Used Date", "Transaction Date"],
    "item_group": ["Item Group", "Group", "Product Group"],
    "kpi_status": ["KPI Status"],
    "issued_by": ["Issued By", "Requested By", "Requestor", "Requester"],
    "last_updated": ["Last Updated Date", "Last Updated", "Modified Date"],
    "location": ["Location", "Store Location", "Warehouse", "Bin Location", "Building"],
    "lead_time_days": ["Lead time delivery (day)", "Lead time delivery"],
    "maintenance_type": ["Maintenance Type", "Job Trade", "PM Type"],
    "max_stock": ["Maximum", "Max", "Maximum Quantity", "Maximum Stock", "Recommended Quantity", "Maximum Stock Level", "Max Stock"],
    "min_stock": ["Minimum", "Min", "Minimum Quantity", "Minimum Stock", "Reorder Point", "Minimum Stock Level", "Min Stock"],
    "name": ["Item Name", "Product Name", "Spare Part Name", "Description", "Item Description", "Item Description", "Product name", "Search name"],
    "on_order": ["On order", "Ordered in total", "Quantity on order"],
    "pd_machine": ["PD Machine", "Machine", "Asset", "Equipment"],
    "po_date": ["PO Date", "Purchase Date", "Order Date", "Date Gen PO", "DMY Create(EN) CPP", "DMY Create PR"],
    "po_number": ["PO Number", "PO No.", "PO No", "Purchase Order"],
    "production_line": ["Production Line", "Line", "Area", "System/Area", "System Area", "Stage"],
    "quantity": ["Qty", "Quantity", "Quantity Ordered", "Quantity Used", "Qty'", "Quantity Drawn", "Quantity Received"],
    "quantity_drawn": ["Quantity Drawn", "Quantity Used", "Qty Used", "Qty Drawn", "Issued Quantity"],
    "quantity_ordered": ["Quantity Ordered", "Qty Ordered", "Qty'", "Qty", "Quantity"],
    "quantity_received": ["Quantity Received", "Qty Received", "Received Quantity"],
    "reserved_physical": ["Reserved physical", "Physical reserved", "Reserved Quantity"],
    "search_name": ["Search name", "Search Name"],
    "supplier": ["Supplier", "Vendor", "Vendor name", "Vendor Name"],
    "sub_cost": ["Sub-Cost", "Sub Cost"],
    "total_available": ["Total available", "Available quantity", "Stock balance", "Total Available"],
    "total_cost": ["Total Cost", "Total price", "Total Price", "Amount"],
    "transaction_date": ["Transaction Date", "Issue Date", "Used Date", "Date"],
    "transaction_type": ["Transaction Type", "Movement Type", "Type"],
    "type_of_cost": ["Type of Cost", "Type of cost"],
    "unit": ["Unit of Measure", "Unit", "Unit of measure", "Inventory unit", "UOM"],
    "unit_cost": ["Unit Cost", "Price", "Average Cost", "Item Cost", "Unit Price", "Cost", "Unit price", "Price/Unit"],
    "work_order_id": ["Work Order ID", "WO ID", "WorkOrderID", "PR No.", "Request ID"],
}
_SPARE_PARTS_CACHE: dict[tuple, dict] = {}
_SPARE_PERSISTENT_CACHE_VERSION = 1
_SPARE_PERSISTENT_CACHE_DIR = DEFAULT_DATA_DIR / "_dashboard_cache" / "spare_parts"
_SPARE_DB_SYNC_GUARD = threading.Lock()
_SPARE_DB_SYNC_RUNNING = False
_SPARE_DB_SYNC_PENDING = False


def _file_signature(path: Path | None):
    if not path:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _persistent_cache_key(signature) -> str:
    raw = json.dumps(signature, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _persistent_cache_path(name: str, signature) -> Path:
    safe_name = re.sub(r"[^a-z0-9_-]+", "_", str(name or "payload").lower()).strip("_") or "payload"
    return _SPARE_PERSISTENT_CACHE_DIR / f"{safe_name}_{_persistent_cache_key(signature)}.json"


def _read_persistent_payload_cache(name: str, signature):
    path = _persistent_cache_path(name, signature)
    try:
        with open(path, encoding="utf-8") as fh:
            wrapper = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if wrapper.get("version") != _SPARE_PERSISTENT_CACHE_VERSION:
        return None
    if wrapper.get("key") != _persistent_cache_key(signature):
        return None
    payload = wrapper.get("payload")
    return payload if isinstance(payload, dict) else None


def _write_persistent_payload_cache(name: str, signature, payload: dict) -> None:
    try:
        _SPARE_PERSISTENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _persistent_cache_path(name, signature)
        temp_path = path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "version": _SPARE_PERSISTENT_CACHE_VERSION,
                    "key": _persistent_cache_key(signature),
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "payload": payload,
                },
                fh,
                default=str,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        temp_path.replace(path)
    except (TypeError, OSError, ValueError):
        # Disk cache is an optimization only; never fail an API response because of it.
        pass


def _clear_persistent_payload_cache() -> None:
    try:
        if not _SPARE_PERSISTENT_CACHE_DIR.exists():
            return
        for path in _SPARE_PERSISTENT_CACHE_DIR.glob("*.json"):
            try:
                path.unlink()
            except OSError:
                pass
        for path in _SPARE_PERSISTENT_CACHE_DIR.glob("*.tmp"):
            try:
                path.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _dedupe_existing_paths(paths):
    ordered = []
    seen = set()
    for path in paths:
        if not path:
            continue
        candidate = Path(path)
        if not candidate.exists():
            continue
        resolved = str(candidate.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(candidate)
    return ordered


def _spare_import_safe_stem(filename: str, fallback: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(filename or fallback).stem).strip("._")
    return stem or fallback


def _project_transactions_source_candidates():
    env_path = PROJECT_TRANSACTIONS_PATH
    priority_paths = [
        PROJECT_TRANSACTIONS_CURRENT_PATH,
        DEFAULT_DATA_DIR / "project_transactions_current.csv",
        DEFAULT_DATA_DIR / "project_transactions_current.xls",
        DEFAULT_DATA_DIR / "Project actual transactions current.xlsx",
        DEFAULT_DATA_DIR / "Project actual transactions current.csv",
        DEFAULT_DATA_DIR / "Project actual transactions 2026.xlsx",
        DEFAULT_DATA_DIR / "Project actual transactions 2026.csv",
        DEFAULT_DATA_DIR / "Project actual transactions.xlsx",
        env_path,
    ]
    return _dedupe_existing_paths(priority_paths)


def _resolve_project_transactions_source_path() -> Path | None:
    candidates = _project_transactions_source_candidates()
    return candidates[0] if candidates else None


def _project_transactions_import_history_paths():
    paths = []
    if PROJECT_TRANSACTIONS_IMPORT_DIR.exists():
        paths.extend(sorted(PROJECT_TRANSACTIONS_IMPORT_DIR.glob("*.xlsx")))
        paths.extend(sorted(PROJECT_TRANSACTIONS_IMPORT_DIR.glob("*.xls")))
        paths.extend(sorted(PROJECT_TRANSACTIONS_IMPORT_DIR.glob("*.csv")))
    current = _resolve_project_transactions_source_path()
    if current and current.resolve() != CSV_ALL_YEARS_PATH.resolve():
        paths.append(current)
    return _dedupe_existing_paths(paths)


def _pt_work_order_sources_signature():
    imports_dir = DEFAULT_DATA_DIR / "work_order_imports"
    paths = []
    if imports_dir.exists():
        paths.extend(sorted(imports_dir.glob("*.xlsx")))
        paths.extend(sorted(imports_dir.glob("*.xls")))
        paths.extend(sorted(imports_dir.glob("*.csv")))
    paths.extend(sorted(DEFAULT_DATA_DIR.glob("work_orders_*.csv")))
    return tuple(_file_signature(path) for path in _dedupe_existing_paths(paths))


def _normalize_key(value: str | None) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _normalize_phrase(value: str | None) -> str:
    cleaned = clean_text(value) or ""
    cleaned = cleaned.lower()
    cleaned = re.sub(r"[^a-z0-9\s/-]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _clean_numeric(value):
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return None
    number = float(numeric)
    if math.isclose(number, round(number)):
        return int(round(number))
    return round(number, 3)


def _parse_date(value):
    parsed = pd.to_datetime(value, errors="coerce", dayfirst=False)
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime()


def _normalize_part_name(value: str | None) -> str:
    phrase = _normalize_phrase(value)
    if not phrase:
        return ""
    phrase = re.sub(r"\b(model|size|color|grade|installed|use|for|the|and)\b", " ", phrase)
    phrase = re.sub(r"\s+", " ", phrase).strip()
    return phrase


def _row_has_any_content(row, columns) -> bool:
    return any(clean_text(row.get(column)) for column in columns)


def _has_pd_machine_reference(value: str | None) -> bool:
    return bool(_normalize_phrase(value))


def _is_relevant_inventory_row(row) -> bool:
    item_group = _normalize_phrase(row.get("Item Group"))
    if item_group != "spare":
        return False
    quantity = _clean_numeric(row.get("Available physical"))
    return quantity is not None and quantity > 0


def _is_relevant_external_row(row) -> bool:
    text = " ".join(
        filter(
            None,
            [
                _normalize_phrase(row.get("Type of cost")),
                _normalize_phrase(row.get("Group of cost")),
                _normalize_phrase(row.get("PD Machine")),
                _normalize_phrase(row.get("Description")),
                _normalize_phrase(row.get("Note")),
            ],
        )
    )
    if not text:
        return False
    if any(keyword in text for keyword in PART_EXCLUDE_KEYWORDS):
        return False
    if any(keyword in text for keyword in PART_INCLUDE_KEYWORDS):
        return True
    return "machine" in text or "cooling" in text or "electrical" in text or "mechanical" in text


def _build_equipment_candidates(data_dir: str):
    equipment_dataset = build_equipment_dataset()
    mapping = load_grouped_machine_mapping(data_dir)

    candidates = {}
    for asset in equipment_dataset.get("assets", []):
        asset_code = clean_text(asset.get("asset_code"))
        asset_name = clean_text(asset.get("asset_name"))
        machine_group = clean_text(asset.get("subcategory") or asset.get("category") or asset_name)
        if not asset_name:
            continue
        key = asset_code or asset_name
        candidates[key] = {
            "asset_id": asset_code,
            "equipment_name": asset_name,
            "machine_group": machine_group or asset_name,
            "location": clean_text(asset.get("location_display")) or machine_group or "Unknown",
            "criticality": clean_text(asset.get("criticality")) or "Standard",
            "aliases": {
                _normalize_phrase(asset_name),
                _normalize_phrase(machine_group),
                _normalize_key(asset_name),
                _normalize_key(machine_group),
            },
        }

    for group in mapping.get("groups", []):
        group_name = clean_text(group.get("machine_group"))
        if not group_name:
            continue
        asset_ids = group.get("asset_ids") or []
        primary_asset = asset_ids[0] if asset_ids else None
        key = primary_asset or group_name
        existing = candidates.get(key, {})
        aliases = set(existing.get("aliases", set()))
        aliases.update({
            _normalize_phrase(group_name),
            _normalize_key(group_name),
        })
        candidates[key] = {
            "asset_id": existing.get("asset_id") or primary_asset,
            "equipment_name": existing.get("equipment_name") or group_name,
            "machine_group": existing.get("machine_group") or group_name,
            "location": existing.get("location") or clean_text(group.get("location")) or "Unknown",
            "criticality": clean_text(group.get("criticality")) or existing.get("criticality") or "Standard",
            "aliases": aliases,
        }

    candidate_rows = []
    for candidate in candidates.values():
        aliases = sorted({alias for alias in candidate["aliases"] if alias}, key=len, reverse=True)
        if not aliases:
            continue
        candidate_rows.append({**candidate, "aliases": aliases})
    candidate_rows.sort(key=lambda item: max(len(alias) for alias in item["aliases"]), reverse=True)
    return candidate_rows


def _link_equipment(record, candidates):
    text_fields = [
        clean_text(record.get("asset_id")),
        clean_text(record.get("equipment_name")),
        clean_text(record.get("item_description")),
        clean_text(record.get("raw_description")),
        clean_text(record.get("machine_hint")),
        clean_text(record.get("remarks")),
    ]
    joined_text = " ".join(filter(None, text_fields))
    phrase = _normalize_phrase(joined_text)
    compact = _normalize_key(joined_text)

    for candidate in candidates:
        asset_id = clean_text(candidate.get("asset_id"))
        if asset_id and asset_id.lower() in joined_text.lower():
                return {
                    "linked_equipment_name": candidate["equipment_name"],
                    "linked_asset_id": asset_id,
                    "linked_machine_group": candidate["machine_group"],
                    "linked_criticality": candidate.get("criticality") or "Standard",
                    "link_confidence": "Exact Asset ID",
                    "unlinked_flag": False,
                }

    for candidate in candidates:
        for alias in candidate["aliases"]:
            if len(alias) < 5:
                continue
            if (" " in alias and alias in phrase) or (" " not in alias and alias in compact):
                confidence = "Exact Name" if alias == _normalize_phrase(candidate["equipment_name"]) else "Machine Group Match"
                return {
                    "linked_equipment_name": candidate["equipment_name"],
                    "linked_asset_id": candidate.get("asset_id"),
                    "linked_machine_group": candidate["machine_group"],
                    "linked_criticality": candidate.get("criticality") or "Standard",
                    "link_confidence": confidence,
                    "unlinked_flag": False,
                }

    return {
        "linked_equipment_name": None,
        "linked_asset_id": None,
        "linked_machine_group": None,
        "linked_criticality": None,
        "link_confidence": "Unlinked / Review Needed",
        "unlinked_flag": True,
    }


def _classify_record(record) -> str | None:
    if record.get("source_type") == "Inventory":
        return "Planned"

    if _has_pd_machine_reference(record.get("machine_hint")):
        return "Urgent"
    return None


def _build_trend(records):
    dated = [row for row in records if row.get("date")]
    if not dated:
        return {"labels": [], "planned_counts": [], "urgent_counts": []}

    buckets = defaultdict(lambda: {"Planned": 0, "Urgent": 0})
    for row in dated:
        dt = _parse_date(row.get("date"))
        if not dt:
            continue
        if row.get("urgency_type") not in {"Planned", "Urgent"}:
            continue
        key = f"{dt.year}-{dt.month:02d}"
        buckets[key][row.get("urgency_type") or "Planned"] += 1

    ordered = sorted(buckets)
    return {
        "labels": [
            f"{MONTH_LABELS[int(key.split('-')[1]) - 1]} {key.split('-')[0]}"
            for key in ordered
        ],
        "planned_counts": [buckets[key]["Planned"] for key in ordered],
        "urgent_counts": [buckets[key]["Urgent"] for key in ordered],
    }


def _build_filter_options(records):
    urgency_types = [value for value in ("Planned", "Urgent") if any(row.get("urgency_type") == value for row in records)]
    return {
        "source_types": sorted({row["source_type"] for row in records}),
        "urgency_types": urgency_types,
        "equipment_names": sorted({row["linked_equipment_name"] for row in records if row.get("linked_equipment_name")}),
        "asset_ids": sorted({row["linked_asset_id"] for row in records if row.get("linked_asset_id")}),
        "vendors": sorted({row["supplier_vendor"] for row in records if row.get("supplier_vendor")}),
        "link_states": [
            {"value": "all", "label": "All"},
            {"value": "linked", "label": "Linked"},
            {"value": "unlinked", "label": "Unlinked"},
        ],
    }


def _find_spare_source_file(data_dir: Path, spec):
    env_path = clean_text(os.environ.get(spec["env"]))
    if env_path:
        candidate = Path(env_path)
        if candidate.exists():
            return candidate, False

    preferred_aliases = spec.get("preferred_aliases") or []
    remaining_aliases = [alias for alias in (spec.get("aliases") or []) if alias not in preferred_aliases]
    base_names = [*preferred_aliases, spec["base"], *remaining_aliases]
    for base in base_names:
        for suffix in (".xlsx", ".xls", ".csv"):
            candidate = data_dir / f"{base}{suffix}"
            if candidate.exists():
                return candidate, False

    fallback_factory = spec.get("fallback")
    fallback = fallback_factory() if callable(fallback_factory) else None
    if fallback and Path(fallback).exists():
        return Path(fallback), True
    return None, False


_FUTURE_SOURCES_CACHE: dict = {"result": None, "dir_mtime": None}


def _resolve_future_sources(data_dir: Path):
    try:
        dir_mtime = data_dir.stat().st_mtime
    except OSError:
        dir_mtime = None
    cached = _FUTURE_SOURCES_CACHE
    if cached["result"] is not None and cached["dir_mtime"] == dir_mtime:
        return cached["result"]

    paths = {}
    status = {}
    for key, spec in FUTURE_SOURCE_SPECS.items():
        path, using_fallback = _find_spare_source_file(data_dir, spec)
        if path:
            paths[key] = path
            status[key] = {
                "label": spec["label"],
                "uploaded": not using_fallback,
                "available": True,
                "using_fallback": using_fallback,
                "file_name": path.name,
                "message": (
                    f"Using {path.name}"
                    if not using_fallback
                    else f"{spec['label']} not uploaded; using {path.name} until {spec['base']}.xlsx or .csv is uploaded"
                ),
            }
        else:
            paths[key] = None
            status[key] = {
                "label": spec["label"],
                "uploaded": False,
                "available": False,
                "using_fallback": False,
                "file_name": None,
                "message": spec["missing"],
            }
    result = (paths, status)
    cached["result"] = result
    cached["dir_mtime"] = dir_mtime
    return result


def _read_spare_source_table(path: Path | None):
    return _read_spare_source_table_with_sheet(path)


def _read_spare_source_table_with_sheet(path: Path | None, preferred_sheet: str | None = None):
    if not path:
        return pd.DataFrame()
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        if preferred_sheet:
            try:
                return pd.read_excel(path, sheet_name=preferred_sheet)
            except ValueError:
                pass
        return pd.read_excel(path)
    if suffix == ".csv":
        try:
            return pd.read_csv(path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            return pd.read_csv(path, encoding="latin1")
    return pd.DataFrame()


def _column_lookup(frame: pd.DataFrame):
    normalized_columns = {_normalize_key(column): column for column in frame.columns}
    lookup = {}
    for field, aliases in FLEXIBLE_COLUMN_ALIASES.items():
        for alias in aliases:
            column = normalized_columns.get(_normalize_key(alias))
            if column is not None:
                lookup[field] = column
                break
    if "code" not in lookup:
        fallback_code_column = _detect_code_like_column(frame)
        if fallback_code_column is not None:
            lookup["code"] = fallback_code_column
    return lookup


def _detect_code_like_column(frame: pd.DataFrame):
    best_column = None
    best_score = 0
    for column in frame.columns:
        try:
            series = frame[column].dropna().map(clean_text)
        except Exception:
            continue
        values = [value for value in series if value]
        if len(values) < 10:
            continue
        match_count = sum(1 for value in values if CODE_LIKE_VALUE_RE.fullmatch(str(value).upper()))
        if match_count > best_score and match_count >= max(10, int(len(values) * 0.2)):
            best_column = column
            best_score = match_count
    return best_column


def _value_from_row(row, lookup, field):
    column = lookup.get(field)
    if column is None:
        return None
    return row.get(column)


def _date_iso(value):
    parsed = _parse_date(value)
    return parsed.date().isoformat() if parsed else None


def _clean_code(value):
    return clean_text(value).upper() if clean_text(value) else None


def _stock_status(quantity, min_stock, max_stock):
    if quantity is None:
        return "Awaiting data input"
    if float(quantity) == 0:
        return "OUT OF STOCK"
    if min_stock is not None and float(quantity) < float(min_stock):
        return "LOW STOCK"
    if max_stock is not None and float(quantity) > float(max_stock):
        return "OVERSTOCK"
    if min_stock is None and max_stock is None:
        return "Awaiting data input"
    return "NORMAL"


def _stock_health_status(quantity, min_stock, max_stock):
    if quantity is None or min_stock is None or max_stock is None:
        return "Missing Threshold Data"
    if float(min_stock) > float(max_stock):
        return "Threshold Error"
    if float(quantity) < float(min_stock):
        return "Reorder Required"
    if float(quantity) > float(max_stock):
        return "Above Recommended"
    return "Normal"


def _quantity_to_buy(quantity, min_stock, max_stock):
    status = _stock_health_status(quantity, min_stock, max_stock)
    if status == "Reorder Required":
        return round(float(min_stock) - float(quantity), 3)
    if status in {"Normal", "Above Recommended"}:
        return 0
    return None


def _data_quality_flags(record):
    flags = []
    if record.get("missing_part_number") or not clean_text(record.get("code")):
        flags.append("Missing Part Number")
    if record.get("missing_spare_part_name") or not clean_text(record.get("name")):
        flags.append("Missing Spare Part Name")
    if record.get("current_quantity") is None:
        flags.append("Missing Current Stock")
    if record.get("min_stock") is None:
        flags.append("Missing Minimum")
    if record.get("max_stock") is None:
        flags.append("Missing Maximum")
    if record.get("has_negative_stock_quantity") or (record.get("current_quantity") is not None and float(record.get("current_quantity") or 0) < 0):
        flags.append("Negative stock quantity")
    if record.get("min_stock") is not None and record.get("max_stock") is not None and float(record["min_stock"]) > float(record["max_stock"]):
        flags.append("Minimum greater than Maximum")
    if int(record.get("duplicate_count") or 1) > 1:
        flags.append("Duplicate Part Number")
    if record.get("duplicate_threshold_conflict"):
        flags.append("Conflicting duplicate threshold data")
    return flags


def _first_present(values):
    for value in values:
        if clean_text(value) or value == 0:
            return value
    return None


def _unique_clean_values(values):
    cleaned = []
    for value in values:
        text = clean_text(value)
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _merge_duplicate_inventory_records(rows):
    if len(rows) == 1:
        record = {**rows[0], "duplicate_count": 1}
        _finalize_inventory_health(record)
        return record

    numeric_available = [row.get("available_physical") for row in rows if row.get("available_physical") is not None]
    numeric_reserved = [row.get("reserved_physical") for row in rows if row.get("reserved_physical") is not None]
    numeric_total_available = [row.get("total_available") for row in rows if row.get("total_available") is not None]
    numeric_on_order = [row.get("on_order") for row in rows if row.get("on_order") is not None]
    numeric_current = [row.get("current_quantity") for row in rows if row.get("current_quantity") is not None]
    numeric_inventory_value = [row.get("inventory_value") for row in rows if row.get("inventory_value") is not None]
    numeric_stock_value = [row.get("stock_value") for row in rows if row.get("stock_value") is not None]
    min_values = [row.get("min_stock") for row in rows if row.get("min_stock") is not None]
    max_values = [row.get("max_stock") for row in rows if row.get("max_stock") is not None]
    unit_cost_values = [row.get("unit_cost") for row in rows if row.get("unit_cost") is not None]
    locations = _unique_clean_values(row.get("location") for row in rows)
    source_files = _unique_clean_values(row.get("source_file") for row in rows)

    duplicate_threshold_conflict = (
        len({float(value) for value in min_values}) > 1
        or len({float(value) for value in max_values}) > 1
    )
    merged = {
        **rows[0],
        "name": clean_text(_first_present(row.get("name") for row in rows)) or rows[0].get("code") or "Unmatched",
        "description": clean_text(_first_present(row.get("description") for row in rows)) or rows[0].get("name") or rows[0].get("code") or "Unmatched",
        "missing_part_number": all(row.get("missing_part_number") for row in rows),
        "missing_spare_part_name": all(row.get("missing_spare_part_name") for row in rows),
        "has_negative_stock_quantity": any(row.get("has_negative_stock_quantity") for row in rows),
        "category": clean_text(_first_present(row.get("category") for row in rows)) or "Unclassified",
        "item_group": clean_text(_first_present(row.get("item_group") for row in rows)) or clean_text(_first_present(row.get("category") for row in rows)) or "Unclassified",
        "search_name": clean_text(_first_present(row.get("search_name") for row in rows)),
        "translated_name": clean_text(_first_present(row.get("translated_name") for row in rows)),
        "translation_status": clean_text(_first_present(row.get("translation_status") for row in rows)) or "No translation needed",
        "available_physical": round(sum(float(value) for value in numeric_available), 3) if numeric_available else None,
        "reserved_physical": round(sum(float(value) for value in numeric_reserved), 3) if numeric_reserved else None,
        "total_available": round(sum(float(value) for value in numeric_total_available), 3) if numeric_total_available else None,
        "on_order": round(sum(float(value) for value in numeric_on_order), 3) if numeric_on_order else None,
        "current_quantity": round(sum(float(value) for value in numeric_current), 3) if numeric_current else None,
        "unit": clean_text(_first_present(row.get("unit") for row in rows)),
        "min_stock": _first_present(min_values),
        "max_stock": _first_present(max_values),
        "unit_cost": _first_present(unit_cost_values),
        "inventory_value": round(sum(float(value) for value in numeric_inventory_value), 2) if numeric_inventory_value else None,
        "stock_value": round(sum(float(value) for value in numeric_stock_value), 2) if numeric_stock_value else None,
        "location": "; ".join(locations) if locations else "Unmatched",
        "equipment_name": clean_text(_first_present(row.get("equipment_name") for row in rows)),
        "equipment_asset_id": clean_text(_first_present(row.get("equipment_asset_id") for row in rows)),
        "equipment_criticality": clean_text(_first_present(row.get("equipment_criticality") for row in rows)),
        "last_updated": clean_text(_first_present(row.get("last_updated") for row in rows)),
        "source_file": "; ".join(source_files) if source_files else rows[0].get("source_file"),
        "duplicate_count": len(rows),
        "duplicate_threshold_conflict": duplicate_threshold_conflict,
    }
    if merged["stock_value"] is None and merged["current_quantity"] is not None and merged["unit_cost"] is not None:
        merged["stock_value"] = round(float(merged["current_quantity"]) * float(merged["unit_cost"]), 2)
    _finalize_inventory_health(merged)
    return merged


def _finalize_inventory_health(record):
    quantity = record.get("current_quantity")
    min_stock = record.get("min_stock")
    max_stock = record.get("max_stock")
    record["stock_status"] = _stock_status(quantity, min_stock, max_stock)
    record["stock_health_status"] = _stock_health_status(quantity, min_stock, max_stock)
    record["stock_status_group"] = _inventory_stock_status_group(quantity, min_stock, max_stock)
    record["quantity_to_buy"] = _quantity_to_buy(quantity, min_stock, max_stock)
    record["estimated_reorder_cost"] = (
        round(float(record["quantity_to_buy"]) * float(record["unit_cost"]), 2)
        if record.get("quantity_to_buy") is not None and record.get("unit_cost") is not None
        else None
    )
    record["stock_health_percent_valid"] = (
        quantity is not None
        and min_stock is not None
        and not (max_stock is not None and float(min_stock) > float(max_stock))
    )
    record["stock_health_healthy"] = bool(
        record["stock_health_percent_valid"]
        and float(quantity) >= float(min_stock)
    )
    record["data_quality_flags"] = _data_quality_flags(record)
    return record


def _aggregate_inventory_records(records):
    grouped = defaultdict(list)
    for index, record in enumerate(records):
        key = f"code:{record.get('code')}" if record.get("code") else f"row:{index}"
        grouped[key].append(record)
    return [_merge_duplicate_inventory_records(rows) for rows in grouped.values()]


def _equipment_type_group(value):
    text = _normalize_phrase(value)
    if any(term in text for term in ("refriger", "cool", "freezer", "chiller", "cold")):
        return "Refrigeration / Cooling"
    if any(term in text for term in ("water", "wwtp", "wtp", "ro", "filter tank")):
        return "Water Treatment"
    if any(term in text for term in ("facility", "building", "office", "store", "guardhouse", "canteen")):
        return "Facility / Building"
    if any(term in text for term in ("safety", "fire", "alarm", "cctv", "monitor")):
        return "Safety / Monitoring"
    if any(term in text for term in ("utility", "boiler", "compressor", "air", "steam", "electrical", "mdb")):
        return "Utility Equipment"
    if any(term in text for term in ("production", "machine", "bratt", "oven", "fryer", "conveyor", "evaporator")):
        return "Production Equipment"
    return "Other / Unclassified"


def _turnover_classification(days, used_date=None):
    if days is None:
        return "Dead stock / dormant"
    if days <= 30:
        return "Fast-moving"
    if days <= 90:
        return "Normal"
    if days <= 180:
        return "Slow-moving"
    return "Dead stock / dormant"


def _contains_thai_text(value: str | None) -> bool:
    return bool(THAI_TEXT_RE.search(str(value or "")))


def _safe_translate_text(value: str | None):
    original = clean_text(value)
    if not original:
        return "", "No translation needed"
    if not _contains_thai_text(original):
        return original, "No translation needed"
    try:
        translated = clean_text(_translate_desc(original))
    except Exception:
        translated = ""
    if translated and translated != original:
        return translated, "Translated"
    return original, "Translation failed"


def _normalized_text_tokens(value: str | None):
    return {
        token
        for token in _normalize_part_name(value).split()
        if token
        and len(token) > 1
        and token not in {"for", "the", "and", "with", "size", "model", "color", "white", "black"}
    }


def _combined_keyword_text(*values):
    return " ".join(filter(None, (_normalize_phrase(value) for value in values)))


def _inventory_stock_status_group(quantity, min_stock, max_stock):
    if quantity is None:
        return "Unknown Stock Threshold"
    if float(quantity) <= 0:
        return "Out of Stock"
    if min_stock is not None and float(quantity) < float(min_stock):
        return "Low Stock"
    if max_stock is not None and float(quantity) > float(max_stock):
        return "Overstock"
    return "In Stock"


def _build_inventory_indexes(records):
    by_code = {}
    exact_text = defaultdict(list)
    tokenized = []
    for record in records:
        code = _clean_code(record.get("code"))
        if code:
            by_code[code] = record
        for candidate in {
            _normalize_part_name(record.get("name")),
            _normalize_part_name(record.get("description")),
            _normalize_part_name(record.get("translated_name")),
            _normalize_part_name(record.get("search_name")),
        }:
            if candidate:
                exact_text[candidate].append(record)
        tokens = _normalized_text_tokens(record.get("name")) | _normalized_text_tokens(record.get("description")) | _normalized_text_tokens(record.get("translated_name"))
        if tokens:
            tokenized.append((record, tokens))
    return {"by_code": by_code, "exact_text": exact_text, "tokenized": tokenized}


def _find_inventory_match(code, clean_description, translated_description, original_description, inventory_index):
    normalized_code = _clean_code(code)
    if normalized_code and normalized_code in inventory_index["by_code"]:
        return {
            "record": inventory_index["by_code"][normalized_code],
            "match_status": "Exact Item Code Match",
            "confidence": "High",
            "reason": "Gen PO item code matched the Dynamics inventory item master",
        }

    exact_candidates = []
    for candidate in {
        _normalize_part_name(clean_description),
        _normalize_part_name(translated_description),
        _normalize_part_name(original_description),
    }:
        if candidate:
            exact_candidates.extend(inventory_index["exact_text"].get(candidate, []))
    if exact_candidates:
        return {
            "record": exact_candidates[0],
            "match_status": "Description Match",
            "confidence": "Medium",
            "reason": "Normalized description matched a Dynamics inventory item",
        }

    po_tokens = _normalized_text_tokens(clean_description) | _normalized_text_tokens(translated_description) | _normalized_text_tokens(original_description)
    best_record = None
    best_overlap = 0.0
    for record, inv_tokens in inventory_index["tokenized"]:
        if not po_tokens or not inv_tokens:
            continue
        overlap = len(po_tokens & inv_tokens) / max(1, min(len(po_tokens), len(inv_tokens)))
        if overlap > best_overlap:
            best_overlap = overlap
            best_record = record
    if best_record is not None and best_overlap >= 0.75:
        return {
            "record": best_record,
            "match_status": "Description Match",
            "confidence": "Medium",
            "reason": f"Keyword overlap with a Dynamics inventory item was {round(best_overlap * 100, 1)}%",
        }
    return {
        "record": None,
        "match_status": "No Inventory Match",
        "confidence": "Low",
        "reason": "No inventory item code or strong description match was found",
    }


def _review_reasons_for_po(row):
    reasons = []
    if not clean_text(row.get("code")):
        reasons.append("Item code missing")
    if not clean_text(row.get("clean_description")) or len(str(row.get("clean_description") or "").split()) < 2:
        reasons.append("Description too short to classify")
    if row.get("translation_status") == "Translation failed":
        reasons.append("Translation failed")
    if row.get("confidence") in {"Low", "Manual Review"}:
        reasons.append("Classification confidence is low")
    if row.get("inventory_match_status") == "No Inventory Match" and row.get("confidence") == "Low":
        reasons.append("Item not found in inventory and keyword match is weak")
    if row.get("total_cost") is None:
        reasons.append("Total price missing or invalid")
    return list(dict.fromkeys(reasons))


PURCHASE_CLASS_STOCK = "Stock Spare Part"
PURCHASE_CLASS_NON_STOCK = "Non-Stock Spare Part"
PURCHASE_CLASS_SERVICE = "Service / Labour / Repair"
PURCHASE_CLASS_CAPEX = "CAPEX"
PURCHASE_CLASS_CONSUMABLE = "Consumable / Chemical"
PURCHASE_CLASS_OTHER = "Other / Unclassified"
PURCHASE_CLASS_ORDER = [
    PURCHASE_CLASS_STOCK,
    PURCHASE_CLASS_NON_STOCK,
    PURCHASE_CLASS_SERVICE,
    PURCHASE_CLASS_CAPEX,
    PURCHASE_CLASS_CONSUMABLE,
    PURCHASE_CLASS_OTHER,
]
_PO_SERVICE_KEYWORDS = tuple(sorted(set(NON_SPARE_PART_KEYWORDS + (
    "installation",
    "job",
    "maintenance service",
    "overhaul",
    "repair",
    "transportation",
    "work",
))))
_PO_CAPEX_KEYWORDS = (
    "capex",
    "budget",
    "unbudget",
    "project asset",
    "equipment purchase",
    "machine purchase",
    "new equipment",
)
_PO_CONSUMABLE_KEYWORDS = (
    "chemical",
    "coolant",
    "gas",
    "grease",
    "lubricant",
    "oil",
    "refrigerant",
    "salt",
    "solution",
)
_PO_SPARE_HINTS = (
    "direct replacement",
    "electrical part",
    "material",
    "mechanical part",
    "replacement part",
    "spare part",
)
_PO_RECEIVED_STATUS_KEYWORDS = (
    "closed",
    "grn already",
    "receive bill and grn already",
    "received",
    "recive bill and grn already",
)
_PO_OPEN_STATUS_KEYWORDS = (
    "open",
    "pending",
    "waitting",
    "waiting",
    "not receive",
    "not received",
)


def _text_contains_any(text: str, keywords) -> bool:
    return any(keyword in text for keyword in keywords if keyword)


def _po_received_status(raw_status: str | None) -> bool:
    status_text = _normalize_phrase(raw_status)
    return bool(status_text and _text_contains_any(status_text, _PO_RECEIVED_STATUS_KEYWORDS))


def _po_open_status(raw_status: str | None) -> bool:
    status_text = _normalize_phrase(raw_status)
    return bool(status_text and _text_contains_any(status_text, _PO_OPEN_STATUS_KEYWORDS))


def _classify_purchase_line(
    *,
    code,
    description,
    translated_description,
    group_of_cost,
    translated_group_of_cost,
    type_of_cost="",
    sub_cost="",
    unit="",
    supplier_name="",
    pd_machine="",
    translated_pd_machine="",
    inventory_match=None,
):
    inventory_match = inventory_match or {}
    match_record = inventory_match.get("record")
    match_status = inventory_match.get("match_status")
    match_reason = inventory_match.get("reason") or "Matched against current inventory"
    match_confidence = inventory_match.get("confidence") or "Medium"

    code_clean = _clean_code(code)
    combined_text = _combined_keyword_text(
        translated_description or description,
        translated_group_of_cost or group_of_cost,
        type_of_cost,
        sub_cost,
        unit,
        supplier_name,
        translated_pd_machine or pd_machine,
    )
    has_service_keyword = _text_contains_any(combined_text, _PO_SERVICE_KEYWORDS)
    has_capex_keyword = _text_contains_any(combined_text, _PO_CAPEX_KEYWORDS)
    has_consumable_keyword = _text_contains_any(combined_text, _PO_CONSUMABLE_KEYWORDS)
    has_spare_keyword = _text_contains_any(combined_text, SPARE_PART_KEYWORDS) or _text_contains_any(combined_text, _PO_SPARE_HINTS)

    if match_record and match_status == "Exact Item Code Match":
        return PURCHASE_CLASS_STOCK, "High", match_reason
    if code_clean.startswith("SFST34"):
        return PURCHASE_CLASS_STOCK, "Medium", "Item number prefix suggests a stocked spare part"
    if match_record and not (has_service_keyword or has_capex_keyword or has_consumable_keyword):
        return PURCHASE_CLASS_STOCK, match_confidence, match_reason
    if has_capex_keyword:
        return PURCHASE_CLASS_CAPEX, "High", "CAPEX or project-asset keyword matched type, group, or description"
    if has_service_keyword:
        return PURCHASE_CLASS_SERVICE, "High", "Service, labour, repair, cleaning, or contractor keyword matched the purchase context"
    if has_consumable_keyword:
        return PURCHASE_CLASS_CONSUMABLE, "High", "Consumable, chemical, refrigerant, oil, or gas keyword matched the purchase context"
    if code_clean.startswith("SFST81") or code_clean.startswith("SFST82"):
        return PURCHASE_CLASS_NON_STOCK, "High", "Item number prefix suggests a non-stock or direct-purchase spare part"
    if has_spare_keyword:
        return PURCHASE_CLASS_NON_STOCK, "Medium", "Part, material, or replacement keyword matched the purchase context"
    if clean_text(description or translated_description) and clean_text(group_of_cost or type_of_cost or supplier_name or pd_machine):
        return PURCHASE_CLASS_NON_STOCK, "Low", "Purchase context exists, but the row does not match current on-hand inventory"
    return PURCHASE_CLASS_OTHER, "Low", "The row could not be reliably classified from the available fields"


def _classify_po_item(code, description, master_codes, work_order_id=None, asset_id=None, source_classification=None):
    raw_class = clean_text(source_classification)
    if raw_class in {"Spare Part", "Non-Spare Part", "Manual Review"}:
        return raw_class, "High", "Classification provided by source file"

    normalized_code = _clean_code(code)
    text = _normalize_phrase(description)
    has_spare_keyword = any(keyword in text for keyword in SPARE_PART_KEYWORDS)
    has_non_spare_keyword = any(keyword in text for keyword in NON_SPARE_PART_KEYWORDS)
    has_maintenance_link = bool(clean_text(work_order_id) or clean_text(asset_id))

    if normalized_code and normalized_code in master_codes:
        return "Spare Part", "High", "Item code matched Spare Parts Master"
    if has_spare_keyword and not has_non_spare_keyword:
        confidence = "High" if has_maintenance_link else "Medium"
        reason = "Spare-part keyword matched description"
        if has_maintenance_link:
            reason += " and item is linked to work order or asset"
        return "Spare Part", confidence, reason
    if has_non_spare_keyword and not has_spare_keyword:
        return "Non-Spare Part", "High", "Service, labour, or non-part keyword matched description"
    if has_maintenance_link:
        return "Manual Review", "Low", "Maintenance-related link found but item description is unclear"
    return "Manual Review", "Low", "No item master, keyword, work order, or asset match"


def _build_inventory_records(path: Path | None, source_status):
    records = []
    if not path:
        return records
    frame = _read_spare_source_table(path)
    lookup = _column_lookup(frame)
    for _, row in frame.iterrows():
        code = _clean_code(_value_from_row(row, lookup, "code"))
        name = clean_text(_value_from_row(row, lookup, "name")) or clean_text(_value_from_row(row, lookup, "description"))
        search_name = clean_text(_value_from_row(row, lookup, "search_name"))
        if not code and not name:
            continue
        translated_name, translation_status = _safe_translate_text(name or search_name)
        available_physical = _clean_numeric(_value_from_row(row, lookup, "available_physical"))
        total_available = _clean_numeric(_value_from_row(row, lookup, "total_available"))
        quantity = available_physical if available_physical is not None else total_available
        reserved_physical = _clean_numeric(_value_from_row(row, lookup, "reserved_physical"))
        on_order = _clean_numeric(_value_from_row(row, lookup, "on_order"))
        min_stock = _clean_numeric(_value_from_row(row, lookup, "min_stock"))
        max_stock = _clean_numeric(_value_from_row(row, lookup, "max_stock"))
        unit_cost = _clean_numeric(_value_from_row(row, lookup, "unit_cost"))
        inventory_value = _clean_numeric(_value_from_row(row, lookup, "inventory_value"))
        stock_value = inventory_value
        if stock_value is None and quantity is not None and unit_cost is not None:
            stock_value = round(float(quantity) * float(unit_cost), 2)
        equipment_name = clean_text(_value_from_row(row, lookup, "equipment_name"))
        equipment_asset_id = _clean_code(_value_from_row(row, lookup, "asset_id"))
        records.append({
            "code": code,
            "name": name or code or "Unmatched",
            "description": clean_text(_value_from_row(row, lookup, "description")) or name or code or "Unmatched",
            "search_name": search_name,
            "translated_name": translated_name or name or code or "Unmatched",
            "translation_status": translation_status,
            "missing_part_number": not bool(code),
            "missing_spare_part_name": not bool(name),
            "has_negative_stock_quantity": quantity is not None and float(quantity) < 0,
            "category": clean_text(_value_from_row(row, lookup, "category")) or "Unclassified",
            "item_group": clean_text(_value_from_row(row, lookup, "item_group") or _value_from_row(row, lookup, "category")) or "Unclassified",
            "available_physical": available_physical,
            "reserved_physical": reserved_physical,
            "total_available": total_available,
            "on_order": on_order,
            "current_quantity": quantity,
            "unit": clean_text(_value_from_row(row, lookup, "unit")),
            "min_stock": min_stock,
            "max_stock": max_stock,
            "unit_cost": unit_cost,
            "inventory_value": inventory_value,
            "stock_value": stock_value,
            "location": clean_text(_value_from_row(row, lookup, "location")) or "Unmatched",
            "equipment_name": equipment_name,
            "equipment_asset_id": equipment_asset_id,
            "equipment_criticality": clean_text(_value_from_row(row, lookup, "criticality")),
            "last_updated": _date_iso(_value_from_row(row, lookup, "last_updated")),
            "source_file": source_status.get("file_name"),
        })
    return _aggregate_inventory_records(records)


def _build_movement_records(path: Path | None, source_status):
    records = []
    if not path:
        return records
    frame = _read_spare_source_table(path)
    lookup = _column_lookup(frame)
    for _, row in frame.iterrows():
        code = _clean_code(_value_from_row(row, lookup, "code"))
        name = clean_text(_value_from_row(row, lookup, "name")) or clean_text(_value_from_row(row, lookup, "description"))
        if not code and not name:
            continue
        transaction_type = clean_text(_value_from_row(row, lookup, "transaction_type"))
        quantity_drawn = _clean_numeric(_value_from_row(row, lookup, "quantity_drawn"))
        quantity_received = _clean_numeric(_value_from_row(row, lookup, "quantity_received"))
        quantity = _clean_numeric(_value_from_row(row, lookup, "quantity"))
        if quantity_drawn is None and transaction_type and any(term in transaction_type.lower() for term in ("issue", "draw", "use", "out")):
            quantity_drawn = quantity
        if quantity_received is None and transaction_type and any(term in transaction_type.lower() for term in ("receive", "receipt", "in")):
            quantity_received = quantity
        if quantity_drawn is None and quantity_received is None:
            quantity_drawn = quantity
        unit_cost = _clean_numeric(_value_from_row(row, lookup, "unit_cost"))
        date_value = _date_iso(_value_from_row(row, lookup, "transaction_date") or _value_from_row(row, lookup, "issue_date"))
        records.append({
            "date": date_value,
            "code": code,
            "name": name or code or "Unmatched",
            "category": clean_text(_value_from_row(row, lookup, "category")) or "Unclassified",
            "quantity_drawn": quantity_drawn,
            "quantity_received": quantity_received,
            "unit_cost": unit_cost,
            "value": round(float(quantity_drawn or 0) * float(unit_cost or 0), 2) if quantity_drawn is not None and unit_cost is not None else None,
            "work_order_id": clean_text(_value_from_row(row, lookup, "work_order_id")),
            "asset_id": _clean_code(_value_from_row(row, lookup, "asset_id")),
            "issued_by": clean_text(_value_from_row(row, lookup, "issued_by")),
            "transaction_type": transaction_type or ("Receipt" if quantity_received is not None else "Issue"),
            "source_file": source_status.get("file_name"),
        })
    return records


def _build_equipment_master_records(path: Path | None):
    records = []
    if not path:
        return records
    frame = _read_spare_source_table(path)
    lookup = _column_lookup(frame)
    for _, row in frame.iterrows():
        asset_id = _clean_code(_value_from_row(row, lookup, "asset_id") or _value_from_row(row, lookup, "code"))
        name = clean_text(_value_from_row(row, lookup, "equipment_name") or _value_from_row(row, lookup, "name"))
        if not asset_id and not name:
            continue
        raw_type = clean_text(_value_from_row(row, lookup, "equipment_type") or _value_from_row(row, lookup, "category"))
        records.append({
            "asset_id": asset_id,
            "equipment_name": name or asset_id or "Unmatched",
            "equipment_type": _equipment_type_group(raw_type or name),
            "raw_equipment_type": raw_type,
            "equipment_criticality": clean_text(_value_from_row(row, lookup, "criticality")) or "Unclassified",
            "production_line": clean_text(_value_from_row(row, lookup, "production_line") or _value_from_row(row, lookup, "location")),
            "location": clean_text(_value_from_row(row, lookup, "location")),
        })
    return records


def _enrich_inventory_with_equipment(records, equipment_records):
    if not records:
        return records
    by_asset = {row.get("asset_id"): row for row in equipment_records if row.get("asset_id")}
    by_name = {
        _normalize_phrase(row.get("equipment_name")): row
        for row in equipment_records
        if row.get("equipment_name")
    }
    for record in records:
        asset_id = _clean_code(record.get("equipment_asset_id"))
        equipment_name = clean_text(record.get("equipment_name"))
        equipment = by_asset.get(asset_id) if asset_id else None
        if not equipment and equipment_name:
            equipment = by_name.get(_normalize_phrase(equipment_name))
        if equipment:
            record["equipment_asset_id"] = record.get("equipment_asset_id") or equipment.get("asset_id")
            record["equipment_name"] = equipment_name or equipment.get("equipment_name")
            record["equipment_type"] = equipment.get("equipment_type")
            record["equipment_criticality"] = record.get("equipment_criticality") or equipment.get("equipment_criticality")
        else:
            record["equipment_name"] = equipment_name or record.get("equipment_asset_id") or None
            record["equipment_type"] = record.get("equipment_type") or "Unclassified"
            record["equipment_criticality"] = record.get("equipment_criticality") or "Unclassified"
    return records


def _build_work_order_records(path: Path | None):
    records = []
    if not path:
        return records
    frame = _read_spare_source_table(path)
    lookup = _column_lookup(frame)
    for _, row in frame.iterrows():
        work_order_id = clean_text(_value_from_row(row, lookup, "work_order_id"))
        asset_id = _clean_code(_value_from_row(row, lookup, "asset_id"))
        if not work_order_id and not asset_id:
            continue
        raw_type = clean_text(_value_from_row(row, lookup, "equipment_type"))
        records.append({
            "work_order_id": work_order_id,
            "asset_id": asset_id,
            "equipment_name": clean_text(_value_from_row(row, lookup, "equipment_name")),
            "equipment_type": _equipment_type_group(raw_type or _value_from_row(row, lookup, "equipment_name")),
            "equipment_criticality": clean_text(_value_from_row(row, lookup, "criticality")) or "Unclassified",
            "maintenance_type": clean_text(_value_from_row(row, lookup, "maintenance_type")) or "Unclassified",
            "actual_start_date": _date_iso(_value_from_row(row, lookup, "transaction_date") or _value_from_row(row, lookup, "po_date")),
        })
    return records


def _build_po_records(path: Path | None, source_status, master_codes):
    records = []
    if not path:
        return records
    frame = _read_spare_source_table_with_sheet(path, preferred_sheet="Gen PO in D365 Rev.01")
    lookup = _column_lookup(frame)
    for _, row in frame.iterrows():
        code = _clean_code(_value_from_row(row, lookup, "code"))
        description = clean_text(_value_from_row(row, lookup, "description") or _value_from_row(row, lookup, "name"))
        po_number = clean_text(_value_from_row(row, lookup, "po_number"))
        if not code and not description and not po_number:
            continue
        translated_description, translation_status = _safe_translate_text(description)
        group_of_cost = clean_text(_value_from_row(row, lookup, "group_of_cost") or _value_from_row(row, lookup, "category"))
        translated_group_of_cost, group_translation_status = _safe_translate_text(group_of_cost)
        pd_machine = clean_text(_value_from_row(row, lookup, "pd_machine") or _value_from_row(row, lookup, "asset_id"))
        translated_pd_machine, machine_translation_status = _safe_translate_text(pd_machine)
        type_of_cost = clean_text(_value_from_row(row, lookup, "type_of_cost"))
        sub_cost = clean_text(_value_from_row(row, lookup, "sub_cost"))
        quantity = _clean_numeric(_value_from_row(row, lookup, "quantity_ordered") or _value_from_row(row, lookup, "quantity"))
        unit_cost = _clean_numeric(_value_from_row(row, lookup, "unit_cost"))
        total_cost = _clean_numeric(_value_from_row(row, lookup, "total_cost"))
        if total_cost is None and quantity is not None and unit_cost is not None:
            total_cost = round(float(quantity) * float(unit_cost), 2)
        work_order_id = clean_text(_value_from_row(row, lookup, "work_order_id"))
        asset_id = _clean_code(_value_from_row(row, lookup, "asset_id"))
        supplier_name = clean_text(_value_from_row(row, lookup, "supplier"))
        unit = clean_text(_value_from_row(row, lookup, "unit"))
        clean_description = _normalize_part_name(translated_description or description)
        inventory_match_status = "No Inventory Match"
        inventory_match_reason = "No inventory item code or strong description match was found"
        inventory_match_confidence = "Low"
        if master_codes:
            # backward-compatible master-code check remains available for callers that only pass code sets
            if code and code in master_codes:
                inventory_match_status = "Exact Item Code Match"
                inventory_match_reason = "Gen PO item code matched the Dynamics inventory item master"
                inventory_match_confidence = "High"
        classification, confidence, reason = _classify_purchase_line(
            code=code,
            description=description,
            translated_description=translated_description,
            group_of_cost=group_of_cost,
            translated_group_of_cost=translated_group_of_cost,
            type_of_cost=type_of_cost,
            sub_cost=sub_cost,
            unit=unit,
            supplier_name=supplier_name,
            pd_machine=pd_machine,
            translated_pd_machine=translated_pd_machine,
            inventory_match={
                "record": {"code": code} if inventory_match_status == "Exact Item Code Match" else None,
                "match_status": inventory_match_status,
                "confidence": inventory_match_confidence,
                "reason": inventory_match_reason,
            },
        )
        status_text = clean_text(_value_from_row(row, lookup, "grn_status"))
        goods_received_date = _date_iso(_value_from_row(row, lookup, "goods_received_date"))
        po_date = _date_iso(_value_from_row(row, lookup, "po_date"))
        completed = _po_received_status(status_text)
        delivery_flag = "Pending"
        if completed:
            kpi_status = clean_text(_value_from_row(row, lookup, "kpi_status"))
            delivery_flag = "Ontime" if "ontime" in kpi_status.lower() else "Delivered"
            if "over" in kpi_status.lower() or "delay" in kpi_status.lower():
                delivery_flag = "Delayed"

        records.append({
            "po_date": po_date,
            "goods_received_date": goods_received_date,
            "po_number": po_number,
            "code": code,
            "description": description or code or "Unmatched",
            "original_description": description or code or "Unmatched",
            "translated_description": translated_description or description or code or "Unmatched",
            "clean_description": clean_description or _normalize_part_name(description or code),
            "quantity_ordered": quantity,
            "quantity_received": _clean_numeric(_value_from_row(row, lookup, "quantity_received")) or quantity,
            "unit": unit,
            "unit_cost": unit_cost,
            "total_cost": total_cost,
            "supplier": supplier_name or "Unmatched",
            "vendor_name": supplier_name or "Unmatched",
            "work_order_id": work_order_id,
            "asset_id": asset_id,
            "group_of_cost": group_of_cost,
            "translated_group_of_cost": translated_group_of_cost or group_of_cost,
            "type_of_cost": type_of_cost,
            "sub_cost": sub_cost,
            "pd_machine": pd_machine,
            "translated_pd_machine": translated_pd_machine or pd_machine,
            "classification": classification,
            "purchase_classification": classification,
            "confidence": confidence,
            "classification_reason": reason,
            "translation_status": "Translation failed" if "Translation failed" in {translation_status, group_translation_status, machine_translation_status} else ("Translated" if "Translated" in {translation_status, group_translation_status, machine_translation_status} else "No translation needed"),
            "inventory_match_status": inventory_match_status,
            "inventory_match_confidence": inventory_match_confidence,
            "inventory_match_reason": inventory_match_reason,
            "inventory_match_code": None,
            "inventory_match_name": None,
            "department": clean_text(_value_from_row(row, lookup, "department")),
            "gl_account": clean_text(_value_from_row(row, lookup, "gl_account")),
            "grn_no": clean_text(_value_from_row(row, lookup, "grn_no")),
            "grn_status": status_text,
            "kpi_status": clean_text(_value_from_row(row, lookup, "kpi_status")),
            "source_stage": "Unmapped Stage",
            "completed": completed,
            "delivery_flag": delivery_flag,
            "lead_time_days": _clean_numeric(_value_from_row(row, lookup, "lead_time_days")),
            "actual_lead_days": _clean_numeric(_value_from_row(row, lookup, "grn_po_days")),
            "used_po_date_as_gr_fallback": bool(completed and not goods_received_date and po_date),
            "source_file": source_status.get("file_name"),
        })
    return records


def _is_spare_purchase_classification(classification: str | None) -> bool:
    return classification in {PURCHASE_CLASS_STOCK, PURCHASE_CLASS_NON_STOCK}


def _refine_po_records_with_inventory(po_records, inventory_records):
    inventory_index = _build_inventory_indexes(inventory_records)
    for row in po_records:
        match = _find_inventory_match(
            row.get("code"),
            row.get("clean_description"),
            row.get("translated_description"),
            row.get("original_description"),
            inventory_index,
        )
        matched_record = match.get("record")
        row["inventory_match_status"] = match.get("match_status")
        row["inventory_match_confidence"] = match.get("confidence")
        row["inventory_match_reason"] = match.get("reason")
        row["inventory_match_code"] = matched_record.get("code") if matched_record else None
        row["inventory_match_name"] = matched_record.get("name") if matched_record else None

        if matched_record and row.get("classification") not in {PURCHASE_CLASS_SERVICE, PURCHASE_CLASS_CAPEX, PURCHASE_CLASS_CONSUMABLE}:
            row["classification"] = PURCHASE_CLASS_STOCK
            row["purchase_classification"] = PURCHASE_CLASS_STOCK
            row["confidence"] = match.get("confidence") or row.get("confidence")
            row["classification_reason"] = match.get("reason") or row.get("classification_reason")
        elif row.get("classification") == PURCHASE_CLASS_OTHER and row.get("inventory_match_status") == "No Inventory Match":
            row["confidence"] = "Low"

        review_reasons = _review_reasons_for_po(row)
        if row.get("classification") == PURCHASE_CLASS_OTHER and row.get("classification_reason"):
            review_reasons.append(row.get("classification_reason"))
        row["review_reasons"] = list(dict.fromkeys(review_reasons))
        row["needs_manual_review"] = bool(row["review_reasons"]) or row.get("classification") == PURCHASE_CLASS_OTHER
    return po_records


def _load_stage_goods_received_rows():
    try:
        import spare_parts_views as spv
    except Exception:
        return [], {}
    try:
        rows, status = spv.get_goods_received_rows()
        return rows or [], status or {}
    except Exception:
        return [], {}


def _stage_po_source_signatures():
    try:
        import spare_parts_views as spv
    except Exception:
        return ()
    signatures = []
    for stage in ("Stage 1", "Stage 2"):
        try:
            signatures.append(_file_signature(spv.resolve_gen_po_file(stage)))
        except Exception:
            signatures.append(None)
    return tuple(signatures)


def _build_po_records_from_stage_rows(received_rows, inventory_records, stage_status=None):
    stage_status = stage_status or {}
    inventory_index = _build_inventory_indexes(inventory_records)
    records = []
    for row in received_rows or []:
        code = _clean_code(row.get("item_number"))
        description = clean_text(row.get("description"))
        po_number = clean_text(row.get("po_no"))
        if not code and not description and not po_number:
            continue
        translated_description, translation_status = _safe_translate_text(description)
        group_of_cost = clean_text(row.get("group_of_cost"))
        translated_group_of_cost, group_translation_status = _safe_translate_text(group_of_cost)
        pd_machine = clean_text(row.get("pd_machine"))
        translated_pd_machine, machine_translation_status = _safe_translate_text(pd_machine)
        type_of_cost = clean_text(row.get("type_of_cost"))
        sub_cost = clean_text(row.get("sub_cost"))
        unit = clean_text(row.get("unit"))
        supplier_name = clean_text(row.get("vendor"))
        quantity = _clean_numeric(row.get("qty"))
        unit_cost = _clean_numeric(row.get("price_unit"))
        total_cost = _clean_numeric(row.get("total_price"))
        if total_cost is None and quantity is not None and unit_cost is not None:
            total_cost = round(float(quantity) * float(unit_cost), 2)

        match = _find_inventory_match(
            code,
            _normalize_part_name(translated_description or description),
            translated_description or description,
            description,
            inventory_index,
        )
        matched_record = match.get("record")
        classification, confidence, reason = _classify_purchase_line(
            code=code,
            description=description,
            translated_description=translated_description,
            group_of_cost=group_of_cost,
            translated_group_of_cost=translated_group_of_cost,
            type_of_cost=type_of_cost,
            sub_cost=sub_cost,
            unit=unit,
            supplier_name=supplier_name,
            pd_machine=pd_machine,
            translated_pd_machine=translated_pd_machine,
            inventory_match=match,
        )

        status_text = clean_text(row.get("grn_status_raw") or row.get("grn_status"))
        kpi_status = clean_text(row.get("kpi_status"))
        po_date = _date_iso(row.get("date_gen_po"))
        goods_received_date = _date_iso(row.get("date_receive_bill"))
        completed = bool(row.get("completed")) or _po_received_status(status_text)
        lead_time_days = _clean_numeric(row.get("lead_time_days"))
        actual_lead_days = _clean_numeric(row.get("grn_po_days"))
        delay_days = None
        delivery_flag = "Pending"
        if completed:
            if actual_lead_days is not None and lead_time_days is not None:
                delay_days = max(0, round(float(actual_lead_days) - float(lead_time_days), 1))
                delivery_flag = "Ontime" if delay_days <= 0 else "Delayed"
            elif "over" in kpi_status.lower() or "delay" in kpi_status.lower():
                delivery_flag = "Delayed"
            elif "ontime" in kpi_status.lower():
                delivery_flag = "Ontime"
            else:
                delivery_flag = "Delivered"
        source_stage = clean_text(row.get("stage")) or "Unmapped Stage"
        records.append({
            "po_date": po_date,
            "goods_received_date": goods_received_date,
            "po_number": po_number,
            "code": code,
            "description": description or code or "Unmatched",
            "original_description": description or code or "Unmatched",
            "translated_description": translated_description or description or code or "Unmatched",
            "clean_description": _normalize_part_name(translated_description or description or code),
            "quantity_ordered": quantity,
            "quantity_received": quantity if completed else None,
            "unit": unit,
            "unit_cost": unit_cost,
            "total_cost": total_cost,
            "supplier": supplier_name or "Unmatched",
            "vendor_name": supplier_name or "Unmatched",
            "work_order_id": clean_text(row.get("pr_no")),
            "asset_id": _clean_code(pd_machine),
            "group_of_cost": group_of_cost,
            "translated_group_of_cost": translated_group_of_cost or group_of_cost,
            "type_of_cost": type_of_cost,
            "sub_cost": sub_cost,
            "pd_machine": pd_machine,
            "translated_pd_machine": translated_pd_machine or pd_machine,
            "classification": classification,
            "purchase_classification": classification,
            "confidence": confidence,
            "classification_reason": reason,
            "translation_status": "Translation failed" if "Translation failed" in {translation_status, group_translation_status, machine_translation_status} else ("Translated" if "Translated" in {translation_status, group_translation_status, machine_translation_status} else "No translation needed"),
            "inventory_match_status": match.get("match_status"),
            "inventory_match_confidence": match.get("confidence"),
            "inventory_match_reason": match.get("reason"),
            "inventory_match_code": matched_record.get("code") if matched_record else None,
            "inventory_match_name": matched_record.get("name") if matched_record else None,
            "department": clean_text(row.get("customer_group")),
            "gl_account": "",
            "grn_no": clean_text(row.get("grn_no")),
            "grn_status": status_text,
            "kpi_status": kpi_status,
            "source_file": (stage_status.get(source_stage, {}) or {}).get("file_name"),
            "source_stage": source_stage,
            "completed": completed,
            "delivery_flag": delivery_flag,
            "delay_days": delay_days,
            "lead_time_days": lead_time_days,
            "actual_lead_days": actual_lead_days,
            "used_po_date_as_gr_fallback": bool(completed and not goods_received_date and po_date),
            "note": clean_text(row.get("note")),
        })
    return records


def _set_max_date(target, candidate):
    if candidate and (target is None or str(candidate) > str(target)):
        return candidate
    return target


def _build_inventory_purchase_summary_rows(inventory_records, po_records):
    summary_rows = {}
    for inventory in inventory_records:
        key = f"inventory::{inventory.get('code') or _normalize_part_name(inventory.get('name'))}"
        summary_rows[key] = {
            "item_code": inventory.get("code"),
            "item_name": inventory.get("name") or inventory.get("description") or inventory.get("code") or "Unmatched",
            "translated_item_name": inventory.get("translated_name") or inventory.get("name") or inventory.get("description") or inventory.get("code") or "Unmatched",
            "current_available_stock": inventory.get("current_quantity"),
            "inventory_unit": inventory.get("unit"),
            "item_group": inventory.get("item_group") or inventory.get("category") or "Unclassified",
            "po_quantity_purchased": 0,
            "po_total_value": 0,
            "vendor_names": set(),
            "vendor_count": 0,
            "last_po_date": None,
            "match_status": "In Inventory",
            "classification": "Current Inventory",
            "confidence": "High",
            "stock_status": inventory.get("stock_status_group"),
        }

    for row in po_records:
        if not _is_spare_purchase_classification(row.get("classification")):
            continue
        match_code = row.get("inventory_match_code")
        if match_code and f"inventory::{match_code}" in summary_rows:
            key = f"inventory::{match_code}"
            summary = summary_rows[key]
        else:
            key = f"direct::{row.get('clean_description') or row.get('translated_description') or row.get('original_description') or row.get('po_number')}"
            summary = summary_rows.setdefault(key, {
                "item_code": row.get("code"),
                "item_name": row.get("translated_description") or row.get("original_description") or row.get("code") or "Unmatched",
                "translated_item_name": row.get("translated_description") or row.get("original_description") or row.get("code") or "Unmatched",
                "current_available_stock": None,
                "inventory_unit": row.get("unit"),
                "item_group": row.get("group_of_cost") or "Direct Purchase",
                "po_quantity_purchased": 0,
                "po_total_value": 0,
                "vendor_names": set(),
                "vendor_count": 0,
                "last_po_date": None,
                "match_status": row.get("inventory_match_status") or "No Inventory Match",
                "classification": row.get("classification"),
                "confidence": row.get("confidence"),
                "stock_status": None,
            })
        summary["po_quantity_purchased"] += float(row.get("quantity_ordered") or 0)
        summary["po_total_value"] += float(row.get("total_cost") or 0)
        vendor_name = clean_text(row.get("vendor_name") or row.get("supplier"))
        if vendor_name:
            summary["vendor_names"].add(vendor_name)
        summary["vendor_count"] = len(summary["vendor_names"])
        summary["last_po_date"] = _set_max_date(summary.get("last_po_date"), row.get("po_date"))
        if summary.get("match_status") in {"In Inventory", None, ""}:
            summary["match_status"] = row.get("inventory_match_status") or summary.get("match_status")
        if summary.get("classification") == "Current Inventory" and row.get("classification"):
            summary["classification"] = row.get("classification")
            summary["confidence"] = row.get("confidence")

    result = []
    for row in summary_rows.values():
        row["po_quantity_purchased"] = round(float(row.get("po_quantity_purchased") or 0), 3)
        row["po_total_value"] = round(float(row.get("po_total_value") or 0), 2)
        row["vendor_count"] = len(row.get("vendor_names") or set())
        row["vendor_names"] = sorted(row.get("vendor_names") or [])
        result.append(row)
    result.sort(key=lambda item: (-float(item.get("po_total_value") or 0), str(item.get("item_name") or "")))
    return result


def _build_top_external_spare_parts_rows(po_records):
    grouped = {}
    for row in po_records:
        if not _is_spare_purchase_classification(row.get("classification")):
            continue
        key = row.get("inventory_match_code") or row.get("clean_description") or row.get("translated_description") or row.get("original_description") or row.get("po_number")
        grouped.setdefault(key, {
            "clean_description": row.get("clean_description") or row.get("translated_description") or row.get("original_description") or row.get("code") or "Unmatched",
            "translated_description": row.get("translated_description") or row.get("original_description") or row.get("code") or "Unmatched",
            "classification": row.get("classification"),
            "total_quantity_purchased": 0,
            "total_po_value": 0,
            "po_line_count": 0,
            "vendor_names": set(),
            "last_purchase_date": None,
            "inventory_match_status": row.get("inventory_match_status") or "No Inventory Match",
        })
        item = grouped[key]
        item["total_quantity_purchased"] += float(row.get("quantity_ordered") or 0)
        item["total_po_value"] += float(row.get("total_cost") or 0)
        item["po_line_count"] += 1
        vendor_name = clean_text(row.get("vendor_name") or row.get("supplier"))
        if vendor_name:
            item["vendor_names"].add(vendor_name)
        item["last_purchase_date"] = _set_max_date(item.get("last_purchase_date"), row.get("po_date"))

    result = []
    for item in grouped.values():
        item["total_quantity_purchased"] = round(float(item.get("total_quantity_purchased") or 0), 3)
        item["total_po_value"] = round(float(item.get("total_po_value") or 0), 2)
        item["vendor_count"] = len(item.get("vendor_names") or set())
        item["vendor_names"] = sorted(item.get("vendor_names") or [])
        result.append(item)
    result.sort(key=lambda row: (-float(row.get("total_po_value") or 0), str(row.get("clean_description") or "")))
    return result


def _build_turnover_records(po_records, movement_records, inventory_lookup):
    receipts_by_key = defaultdict(list)
    for po in po_records:
        if not _is_spare_purchase_classification(po.get("classification")):
            continue
        key = po.get("code") or _normalize_part_name(po.get("description"))
        if not key:
            continue
        received_date = po.get("goods_received_date") or po.get("po_date")
        quantity = _clean_numeric(po.get("quantity_received") or po.get("quantity_ordered")) or 0
        receipts_by_key[key].append({
            "date": received_date,
            "remaining": float(quantity or 0),
            "quantity": float(quantity or 0),
            "code": po.get("code"),
            "name": po.get("description"),
            "category": inventory_lookup.get(po.get("code"), {}).get("category", "Unclassified"),
            "unit_cost": po.get("unit_cost"),
        })
    for key in receipts_by_key:
        receipts_by_key[key].sort(key=lambda item: item.get("date") or "9999-99-99")

    turnover_rows = []
    issue_rows = [
        row for row in movement_records
        if row.get("quantity_drawn") is not None and float(row.get("quantity_drawn") or 0) > 0
    ]
    issue_rows.sort(key=lambda item: item.get("date") or "9999-99-99")
    for issue in issue_rows:
        key = issue.get("code") or _normalize_part_name(issue.get("name"))
        remaining_issue = float(issue.get("quantity_drawn") or 0)
        matched_any = False
        for receipt in receipts_by_key.get(key, []):
            if remaining_issue <= 0:
                break
            if receipt["remaining"] <= 0:
                continue
            used_qty = min(remaining_issue, receipt["remaining"])
            remaining_issue -= used_qty
            receipt["remaining"] -= used_qty
            received_dt = _parse_date(receipt.get("date"))
            used_dt = _parse_date(issue.get("date"))
            days = (used_dt - received_dt).days if received_dt and used_dt else None
            turnover_rows.append({
                "code": issue.get("code") or receipt.get("code"),
                "name": issue.get("name") or receipt.get("name") or "Unmatched",
                "category": issue.get("category") or receipt.get("category") or "Unclassified",
                "received_date": receipt.get("date"),
                "used_date": issue.get("date"),
                "turnover_days": days if days is not None and days >= 0 else None,
                "quantity": round(used_qty, 3),
                "equipment_used_on": issue.get("asset_id") or "Unmatched",
                "work_order_id": issue.get("work_order_id"),
                "classification": _turnover_classification(days),
            })
            matched_any = True
        if remaining_issue > 0 and not matched_any:
            turnover_rows.append({
                "code": issue.get("code"),
                "name": issue.get("name") or "Unmatched",
                "category": issue.get("category") or "Unclassified",
                "received_date": None,
                "used_date": issue.get("date"),
                "turnover_days": None,
                "quantity": round(remaining_issue, 3),
                "equipment_used_on": issue.get("asset_id") or "Unmatched",
                "work_order_id": issue.get("work_order_id"),
                "classification": "Unmatched",
            })

    for receipts in receipts_by_key.values():
        for receipt in receipts:
            if receipt["remaining"] <= 0:
                continue
            turnover_rows.append({
                "code": receipt.get("code"),
                "name": receipt.get("name") or "Unmatched",
                "category": receipt.get("category") or "Unclassified",
                "received_date": receipt.get("date"),
                "used_date": None,
                "turnover_days": None,
                "quantity": round(receipt["remaining"], 3),
                "equipment_used_on": "Awaiting usage",
                "work_order_id": None,
                "classification": "Dead stock / dormant",
            })
    return turnover_rows


def _build_usage_by_equipment(movement_records, work_orders, equipment_master):
    wo_lookup = {row.get("work_order_id"): row for row in work_orders if row.get("work_order_id")}
    equipment_lookup = {row.get("asset_id"): row for row in equipment_master if row.get("asset_id")}
    usage_rows = []
    for issue in movement_records:
        quantity = issue.get("quantity_drawn")
        if quantity is None or float(quantity or 0) <= 0:
            continue
        wo = wo_lookup.get(issue.get("work_order_id")) or {}
        asset_id = issue.get("asset_id") or wo.get("asset_id")
        equipment = equipment_lookup.get(asset_id) or {}
        equipment_name = wo.get("equipment_name") or equipment.get("equipment_name") or asset_id or "Unmatched"
        raw_type = wo.get("equipment_type") or equipment.get("equipment_type") or equipment_name
        unit_cost = issue.get("unit_cost")
        cost = round(float(quantity or 0) * float(unit_cost or 0), 2) if unit_cost is not None else None
        usage_rows.append({
            "date": issue.get("date"),
            "equipment_type": _equipment_type_group(raw_type),
            "equipment_criticality": wo.get("equipment_criticality") or equipment.get("equipment_criticality") or "Unclassified",
            "equipment_name": equipment_name,
            "asset_id": asset_id,
            "spare_part_code": issue.get("code"),
            "spare_part_name": issue.get("name"),
            "quantity_used": quantity,
            "unit_cost": unit_cost,
            "cost": cost,
            "work_order_id": issue.get("work_order_id"),
            "maintenance_type": wo.get("maintenance_type") or "Unclassified",
        })
    return usage_rows


def _count_by(records, key):
    counts = Counter(row.get(key) or "Unclassified" for row in records)
    return [{"label": label, "count": count} for label, count in sorted(counts.items())]


def _sum_by(records, key, value_key):
    buckets = defaultdict(float)
    for row in records:
        value = row.get(value_key)
        if value is None:
            continue
        buckets[row.get(key) or "Unclassified"] += float(value)
    return [{"label": label, "value": round(value, 2)} for label, value in sorted(buckets.items(), key=lambda item: item[1], reverse=True)]


# ── Inventory valuation via derived unit cost ─────────────────────────────────

_INVENTORY_EXCLUDE_GROUP_KEYWORDS = frozenset({
    "service", "labour", "labor", "repair", "contractor", "cleaning",
    "direct purchase", "non-stock", "nonstock", "capex", "consumable",
    "chemical", "rental",
})


def _is_valued_inventory_item(record) -> bool:
    """True when a record is a physical on-hand stock spare part that should be valued."""
    qty = record.get("current_quantity")
    if qty is None or float(qty) <= 0:
        return False
    group = _normalize_phrase(record.get("item_group") or record.get("category") or "")
    return not any(kw in group for kw in _INVENTORY_EXCLUDE_GROUP_KEYWORDS)


def _build_po_cost_lookup(po_records):
    """Build a received-price cost lookup from GRN-completed spare PO lines.

    Returns (by_code, by_desc): each maps a key to
    {"latest_unit_cost": float, "weighted_unit_cost": float}.
    Only completed (received) lines with a positive unit price and qty are included.
    """
    code_groups: dict[str, list] = defaultdict(list)
    desc_groups: dict[str, list] = defaultdict(list)

    for row in po_records:
        if not row.get("completed"):
            continue
        uc = row.get("unit_cost")
        if uc is None or float(uc) <= 0:
            continue
        qty = _clean_numeric(row.get("quantity_received") or row.get("quantity_ordered"))
        if qty is None or float(qty) <= 0:
            continue
        date_str = str(row.get("goods_received_date") or row.get("po_date") or "")
        entry = {"date": date_str, "unit_cost": float(uc), "qty": float(qty)}

        code = _clean_code(row.get("code"))
        if code:
            code_groups[code].append(entry)

        desc = _normalize_part_name(row.get("clean_description") or row.get("description") or "")
        if len(desc) > 3:
            desc_groups[desc].append(entry)

    def _agg(groups):
        result = {}
        for key, entries in groups.items():
            sorted_entries = sorted(entries, key=lambda e: e["date"], reverse=True)
            latest_uc = sorted_entries[0]["unit_cost"]
            total_qty = sum(e["qty"] for e in entries)
            total_val = sum(e["unit_cost"] * e["qty"] for e in entries)
            weighted_uc = round(total_val / total_qty, 4) if total_qty > 0 else None
            result[key] = {"latest_unit_cost": round(latest_uc, 4), "weighted_unit_cost": weighted_uc}
        return result

    return _agg(code_groups), _agg(desc_groups)


def _derive_inventory_valuation(inventory_records, po_records):
    """Compute current on-hand inventory value using a multi-tier cost derivation.

    Priority order per item:
      1. Inventory file unit_cost (direct)
      2. Inventory file inventory_value / current_quantity
      3. Latest received PO price by item code
      4. Weighted average received PO price by item code
      5. Latest received PO price by normalised description
      6. Weighted average received PO price by normalised description
      7. Unvalued (None) — never forced to zero

    Only counts items where current_quantity > 0 and the item is a stock spare
    (not service, labour, CAPEX, consumable, etc.).

    Mutates each inventory record to add:
      - derived_unit_cost: best available unit cost (float | None)
      - derived_cost_source: string tag for which source was used (str | None)
      - derived_item_value: current_quantity * derived_unit_cost (float | None)
    Also back-fills stock_value when it was previously None so the existing
    frontend fallback (which sums stock_value) benefits automatically.

    Returns a metrics dict.
    """
    po_by_code, po_by_desc = _build_po_cost_lookup(po_records)

    total_in_stock = 0
    valued_count = 0
    unvalued_count = 0
    total_value = 0.0
    has_estimated = False  # True if any PO fallback price was used

    for rec in inventory_records:
        if not _is_valued_inventory_item(rec):
            rec.setdefault("derived_unit_cost", None)
            rec.setdefault("derived_cost_source", None)
            rec.setdefault("derived_item_value", None)
            continue

        qty = float(rec["current_quantity"])
        total_in_stock += 1
        derived_uc = None
        cost_source = None

        # Priority 1: inventory file unit_cost
        inv_uc = rec.get("unit_cost")
        if inv_uc is not None and float(inv_uc) > 0:
            derived_uc = float(inv_uc)
            cost_source = "inventory_file"

        # Priority 1b: derive from inventory_value / qty
        if derived_uc is None:
            inv_val = rec.get("inventory_value")
            if inv_val is not None and float(inv_val) > 0 and qty > 0:
                derived_uc = float(inv_val) / qty
                cost_source = "inventory_value_derived"

        # Priority 2–3: PO match by item code
        if derived_uc is None:
            code = rec.get("code")
            if code and code in po_by_code:
                po = po_by_code[code]
                if po.get("latest_unit_cost"):
                    derived_uc = po["latest_unit_cost"]
                    cost_source = "po_code_latest"
                    has_estimated = True
                elif po.get("weighted_unit_cost"):
                    derived_uc = po["weighted_unit_cost"]
                    cost_source = "po_code_weighted"
                    has_estimated = True

        # Priority 4–5: PO match by normalised description
        if derived_uc is None:
            desc_key = _normalize_part_name(rec.get("name") or rec.get("description") or "")
            if desc_key and desc_key in po_by_desc:
                po = po_by_desc[desc_key]
                if po.get("latest_unit_cost"):
                    derived_uc = po["latest_unit_cost"]
                    cost_source = "po_desc_latest"
                    has_estimated = True
                elif po.get("weighted_unit_cost"):
                    derived_uc = po["weighted_unit_cost"]
                    cost_source = "po_desc_weighted"
                    has_estimated = True

        rec["derived_unit_cost"] = round(derived_uc, 4) if derived_uc is not None else None
        rec["derived_cost_source"] = cost_source

        if derived_uc is not None:
            item_val = round(qty * derived_uc, 2)
            rec["derived_item_value"] = item_val
            # Back-fill stock_value when missing so the frontend aggregate also benefits
            if rec.get("stock_value") is None:
                rec["stock_value"] = item_val
            valued_count += 1
            total_value += qty * derived_uc
        else:
            rec["derived_item_value"] = None
            unvalued_count += 1

    coverage_pct = round(valued_count / total_in_stock * 100, 1) if total_in_stock > 0 else None
    return {
        "current_inventory_value": round(total_value, 2) if valued_count > 0 else None,
        "valued_in_stock_items": valued_count,
        "unvalued_in_stock_items": unvalued_count,
        "total_in_stock_items": total_in_stock,
        "inventory_valuation_coverage_pct": coverage_pct,
        "inventory_value_is_estimated": has_estimated,
    }


def _run_spare_aggregation(
    inventory_records,
    po_records,
    movement_records,
    work_order_records=None,
    equipment_records=None,
    source_status=None,
    errors=None,
):
    """Shared aggregation pipeline used by both the Excel path and the SQL path."""
    work_order_records = work_order_records or []
    equipment_records = equipment_records or []
    source_status = source_status or {}
    errors = list(errors or [])

    # Derive unit costs from PO history for items missing cost data in the inventory
    # file. This mutates each record in-place (adds derived_unit_cost, derived_item_value,
    # derived_cost_source) and back-fills stock_value when it was previously None.
    inv_val_meta = _derive_inventory_valuation(inventory_records, po_records)

    inventory_lookup = {row.get("code"): row for row in inventory_records if row.get("code")}
    turnover_records = _build_turnover_records(po_records, movement_records, inventory_lookup)
    usage_rows = _build_usage_by_equipment(movement_records, work_order_records, equipment_records)

    valid_stock_values = [row["stock_value"] for row in inventory_records if row.get("stock_value") is not None]
    current_quantities = [row["current_quantity"] for row in inventory_records if row.get("current_quantity") is not None]
    health_valid_records = [row for row in inventory_records if row.get("stock_health_percent_valid")]
    healthy_records = [row for row in health_valid_records if row.get("stock_health_healthy")]
    turnover_days = [row["turnover_days"] for row in turnover_records if row.get("turnover_days") is not None]

    stocked_spare_records = [row for row in po_records if row.get("classification") == PURCHASE_CLASS_STOCK]
    non_stock_spare_records = [row for row in po_records if row.get("classification") == PURCHASE_CLASS_NON_STOCK]
    non_spare_service_records = [row for row in po_records if row.get("classification") == PURCHASE_CLASS_SERVICE]
    capex_records = [row for row in po_records if row.get("classification") == PURCHASE_CLASS_CAPEX]
    consumable_records = [row for row in po_records if row.get("classification") == PURCHASE_CLASS_CONSUMABLE]
    classified_manual_review_records = [row for row in po_records if row.get("classification") == PURCHASE_CLASS_OTHER]
    manual_review_records = [row for row in po_records if row.get("needs_manual_review") or row.get("classification") == PURCHASE_CLASS_OTHER]
    external_spare_records = [row for row in po_records if _is_spare_purchase_classification(row.get("classification"))]

    internal_drawn_value = sum(float(row.get("value") or 0) for row in movement_records if row.get("value") is not None)
    external_value = round(sum(float(row.get("total_cost") or 0) for row in external_spare_records if row.get("total_cost") is not None), 2) if external_spare_records else None
    inventory_value = round(sum(float(value) for value in valid_stock_values), 2) if valid_stock_values else None
    total_consumption_value = (internal_drawn_value or 0) + (external_value or 0)
    stocked_po_value = round(sum(float(row.get("total_cost") or 0) for row in stocked_spare_records if row.get("total_cost") is not None), 2) if stocked_spare_records else None
    non_stock_po_value = round(sum(float(row.get("total_cost") or 0) for row in non_stock_spare_records if row.get("total_cost") is not None), 2) if non_stock_spare_records else None
    non_spare_po_value = round(sum(float(row.get("total_cost") or 0) for row in non_spare_service_records if row.get("total_cost") is not None), 2) if non_spare_service_records else None
    capex_po_value = round(sum(float(row.get("total_cost") or 0) for row in capex_records if row.get("total_cost") is not None), 2) if capex_records else None
    consumable_po_value = round(sum(float(row.get("total_cost") or 0) for row in consumable_records if row.get("total_cost") is not None), 2) if consumable_records else None
    dependency_pct = (
        round((float(external_value or 0) / (float(external_value or 0) + float(inventory_value))) * 100, 1)
        if inventory_value not in (None, 0) and external_value is not None
        else None
    )
    exact_code_matches = sum(1 for row in po_records if row.get("inventory_match_status") == "Exact Item Code Match")
    description_matches = sum(1 for row in po_records if row.get("inventory_match_status") == "Description Match")
    translation_failed = sum(1 for row in po_records if row.get("translation_status") == "Translation failed")

    comparison_notes = [
        "Dynamics inventory represents current in-store stock. Gen PO represents external purchase records. Gen PO includes spare parts, direct purchases, services, and other non-spare items, so PO rows are classified before comparison.",
        "External purchase comparison excludes service, CAPEX, consumable, and unclassified PO rows unless selected in filters.",
    ]
    if not exact_code_matches and po_records:
        comparison_notes.append("No inventory-code matches found; using description and keyword classification.")
    if not _PT_TRANSLATOR_OK:
        comparison_notes.append("Translator unavailable; using original description.")

    return {
        "data_sources": source_status,
        "future_file_support": {
            "accepted_files": [
                "DynamicsExport_complete_final.xlsx",
                "Gen PO D365 Rev.03.xlsx",
                "spare_parts_master.xlsx/.csv",
                "po_list.xlsx/.csv",
            ],
            "column_detection": "Flexible aliases plus code-column heuristics are normalized in backend/spare_parts_service.py",
        },
        "inventory": {
            "records": inventory_records,
            "summary": {
                "total_items": len(inventory_records),
                "total_current_quantity": round(sum(float(value) for value in current_quantities), 3) if current_quantities else None,
                "total_inventory_value": inventory_value,
                "in_stock_items": sum(1 for row in inventory_records if float(row.get("current_quantity") or 0) > 0),
                "in_stock_value": round(sum(float(row.get("stock_value") or 0) for row in inventory_records if float(row.get("current_quantity") or 0) > 0 and row.get("stock_value") is not None), 2) or None,
                "low_stock_items": sum(1 for row in inventory_records if row.get("stock_status_group") == "Low Stock"),
                "out_of_stock_items": sum(1 for row in inventory_records if row.get("stock_status_group") == "Out of Stock"),
                "overstock_items": sum(1 for row in inventory_records if row.get("stock_status_group") == "Overstock"),
                "reorder_required_items": sum(1 for row in inventory_records if row.get("stock_health_status") == "Reorder Required"),
                "total_quantity_to_buy": round(sum(float(row.get("quantity_to_buy") or 0) for row in inventory_records if row.get("stock_health_status") == "Reorder Required"), 3),
                "within_recommended_range": sum(1 for row in inventory_records if row.get("stock_status_group") == "In Stock"),
                "above_recommended_items": sum(1 for row in inventory_records if row.get("stock_status_group") == "Overstock"),
                "missing_threshold_items": sum(1 for row in inventory_records if row.get("stock_status_group") == "Unknown Stock Threshold"),
                "threshold_error_items": sum(1 for row in inventory_records if row.get("stock_health_status") == "Threshold Error"),
                "stock_health_pct": round((len(healthy_records) / len(health_valid_records)) * 100, 1) if health_valid_records else None,
                "critical_equipment_parts_below_minimum": sum(
                    1
                    for row in inventory_records
                    if row.get("stock_health_status") == "Reorder Required"
                    and _normalize_phrase(row.get("equipment_criticality")) == "critical"
                ),
            },
            "stock_status_breakdown": _count_by(inventory_records, "stock_status_group"),
            "value_by_category": _sum_by(inventory_records, "category", "stock_value"),
        },
        "consumption": {
            "store_drawn_records": movement_records,
            "external_bought_records": external_spare_records,
            "summary": {
                "internal_drawn_quantity": round(sum(float(row.get("quantity_drawn") or 0) for row in movement_records), 3) if movement_records else None,
                "internal_drawn_value": round(internal_drawn_value, 2) if movement_records else None,
                "internal_drawn_count": len(movement_records),
                "external_bought_quantity": round(sum(float(row.get("quantity_ordered") or 0) for row in external_spare_records), 3) if external_spare_records else None,
                "external_bought_value": external_value,
                "total_spare_part_consumption_value": round(total_consumption_value, 2) if total_consumption_value else None,
                "external_purchase_dependency_pct": dependency_pct,
            },
        },
        "turnover": {
            "records": turnover_records,
            "summary": {
                "average_turnover_days": round(sum(turnover_days) / len(turnover_days), 1) if turnover_days else None,
                "fast_moving_count": sum(1 for row in turnover_records if row.get("classification") == "Fast-moving"),
                "slow_moving_count": sum(1 for row in turnover_records if row.get("classification") == "Slow-moving"),
                "dormant_count": sum(1 for row in turnover_records if row.get("classification") == "Dead stock / dormant"),
            },
            "classification_breakdown": _count_by(turnover_records, "classification"),
            "average_days_by_category": [
                {"label": category, "value": round(sum(values) / len(values), 1)}
                for category, values in sorted(
                    {
                        category: [row["turnover_days"] for row in turnover_records if row.get("category") == category and row.get("turnover_days") is not None]
                        for category in {row.get("category") or "Unclassified" for row in turnover_records}
                    }.items()
                )
                if values
            ],
        },
        "equipment_usage": {
            "records": usage_rows,
            "summary": {
                "top_equipment_type_by_cost": (_sum_by(usage_rows, "equipment_type", "cost") or [{}])[0].get("label"),
                "top_equipment_by_usage": (Counter(row.get("equipment_name") or "Unmatched" for row in usage_rows).most_common(1) or [(None, 0)])[0][0],
            },
            "cost_by_equipment_type": _sum_by(usage_rows, "equipment_type", "cost"),
            "usage_share_by_equipment_type": _count_by(usage_rows, "equipment_type"),
        },
        "po_classification": {
            "records": po_records,
            "manual_review_records": manual_review_records,
            "summary": {
                "spare_part_po_value": external_value,
                "stocked_spare_part_po_value": stocked_po_value,
                "non_stock_spare_part_po_value": non_stock_po_value,
                "service_labour_repair_po_value": non_spare_po_value,
                "non_spare_part_po_value": non_spare_po_value,
                "capex_po_value": capex_po_value,
                "consumable_po_value": consumable_po_value,
                "manual_review_items": len(manual_review_records),
                "spare_part_po_count": len(external_spare_records),
                "stocked_spare_part_po_count": len(stocked_spare_records),
                "non_stock_spare_part_po_count": len(non_stock_spare_records),
                "service_labour_repair_po_count": len(non_spare_service_records),
                "non_spare_part_po_count": len(non_spare_service_records),
                "capex_po_count": len(capex_records),
                "consumable_po_count": len(consumable_records),
                "other_unclassified_po_count": len(classified_manual_review_records),
                "exact_item_code_matches": exact_code_matches,
                "description_matches": description_matches,
                "translation_failed_items": translation_failed,
            },
            "value_by_classification": _sum_by(po_records, "classification", "total_cost"),
        },
        "comparison": {
            "notes": comparison_notes,
            "inventory_purchase_summary_rows": _build_inventory_purchase_summary_rows(inventory_records, po_records),
            "top_external_spare_parts_rows": _build_top_external_spare_parts_rows(po_records),
            "summary": {
                # current_inventory_value = SUM(on_hand_qty × derived_unit_cost) for
                # in-stock stock spare items only. Uses inventory file cost first, then
                # falls back to latest / weighted-average received PO price.
                "current_inventory_value": inv_val_meta["current_inventory_value"],
                "valued_in_stock_items": inv_val_meta["valued_in_stock_items"],
                "unvalued_in_stock_items": inv_val_meta["unvalued_in_stock_items"],
                "total_in_stock_items_for_valuation": inv_val_meta["total_in_stock_items"],
                "inventory_valuation_coverage_pct": inv_val_meta["inventory_valuation_coverage_pct"],
                "inventory_value_is_estimated": inv_val_meta["inventory_value_is_estimated"],
                "current_stocked_spare_part_items": len(inventory_records),
                "current_stock_quantity": round(sum(float(value) for value in current_quantities), 3) if current_quantities else None,
                "in_stock_items": sum(1 for row in inventory_records if float(row.get("current_quantity") or 0) > 0),
                "in_stock_value": inv_val_meta["current_inventory_value"],
                "internal_drawn_value": round(internal_drawn_value, 2) if movement_records else None,
                "internal_drawn_count": len(movement_records),
                "non_stock_spare_part_po_count": len(non_stock_spare_records),
                "non_spare_part_po_count": len(non_spare_service_records),
                "external_po_spare_part_value": external_value,
                "stocked_spare_part_po_value": stocked_po_value,
                "non_stock_spare_part_po_value": non_stock_po_value,
                "non_spare_part_service_po_value": non_spare_po_value,
                "service_labour_repair_po_value": non_spare_po_value,
                "capex_po_value": capex_po_value,
                "consumable_po_value": consumable_po_value,
                "manual_review_po_items": len(classified_manual_review_records),
                "external_purchase_dependency_pct": dependency_pct,
                "inventory_value_unavailable": inv_val_meta["current_inventory_value"] is None,
                "gen_po_spare_part_quantity": round(sum(float(row.get("quantity_ordered") or 0) for row in external_spare_records), 3) if external_spare_records else None,
                "gen_po_non_spare_part_quantity": round(sum(float(row.get("quantity_ordered") or 0) for row in non_spare_service_records), 3) if non_spare_service_records else None,
                "gen_po_spare_part_line_count": len(external_spare_records),
                "gen_po_non_spare_part_line_count": len(non_spare_service_records),
                "translation_failed_items": translation_failed,
                "exact_item_code_matches": exact_code_matches,
                "description_matches": description_matches,
            },
        },
        "structured_errors": errors,
    }


def _build_structured_spare_parts_payload(paths, source_status):
    errors = []
    try:
        inventory_records = _build_inventory_records(paths.get("spare_parts_master"), source_status.get("spare_parts_master", {}))
    except Exception as exc:
        errors.append(f"Current Inventory: {exc}")
        inventory_records = []
    inventory_lookup = {row.get("code"): row for row in inventory_records if row.get("code")}
    master_codes = set(inventory_lookup)

    try:
        movement_records = _build_movement_records(paths.get("inventory_movement"), source_status.get("inventory_movement", {}))
    except Exception as exc:
        errors.append(f"Inventory Movement: {exc}")
        movement_records = []
    try:
        equipment_records = _build_equipment_master_records(paths.get("equipment_master"))
    except Exception as exc:
        errors.append(f"Equipment Master: {exc}")
        equipment_records = []
    inventory_records = _enrich_inventory_with_equipment(inventory_records, equipment_records)
    try:
        work_order_records = _build_work_order_records(paths.get("work_orders"))
    except Exception as exc:
        errors.append(f"Work Order Data: {exc}")
        work_order_records = []
    try:
        stage_po_rows, stage_po_status = _load_stage_goods_received_rows()
        if stage_po_rows:
            po_records = _build_po_records_from_stage_rows(stage_po_rows, inventory_records, stage_po_status)
        else:
            po_records = _build_po_records(paths.get("po_list"), source_status.get("po_list", {}), master_codes)
    except Exception as exc:
        errors.append(f"Gen PO: {exc}")
        po_records = []
    po_records = _refine_po_records_with_inventory(po_records, inventory_records)

    return _run_spare_aggregation(
        inventory_records, po_records, movement_records,
        work_order_records=work_order_records,
        equipment_records=equipment_records,
        source_status=source_status,
        errors=errors,
    )


# ── Phase 5: SQL helpers for spare parts ──────────────────────────────────────

def _sql_has_spare_parts() -> bool:
    try:
        import db as _db
        with _db.get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM spare_parts").fetchone()[0]
        return count > 0
    except Exception:
        return False


def _build_spare_sql_asset_lookup():
    lookup = {}
    try:
        import db as _db
        for row in _db.query_asset_master():
            asset_id = clean_text(row.get("asset_id"))
            if not asset_id:
                continue
            lookup[asset_id.upper()] = {
                "asset_name": clean_text(row.get("asset_name")),
                "stage": clean_text(row.get("stage")),
                "category": clean_text(row.get("category")),
                "machine_group": clean_text(row.get("machine_group")),
            }
    except Exception:
        return {}
    return lookup


def _flatten_inventory_to_sql_rows(records, asset_lookup=None):
    asset_lookup = asset_lookup or {}
    rows = []
    for r in records:
        asset_id = clean_text(r.get("equipment_asset_id"))
        asset_meta = asset_lookup.get((asset_id or "").upper(), {})
        extra = {
            "equipment_criticality": r.get("equipment_criticality"),
            "equipment_type": r.get("equipment_type"),
            "translation_status": r.get("translation_status"),
            "translated_name": r.get("translated_name"),
            "search_name": r.get("search_name"),
            "available_physical": r.get("available_physical"),
            "reserved_physical": r.get("reserved_physical"),
            "on_order": r.get("on_order"),
            "inventory_value": r.get("inventory_value"),
            "data_quality_flags": r.get("data_quality_flags"),
            "has_negative_stock_quantity": r.get("has_negative_stock_quantity"),
            "missing_part_number": r.get("missing_part_number"),
            "missing_spare_part_name": r.get("missing_spare_part_name"),
            "machine_group": asset_meta.get("machine_group"),
        }
        rows.append({
            "item_number": r.get("code"),
            "item_name": r.get("name") or r.get("translated_name"),
            "asset_id": asset_id,
            "asset_name": r.get("equipment_name") or asset_meta.get("asset_name"),
            "stage": asset_meta.get("stage"),
            "category": r.get("category") or r.get("item_group") or asset_meta.get("category"),
            "transaction_type": "inventory",
            "quantity": r.get("current_quantity"),
            "unit_price": r.get("unit_cost"),
            "total_value": r.get("stock_value"),
            "supplier": None,
            "po_number": None,
            "pr_number": None,
            "transaction_date": r.get("last_updated"),
            "source_file": r.get("source_file"),
            "classification": None,
            "min_stock": r.get("min_stock"),
            "max_stock": r.get("max_stock"),
            "unit": r.get("unit"),
            "location": r.get("location"),
            "needs_review": bool(r.get("has_negative_stock_quantity") or r.get("missing_part_number")),
            "extra_json": json.dumps({k: v for k, v in extra.items() if v is not None}, default=str),
        })
    return rows


def _flatten_po_to_sql_rows(records, transaction_type="gen_po", asset_lookup=None):
    asset_lookup = asset_lookup or {}
    rows = []
    for r in records:
        asset_id = clean_text(r.get("asset_id"))
        asset_meta = asset_lookup.get((asset_id or "").upper(), {})
        source_stage = clean_text(r.get("source_stage"))
        extra = {
            "work_order_id": r.get("work_order_id"),
            "original_description": r.get("original_description") or r.get("description"),
            "translated_description": r.get("translated_description") or r.get("description"),
            "group_of_cost": r.get("group_of_cost"),
            "translated_group_of_cost": r.get("translated_group_of_cost"),
            "type_of_cost": r.get("type_of_cost"),
            "sub_cost": r.get("sub_cost"),
            "pd_machine": r.get("pd_machine"),
            "translated_pd_machine": r.get("translated_pd_machine"),
            "grn_no": r.get("grn_no"),
            "grn_status": r.get("grn_status"),
            "kpi_status": r.get("kpi_status"),
            "delivery_flag": r.get("delivery_flag"),
            "lead_time_days": r.get("lead_time_days"),
            "actual_lead_days": r.get("actual_lead_days"),
            "delay_days": r.get("delay_days"),
            "used_po_date_as_gr_fallback": r.get("used_po_date_as_gr_fallback"),
            "goods_received_date": r.get("goods_received_date"),
            "grn_date": r.get("grn_date"),
            "actual_end_date": r.get("actual_end_date"),
            "department": r.get("department"),
            "gl_account": r.get("gl_account"),
            "asset_name": r.get("asset_name") or r.get("equipment_name"),
            "translation_status": r.get("translation_status"),
            "confidence": r.get("confidence"),
            "classification_reason": r.get("classification_reason"),
            "inventory_match_status": r.get("inventory_match_status"),
            "inventory_match_confidence": r.get("inventory_match_confidence"),
            "inventory_match_reason": r.get("inventory_match_reason"),
            "inventory_match_code": r.get("inventory_match_code"),
            "inventory_match_name": r.get("inventory_match_name"),
            "completed": r.get("completed"),
            "review_reasons": r.get("review_reasons"),
            "quantity_received": r.get("quantity_received"),
            "machine_group": asset_meta.get("machine_group"),
        }
        rows.append({
            "item_number": r.get("code"),
            "item_name": r.get("translated_description") or r.get("description"),
            "asset_id": asset_id,
            "asset_name": r.get("asset_name") or r.get("equipment_name") or asset_meta.get("asset_name"),
            "stage": source_stage or asset_meta.get("stage"),
            "category": r.get("group_of_cost") or r.get("translated_group_of_cost") or asset_meta.get("category"),
            "transaction_type": transaction_type,
            "quantity": r.get("quantity_ordered"),
            "unit_price": r.get("unit_cost"),
            "total_value": r.get("total_cost"),
            "supplier": r.get("supplier") or r.get("vendor_name"),
            "po_number": r.get("po_number"),
            "pr_number": r.get("work_order_id"),
            "transaction_date": r.get("po_date"),
            "source_file": r.get("source_file"),
            "classification": r.get("classification"),
            "min_stock": None,
            "max_stock": None,
            "unit": r.get("unit"),
            "location": None,
            "needs_review": bool(r.get("needs_manual_review")),
            "extra_json": json.dumps({k: v for k, v in extra.items() if v is not None}, default=str),
        })
    return rows


def _flatten_movement_to_sql_rows(records, asset_lookup=None):
    asset_lookup = asset_lookup or {}
    rows = []
    for r in records:
        asset_id = clean_text(r.get("asset_id"))
        asset_meta = asset_lookup.get((asset_id or "").upper(), {})
        extra = {
            "value": r.get("value"),
            "document_number": r.get("document_number"),
            "movement_type": r.get("movement_type"),
            "work_order_id": r.get("work_order_id"),
            "machine_group": r.get("machine_group"),
            "general_area": r.get("general_area"),
        }
        rows.append({
            "item_number": r.get("code"),
            "item_name": r.get("name"),
            "asset_id": asset_id,
            "asset_name": r.get("equipment_name") or asset_meta.get("asset_name"),
            "stage": asset_meta.get("stage"),
            "category": r.get("category") or asset_meta.get("category"),
            "transaction_type": "movement",
            "quantity": r.get("quantity_drawn"),
            "unit_price": r.get("unit_cost"),
            "total_value": r.get("value"),
            "supplier": None,
            "po_number": None,
            "pr_number": None,
            "transaction_date": r.get("date"),
            "source_file": r.get("source_file"),
            "classification": None,
            "min_stock": None,
            "max_stock": None,
            "unit": None,
            "location": None,
            "needs_review": False,
            "extra_json": json.dumps({k: v for k, v in extra.items() if v is not None}, default=str),
        })
    return rows


def _flatten_project_transactions_to_sql_rows(records, asset_lookup=None, source_file="project_transactions_sql_sync"):
    asset_lookup = asset_lookup or {}
    rows = []
    for r in records:
        asset_id = clean_text(r.get("asset_id"))
        asset_meta = asset_lookup.get((asset_id or "").upper(), {})
        description = (
            clean_text(r.get("translated_description"))
            or clean_text(r.get("original_description"))
            or clean_text(r.get("clean_description"))
            or "Unknown"
        )
        extra = {
            "transaction_id": r.get("transaction_id"),
            "original_description": r.get("original_description"),
            "translated_description": r.get("translated_description"),
            "clean_description": r.get("clean_description"),
            "item_category": r.get("item_category"),
            "quantity_used": r.get("quantity_used"),
            "total_consumption": r.get("total_consumption"),
            "unit_cost_estimate": r.get("unit_cost_estimate"),
            "link_status": r.get("link_status"),
            "equipment_name": r.get("equipment_name"),
            "year": r.get("year"),
            "month": r.get("month"),
            "fy": r.get("fy"),
            "machine_group": asset_meta.get("machine_group"),
        }
        rows.append({
            "item_number": clean_text(r.get("transaction_id")) or None,
            "item_name": description,
            "asset_id": asset_id,
            "asset_name": clean_text(r.get("equipment_name")) or asset_meta.get("asset_name"),
            "stage": asset_meta.get("stage"),
            "category": clean_text(r.get("item_category")) or asset_meta.get("category"),
            "transaction_type": "project_txn",
            "quantity": r.get("quantity_used"),
            "unit_price": r.get("unit_cost_estimate"),
            "total_value": r.get("total_consumption"),
            "supplier": None,
            "po_number": None,
            "pr_number": clean_text(r.get("work_order_id")) or None,
            "transaction_date": r.get("project_date"),
            "source_file": source_file,
            "classification": clean_text(r.get("link_status")) or None,
            "min_stock": None,
            "max_stock": None,
            "unit": None,
            "location": None,
            "needs_review": clean_text(r.get("link_status")) not in {"", "Linked"},
            "extra_json": json.dumps({k: v for k, v in extra.items() if v is not None}, default=str),
        })
    return rows


def _sql_to_inventory_record(row):
    try:
        extra = json.loads(row.get("extra_json") or "{}") or {}
    except Exception:
        extra = {}
    record = {
        "code": row.get("item_number"),
        "name": row.get("item_name") or row.get("item_number") or "Unmatched",
        "description": row.get("item_name") or row.get("item_number") or "Unmatched",
        "search_name": row.get("item_name"),
        "translated_name": row.get("item_name") or row.get("item_number") or "Unmatched",
        "translation_status": "No translation needed",
        "missing_part_number": not bool(row.get("item_number")),
        "missing_spare_part_name": not bool(row.get("item_name")),
        "has_negative_stock_quantity": row.get("quantity") is not None and float(row.get("quantity") or 0) < 0,
        "category": row.get("category") or "Unclassified",
        "item_group": row.get("category") or "Unclassified",
        "available_physical": row.get("quantity"),
        "reserved_physical": None,
        "total_available": row.get("quantity"),
        "on_order": None,
        "current_quantity": row.get("quantity"),
        "unit": row.get("unit"),
        "min_stock": row.get("min_stock"),
        "max_stock": row.get("max_stock"),
        "unit_cost": row.get("unit_price"),
        "inventory_value": row.get("total_value"),
        "stock_value": row.get("total_value"),
        "location": row.get("location") or "Unmatched",
        "equipment_name": row.get("asset_name"),
        "equipment_asset_id": row.get("asset_id"),
        "equipment_criticality": "Unclassified",
        "equipment_type": "Unclassified",
        "last_updated": row.get("transaction_date"),
        "source_file": row.get("source_file"),
    }
    record.update(extra)
    return _finalize_inventory_health(record)


def _sql_to_po_record(row):
    try:
        extra = json.loads(row.get("extra_json") or "{}") or {}
    except Exception:
        extra = {}
    classification = row.get("classification") or PURCHASE_CLASS_OTHER
    completed = bool(row.get("po_number") and row.get("transaction_date"))
    base = {
        "po_date": row.get("transaction_date"),
        "goods_received_date": None,
        "po_number": row.get("po_number"),
        "code": row.get("item_number"),
        "description": row.get("item_name") or row.get("item_number") or "Unmatched",
        "original_description": row.get("item_name") or row.get("item_number") or "Unmatched",
        "translated_description": row.get("item_name") or row.get("item_number") or "Unmatched",
        "clean_description": _normalize_part_name(row.get("item_name") or row.get("item_number") or ""),
        "quantity_ordered": row.get("quantity"),
        "quantity_received": row.get("quantity"),
        "unit": row.get("unit"),
        "unit_cost": row.get("unit_price"),
        "total_cost": row.get("total_value"),
        "supplier": row.get("supplier") or "Unmatched",
        "vendor_name": row.get("supplier") or "Unmatched",
        "work_order_id": row.get("pr_number"),
        "asset_id": row.get("asset_id"),
        "group_of_cost": row.get("category"),
        "translated_group_of_cost": row.get("category"),
        "type_of_cost": None,
        "sub_cost": None,
        "pd_machine": row.get("asset_id"),
        "translated_pd_machine": row.get("asset_id"),
        "classification": classification,
        "purchase_classification": classification,
        "confidence": "High" if classification != PURCHASE_CLASS_OTHER else "Low",
        "classification_reason": "Stored classification from import",
        "translation_status": "No translation needed",
        "inventory_match_status": "No Inventory Match",
        "inventory_match_confidence": "Low",
        "inventory_match_reason": "No inventory item code or strong description match was found",
        "inventory_match_code": None,
        "inventory_match_name": None,
        "department": None,
        "gl_account": None,
        "grn_no": None,
        "grn_status": None,
        "kpi_status": "",
        "source_stage": row.get("stage") or "Unmapped Stage",
        "completed": completed,
        "delivery_flag": "Delivered" if completed else "Pending",
        "lead_time_days": None,
        "actual_lead_days": None,
        "used_po_date_as_gr_fallback": False,
        "source_file": row.get("source_file"),
        "review_reasons": [],
        "needs_manual_review": bool(row.get("needs_review")),
    }
    base.update(extra)
    return base


def _sql_to_movement_record(row):
    try:
        extra = json.loads(row.get("extra_json") or "{}") or {}
    except Exception:
        extra = {}
    base = {
        "date": row.get("transaction_date"),
        "code": row.get("item_number"),
        "name": row.get("item_name") or row.get("item_number") or "Unmatched",
        "category": row.get("category") or "Unclassified",
        "quantity_drawn": row.get("quantity"),
        "quantity_received": None,
        "unit_cost": row.get("unit_price"),
        "value": row.get("total_value"),
        "work_order_id": None,
        "asset_id": row.get("asset_id"),
        "issued_by": None,
        "transaction_type": "Issue",
        "source_file": row.get("source_file"),
    }
    base.update(extra)
    return base


def _build_structured_spare_parts_payload_from_sql(source_status):
    """Build the structured payload using pre-classified SQL records instead of Excel."""
    try:
        import db as _db
        inv_rows = _db.load_spare_parts_from_sql(transaction_type="inventory")
        po_rows = (
            _db.load_spare_parts_from_sql(transaction_type="gen_po")
            + _db.load_spare_parts_from_sql(transaction_type="stage_po")
        )
        mov_rows = _db.load_spare_parts_from_sql(transaction_type="movement")
    except Exception as exc:
        print(f"[db] spare parts SQL load failed: {exc}")
        return None

    if not inv_rows and not po_rows:
        return None

    inventory_records = _aggregate_inventory_records([_sql_to_inventory_record(r) for r in inv_rows])
    po_records = _refine_po_records_with_inventory(
        [_sql_to_po_record(r) for r in po_rows],
        inventory_records,
    )
    movement_records = [_sql_to_movement_record(r) for r in mov_rows]

    return _run_spare_aggregation(
        inventory_records, po_records, movement_records,
        source_status=source_status,
    )


def write_spare_parts_to_db() -> dict:
    """Build spare parts records from Excel and sync to SQL."""
    try:
        import db as _db
    except Exception as exc:
        return {"ok": False, "rows": 0, "message": f"db module unavailable: {exc}"}
    try:
        data_dir_path = Path(__file__).resolve().parent.parent / "data"
        paths, source_status = _resolve_future_sources(data_dir_path)
        asset_lookup = _build_spare_sql_asset_lookup()

        inventory_records = []
        try:
            inventory_records = _build_inventory_records(
                paths.get("spare_parts_master"), source_status.get("spare_parts_master", {})
            )
        except Exception as exc:
            print(f"[db] spare parts inventory build error: {exc}")

        movement_records = []
        try:
            movement_records = _build_movement_records(
                paths.get("inventory_movement"), source_status.get("inventory_movement", {})
            )
        except Exception as exc:
            print(f"[db] spare parts movement build error: {exc}")

        master_codes = {r["code"] for r in inventory_records if r.get("code")}
        po_records = []
        po_ttype = "gen_po"
        try:
            stage_po_rows, stage_po_status = _load_stage_goods_received_rows()
            if stage_po_rows:
                po_records = _build_po_records_from_stage_rows(stage_po_rows, inventory_records, stage_po_status)
                po_ttype = "stage_po"
            else:
                po_records = _build_po_records(paths.get("po_list"), source_status.get("po_list", {}), master_codes)
            po_records = _refine_po_records_with_inventory(po_records, inventory_records)
        except Exception as exc:
            print(f"[db] spare parts PO build error: {exc}")

        project_transaction_records = []
        try:
            project_payload = build_all_years_transactions_payload()
            if project_payload.get("status") == "ok":
                project_transaction_records = list(project_payload.get("transactions") or [])
            else:
                print(f"[db] spare parts project transactions unavailable: {project_payload.get('error')}")
        except Exception as exc:
            print(f"[db] spare parts project transactions build error: {exc}")

        sql_rows = (
            _flatten_inventory_to_sql_rows(inventory_records, asset_lookup=asset_lookup)
            + _flatten_po_to_sql_rows(po_records, po_ttype, asset_lookup=asset_lookup)
            + _flatten_movement_to_sql_rows(movement_records, asset_lookup=asset_lookup)
            + _flatten_project_transactions_to_sql_rows(
                project_transaction_records,
                asset_lookup=asset_lookup,
                source_file="project_transactions_history",
            )
        )

        result = _db.upsert_spare_parts(sql_rows)
        total_rows = result.get("rows", 0)
        file_names = ", ".join(p.name for p in paths.values() if p)
        _db.log_import(
            source_type="spare_parts",
            source_file=file_names or "unknown",
            row_count=total_rows,
            valid_count=total_rows,
            invalid_count=0,
            notes=f"types: {result.get('types', [])}",
        )
        return {
            "ok": True,
            "rows": total_rows,
            "message": (
                f"Synced {total_rows} spare parts row(s) to SQL "
                f"({len(inventory_records)} inventory, {len(po_records)} PO, {len(movement_records)} movement, "
                f"{len(project_transaction_records)} project transactions)."
            ),
        }
    except Exception as exc:
        return {"ok": False, "rows": 0, "message": f"Spare parts SQL sync failed: {exc}"}


def _write_spare_to_db_background() -> None:
    global _SPARE_DB_SYNC_RUNNING, _SPARE_DB_SYNC_PENDING
    while True:
        try:
            result = write_spare_parts_to_db()
            print(f"[db] spare parts: {result.get('message', result)}")
        except Exception as exc:
            print(f"[db] spare parts SQL sync error: {exc}")
        with _SPARE_DB_SYNC_GUARD:
            if _SPARE_DB_SYNC_PENDING:
                _SPARE_DB_SYNC_PENDING = False
                continue
            _SPARE_DB_SYNC_RUNNING = False
            break


def request_spare_db_sync() -> bool:
    """Queue a single background SQL sync; collapse bursts of repeated requests."""
    global _SPARE_DB_SYNC_RUNNING, _SPARE_DB_SYNC_PENDING
    with _SPARE_DB_SYNC_GUARD:
        if _SPARE_DB_SYNC_RUNNING:
            _SPARE_DB_SYNC_PENDING = True
            return False
        _SPARE_DB_SYNC_RUNNING = True
        _SPARE_DB_SYNC_PENDING = False
    threading.Thread(
        target=_write_spare_to_db_background,
        name="db-spare-sync",
        daemon=True,
    ).start()
    return True


def _build_full_payload_from_structured(structured_payload, source_paths):
    """Wrap a structured spare parts payload in the full API response envelope."""
    inventory_records = structured_payload.get("inventory", {}).get("records", [])
    po_records = structured_payload.get("po_classification", {}).get("records", [])
    external_spare_records = [row for row in po_records if _is_spare_purchase_classification(row.get("classification"))]
    source_errors = structured_payload.get("structured_errors", [])
    inventory_value = structured_payload.get("inventory", {}).get("summary", {}).get("total_inventory_value")
    external_value = structured_payload.get("po_classification", {}).get("summary", {}).get("spare_part_po_value")

    legacy_records = [
        {
            "source_type": "Inventory",
            "item_description": row.get("name"),
            "normalized_part_name": _normalize_part_name(row.get("name")),
            "quantity": row.get("current_quantity"),
            "unit": row.get("unit"),
            "date": row.get("last_updated"),
            "supplier_vendor": None,
            "cost_value": row.get("stock_value"),
            "raw_source_reference": row.get("code"),
            "raw_description": row.get("name"),
            "urgency_type": "Planned",
            "linked_equipment_name": None,
            "linked_asset_id": None,
            "unlinked_flag": False,
        }
        for row in inventory_records
    ] + [
        {
            "source_type": "External Purchase",
            "item_description": row.get("translated_description") or row.get("description"),
            "normalized_part_name": _normalize_part_name(row.get("translated_description") or row.get("description")),
            "quantity": row.get("quantity_ordered"),
            "unit": row.get("unit"),
            "date": row.get("po_date"),
            "supplier_vendor": row.get("supplier"),
            "cost_value": row.get("total_cost"),
            "raw_source_reference": row.get("po_number"),
            "raw_description": row.get("original_description") or row.get("description"),
            "urgency_type": "Urgent",
            "linked_equipment_name": None,
            "linked_asset_id": row.get("asset_id"),
            "unlinked_flag": not bool(row.get("asset_id") or row.get("work_order_id")),
        }
        for row in external_spare_records
    ]

    trend = _build_trend(legacy_records)
    planned_count = len(inventory_records)
    urgent_count = len(external_spare_records)
    classified_total = planned_count + urgent_count

    last_synced = None
    if source_paths:
        timestamps = []
        for p in source_paths:
            try:
                timestamps.append(datetime.fromtimestamp(p.stat().st_mtime).isoformat())
            except OSError:
                pass
        last_synced = max(timestamps, default=None)

    return {
        "meta": {
            "last_synced": last_synced,
            "source_paths": [str(p) for p in source_paths],
            "errors": source_errors,
        },
        "summary": {
            "total_records": len(legacy_records),
            "planned_count": planned_count,
            "urgent_count": urgent_count,
            "inventory_count": len(inventory_records),
            "external_count": len(po_records),
            "linked_equipment_count": 0,
            "unlinked_count": sum(1 for row in legacy_records if row.get("unlinked_flag")),
            "top_equipment_name": None,
            "top_equipment_usage_count": 0,
            "total_external_purchase_value": external_value or 0,
            "total_inventory_value": inventory_value,
        },
        "planned_vs_urgent": {
            "planned_count": planned_count,
            "urgent_count": urgent_count,
            "planned_pct": round((planned_count / classified_total) * 100, 1) if classified_total else 0,
            "urgent_pct": round((urgent_count / classified_total) * 100, 1) if classified_total else 0,
            "trend": trend,
        },
        "source_split": {
            "inventory": {
                "count": len(inventory_records),
                "part_count": len(inventory_records),
                "quantity": structured_payload.get("inventory", {}).get("summary", {}).get("total_current_quantity"),
                "value": inventory_value,
            },
            "external_purchase": {
                "count": len(po_records),
                "part_count": len(external_spare_records),
                "quantity": structured_payload.get("consumption", {}).get("summary", {}).get("external_bought_quantity"),
                "value": external_value,
            },
        },
        "equipment_rows": [],
        "top_external_parts": [],
        "top_urgent_parts": [],
        "records": legacy_records,
        "unlinked_rows": [],
        "filter_options": _build_filter_options(legacy_records),
        "matching_rules": {
            "file_support": "Drop spare_parts_master, Item_list_for_keep_spare_part_TRANSLATED, inventory_movement, po_list, work_orders, and equipment_master files into the data folder as .xlsx or .csv.",
            "column_detection": "Adjust FLEXIBLE_COLUMN_ALIASES in backend/spare_parts_service.py for new source column names.",
            "po_classification": "Adjust _classify_po_item() keyword logic in backend/spare_parts_service.py.",
        },
        **structured_payload,
    }


def build_spare_parts_payload():
    data_dir_path = Path(__file__).resolve().parent.parent / "data"
    future_paths, future_source_status = _resolve_future_sources(data_dir_path)
    source_paths = [p for p in future_paths.values() if p]
    cache_signature = (
        _file_signature(D365_SPARE_PARTS_PATH),
        _file_signature(GEN_PO_SPARE_PARTS_PATH),
        _stage_po_source_signatures(),
        *(_file_signature(path) for path in future_paths.values()),
    )

    cached = _SPARE_PARTS_CACHE.get(cache_signature)
    if cached:
        return cached
    persistent_cached = _read_persistent_payload_cache("spare_parts_payload", cache_signature)
    if persistent_cached is not None:
        _SPARE_PARTS_CACHE.clear()
        _SPARE_PARTS_CACHE[cache_signature] = persistent_cached
        return persistent_cached

    # SQL path — fast cold-start when disk cache is empty but SQL has records.
    if _sql_has_spare_parts():
        try:
            sql_struct = _build_structured_spare_parts_payload_from_sql(future_source_status)
            if sql_struct is not None:
                payload = _build_full_payload_from_structured(sql_struct, source_paths)
                _SPARE_PARTS_CACHE.clear()
                _SPARE_PARTS_CACHE[cache_signature] = payload
                _write_persistent_payload_cache("spare_parts_payload", cache_signature, payload)
                return payload
        except Exception as exc:
            print(f"[db] spare parts SQL path failed, falling back to Excel: {exc}")

    # Excel path (cold build — may be slow for large files).
    structured_payload = _build_structured_spare_parts_payload(future_paths, future_source_status)
    payload = _build_full_payload_from_structured(structured_payload, source_paths)
    _SPARE_PARTS_CACHE.clear()
    _SPARE_PARTS_CACHE[cache_signature] = payload
    _write_persistent_payload_cache("spare_parts_payload", cache_signature, payload)
    request_spare_db_sync()
    return payload



# ── Project Actual Transactions Parser ──────────────────────────────────────

PROJECT_TRANSACTIONS_PATH = Path(
    os.environ.get(
        "PROJECT_TRANSACTIONS_PATH",
        str(DEFAULT_DATA_DIR / "Project actual transactions 2026.xlsx"),
    )
)

CSV_ALL_YEARS_PATH = Path(
    os.environ.get(
        "CSV_ALL_YEARS_PATH",
        str(DEFAULT_DATA_DIR / "Project actual transactions.csv"),
    )
)
PROJECT_TRANSACTIONS_CURRENT_PATH = DEFAULT_DATA_DIR / "project_transactions_current.xlsx"
PROJECT_TRANSACTIONS_IMPORT_DIR = DEFAULT_DATA_DIR / "project_transactions_imports"
SPARE_IMPORT_EXTENSIONS = {".csv", ".xlsx", ".xls"}
SPARE_IMPORT_CANONICAL_FILES = {
    "inventory": DEFAULT_DATA_DIR / "spare_parts_master.xlsx",
    "external_po": DEFAULT_DATA_DIR / "po_list.xlsx",
    "project_transactions": PROJECT_TRANSACTIONS_CURRENT_PATH,
}

_AY_CACHE: dict = {"result": None, "mtime": None}
_PT_CACHE: dict = {"result": None, "mtime": None}
_ASSET_LIST_CACHE: dict = {"result": None, "sig": None}
_PT_ASSET_CATALOG_CACHE: dict = {"result": None, "sig": None}

_THAI_RE = re.compile(r"[฀-๿]")
_ASSET_ID_RE = re.compile(r"[A-Z]{2,}[A-Z0-9]*-\d+")
_PT_PART_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9-]{2,}\b")

_PT_GENERAL_AREA_TOKENS = {
    "production",
    "low",
    "high",
    "risk",
    "area",
    "kitchen",
    "facility",
    "general",
    "utilities",
    "utility",
    "packaging",
    "packing",
    "process",
    "line",
    "warehouse",
    "store",
    "uncategorised",
    "uncategorized",
    "unknown",
    "review",
    "support",
    "work",
    "shop",
}


def _load_asset_list_lookup() -> dict[str, dict]:
    """Return {asset_id: {name, criticality, location}} from Asset_Master.xlsx via asset_mapping."""
    try:
        from asset_mapping import load_asset_mapping
        mapping = load_asset_mapping(str(DEFAULT_DATA_DIR))
        asset_map = mapping.get("asset_map", {})
        lookup = {
            asset_id: {
                "name": entry.get("display_name", asset_id),
                "criticality": entry.get("raw_criticality", ""),
                "location": entry.get("location", ""),
            }
            for asset_id, entry in asset_map.items()
        }
        return lookup
    except Exception:
        return {}

try:
    from downtime_service import translate_maintenance_description as _translate_desc  # type: ignore
    _PT_TRANSLATOR_OK = True
except Exception:
    _PT_TRANSLATOR_OK = False

    def _translate_desc(text: str) -> str:  # type: ignore
        return text


_PT_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Refrigerant / Chemical", ["refrigerant", "r507", "r22", "r404", "oil", "chemical", "น้ำยา", "เกลือ", "lubrication", "lubricant", "grease"]),
    ("Filter", ["filter", "cartridge", "melt blown", "micron", "strainer"]),
    ("Electrical", ["cable", "relay", "switch", "sensor", "lamp", "light", "philips", "led", "electrical", "circuit", "fuse", "contactor", "inverter", "transformer", "capacitor", "timer"]),
    ("Mechanical", ["bearing", "belt", "chain", "gear", "pulley", "roller", "motor", "fan", "blade", "gasket", "seal", "shaft", "bushing", "coupling", "spring", "piston", "o-ring", "oring"]),
    ("Piping / Plumbing", ["pvc", "brass", "valve", "hose", "pipe", "ข้อต่อ", "เทปพัน", "fitting", "flange", "nipple", "elbow", "tee", "housing", "connector", "adapter"]),
    ("Consumable", ["cable tie", "tape", "cleaning", "zip tie", "sealant", "adhesive", "sandpaper", "brush", "cloth"]),
]


def _pt_classify(desc: str) -> tuple[str, str, str]:
    lower = (desc or "").lower()
    for cat, kws in _PT_CATEGORIES:
        matched = [kw for kw in kws if kw in lower]
        if matched:
            return cat, ("High" if len(matched) > 1 else "Medium"), f"Matched: {', '.join(matched[:3])}"
    return "Other / Manual Review", "Low", "No category keywords matched"


def _pt_parse_excel_date(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if s in ("nan", "None", ""):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    try:
        serial = int(float(s))
        ts = pd.Timestamp("1899-12-30") + pd.Timedelta(days=serial)
        return ts.strftime("%Y-%m-%d")
    except Exception:
        pass
    return None


def _pt_parse_name(name: str) -> tuple[str, str]:
    if not name or str(name).strip() in ("nan", "None"):
        return "", ""
    name = str(name).strip()
    sep = ": " if ": " in name else (":" if ":" in name else None)
    if sep:
        parts = name.split(sep, 1)
        return parts[0].strip(), parts[1].strip()
    return name, ""


def _pt_parse_desc(raw: str) -> tuple[float | None, str]:
    raw = str(raw or "").strip()
    if " / " in raw:
        idx = raw.index(" / ")
        try:
            qty = float(raw[:idx].strip())
            return qty, raw[idx + 3:].strip()
        except ValueError:
            pass
    return None, raw


def _pt_clean_desc(desc: str) -> str:
    cleaned = _THAI_RE.sub("", desc or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or desc or ""


_WO_RECORDS_CACHE: dict = {"result": None, "sig": None}


def _pt_load_wo_records() -> list[dict]:
    imports_dir = DEFAULT_DATA_DIR / "work_order_imports"
    paths: list[Path] = []
    if imports_dir.exists():
        paths += sorted(imports_dir.glob("*.xlsx")) + sorted(imports_dir.glob("*.csv"))
    paths += sorted(DEFAULT_DATA_DIR.glob("work_orders_*.csv"))

    sig = tuple(_file_signature(p) for p in paths)
    if _WO_RECORDS_CACHE["result"] is not None and _WO_RECORDS_CACHE["sig"] == sig:
        return _WO_RECORDS_CACHE["result"]

    records: list[dict] = []
    for p in paths:
        try:
            df = pd.read_excel(p, dtype=str) if p.suffix == ".xlsx" else pd.read_csv(p, dtype=str)
            records.extend(df.fillna("").to_dict("records"))
        except Exception:
            pass
    _WO_RECORDS_CACHE["result"] = records
    _WO_RECORDS_CACHE["sig"] = sig
    return records


_WO_ID_KEYS = ["work_order_id", "WorkOrderId", "wo_id", "WO ID", "maintenance_order_id", "WorkOrder"]
_ASSET_ID_KEYS = ["asset_id", "AssetId", "machine_code", "Asset ID", "AssetID", "PD Machine"]


def _pt_build_lookups(wo_records: list[dict]) -> tuple[dict, dict]:
    wo_lkp: dict[str, dict] = {}
    asset_lkp: dict[str, dict] = {}
    for rec in wo_records:
        for k in _WO_ID_KEYS:
            v = str(rec.get(k) or "").strip()
            if v and v != "nan":
                wo_lkp.setdefault(v, rec)
                break
        for k in _ASSET_ID_KEYS:
            v = str(rec.get(k) or "").strip()
            if v and v != "nan":
                asset_lkp.setdefault(v, rec)
                break
    return wo_lkp, asset_lkp


def _pt_pick(rec: dict, *keys: str) -> str:
    for k in keys:
        v = str(rec.get(k) or rec.get(k.lower()) or rec.get(k.upper()) or "").strip()
        if v and v != "nan":
            return v
    return ""


def _pt_extract_wo_fields(rec: dict) -> dict:
    return {
        "equipment_name": _pt_pick(rec, "machine_name", "MachineName", "equipment_name", "Name"),
        "equipment_type": _pt_pick(rec, "machine_group", "MachineGroup", "equipment_type", "EquipmentType"),
        "equipment_criticality": _pt_pick(rec, "criticality", "Criticality", "normalized_criticality"),
        "maintenance_type": _pt_pick(rec, "job_trade", "JobTrade", "maintenance_type"),
        "wo_actual_start": _pt_pick(rec, "actual_start_time", "ActualStart", "maintenance_start_time"),
        "wo_actual_end": _pt_pick(rec, "actual_end_time", "ActualEnd", "maintenance_end_time"),
        "wo_severity": _pt_pick(rec, "service_level", "ServiceLevel", "priority"),
        "wo_request_id": _pt_pick(rec, "maintenance_order_id", "Request ID", "RequestId", "request_id"),
        "wo_description": _pt_pick(rec, "description_original", "Description", "description", "Notes", "Remarks"),
        "wo_translated_description": _pt_pick(rec, "translated_description", "TranslatedDescription"),
        "wo_location": _pt_pick(rec, "raw_functional_location", "raw_location", "Location", "location", "Area"),
    }


def normalize_spare_part_text(text: str) -> str:
    return normalize_asset_text(text or "")


def _pt_slug(text: str) -> str:
    return normalize_spare_part_text(text).replace(" ", "_") or "unknown"


def _pt_title_token(token: str) -> str:
    if not token:
        return ""
    if token.isdigit():
        return token
    if token.isalpha() and len(token) <= 3:
        return token.upper()
    return token.capitalize()


def _pt_family_name_from_asset_name(name: str) -> str:
    tokens = [tok for tok in normalize_spare_part_text(name).split() if tok]
    while tokens and tokens[-1].isdigit():
        tokens = tokens[:-1]
    if not tokens:
        return clean_text(name) or "Unknown Family"
    return " ".join(_pt_title_token(token) for token in tokens)


def _pt_is_specific_asset_id(value: str) -> bool:
    return bool(_ASSET_ID_RE.search(str(value or "").upper()))


def _pt_is_general_area_value(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if _pt_is_specific_asset_id(text):
        return False
    tokens = [tok for tok in normalize_spare_part_text(text).split() if tok]
    if not tokens:
        return False
    return any(token in _PT_GENERAL_AREA_TOKENS for token in tokens)


def _pt_general_area_label(record: dict) -> str | None:
    raw_asset = clean_text(record.get("asset_id"))
    if raw_asset and _pt_is_general_area_value(raw_asset):
        return raw_asset
    if not raw_asset:
        return "Missing / Uncategorised Asset"
    return None


def _pt_extract_part_code(record: dict) -> str:
    for raw in (
        record.get("line_property"),
        record.get("original_description"),
        record.get("translated_description"),
        record.get("clean_description"),
    ):
        text = str(raw or "").upper()
        for token in _PT_PART_CODE_RE.findall(text):
            if token in {"ITEM", "THB", "PCS", "JOB"}:
                continue
            if token.startswith("WO"):
                continue
            return token
    return ""


def build_asset_family_profiles(asset_rows: list[dict]) -> dict:
    grouped: dict[str, dict] = {}
    for asset in asset_rows:
        family_id = asset.get("asset_family_id") or _pt_slug(asset.get("asset_family") or asset.get("name"))
        bucket = grouped.setdefault(
            family_id,
            {
                "asset_family_id": family_id,
                "asset_family": asset.get("asset_family") or _pt_family_name_from_asset_name(asset.get("name") or ""),
                "asset_ids": [],
                "machine_groups": Counter(),
                "locations": Counter(),
            },
        )
        bucket["asset_ids"].append(asset.get("asset_id"))
        bucket["machine_groups"][asset.get("machine_group") or "Unknown / Review"] += 1
        bucket["locations"][asset.get("functional_location") or ""] += 1

    profiles = {}
    family_rows = []
    for family_id, bucket in grouped.items():
        primary_group = (bucket["machine_groups"].most_common(1) or [("Unknown / Review", 0)])[0][0]
        primary_location = (bucket["locations"].most_common(1) or [("", 0)])[0][0]
        profile = build_asset_profile(
            {
                "asset_id": family_id,
                "name": bucket["asset_family"],
                "machine_group": primary_group,
                "functional_location": primary_location,
            }
        )
        profile["assetFamilyId"] = family_id
        profile["assetFamilyName"] = bucket["asset_family"]
        profile["includedAssetIds"] = sorted(asset_id for asset_id in bucket["asset_ids"] if asset_id)
        profiles[family_id] = profile
        family_rows.append(
            {
                "asset_family_id": family_id,
                "asset_family": bucket["asset_family"],
                "machine_group": primary_group,
                "included_asset_ids": sorted(asset_id for asset_id in bucket["asset_ids"] if asset_id),
            }
        )

    return {
        "profiles": profiles,
        "rows": family_rows,
        "lookup": {row["asset_family_id"]: row for row in family_rows},
    }


def _pt_brand_model_from_remarks(remarks) -> str:
    """Pull the manufacturer/model out of an Asset_Master remark such as
    'Mfr/Model: Rational / iCombi Pro; Stage 2 import …' -> 'Rational / iCombi Pro'."""
    m = re.search(r"mfr\s*/?\s*model\s*:\s*([^;]+)", str(remarks or ""), re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _pt_brand_aliases(brand_model: str) -> list[str]:
    out = []
    for part in re.split(r"[/,]", brand_model or ""):
        nb = normalize_asset_text(part)
        if nb and len(nb) >= 3 and not nb.isdigit():
            out.append(nb)
    return out


def _pt_augment_profiles_with_brand(asset_profiles: dict, asset_rows: list[dict]) -> None:
    """Add each asset's manufacturer/model as profile aliases so PO lines that
    reference the asset by brand (e.g. 'Rational' / 'iCombi') match — generally,
    for every asset, not just known ones."""
    for row in asset_rows:
        prof = asset_profiles.get(row.get("asset_id"))
        if not prof:
            continue
        brand_aliases = _pt_brand_aliases(row.get("brand_model"))
        if brand_aliases:
            prof["aliases"] = sorted(set(prof.get("aliases") or []) | set(brand_aliases))


def build_spare_part_asset_profiles() -> dict:
    sig = (_file_signature(ASSET_MASTER_PATH),)
    if _PT_ASSET_CATALOG_CACHE["result"] is not None and _PT_ASSET_CATALOG_CACHE["sig"] == sig:
        return _PT_ASSET_CATALOG_CACHE["result"]

    asset_rows: list[dict] = []
    try:
        from asset_mapping import load_asset_mapping

        mapping = load_asset_mapping(str(DEFAULT_DATA_DIR))
        for asset_id, entry in (mapping.get("asset_map") or {}).items():
            name = clean_text(entry.get("display_name") or entry.get("mappedAssetName") or asset_id)
            machine_group = clean_text(entry.get("machine_group") or entry.get("mappedMainAssetGroup")) or "Unknown / Review"
            location = clean_text(entry.get("location") or entry.get("mappedLocation"))
            criticality = clean_text(entry.get("raw_criticality") or entry.get("criticality")) or "Unclassified"
            family_name = _pt_family_name_from_asset_name(name)
            asset_rows.append(
                {
                    "asset_id": asset_id,
                    "name": name or asset_id,
                    "machine_group": machine_group,
                    "functional_location": location,
                    "criticality": criticality,
                    "asset_family": family_name,
                    "asset_family_id": _pt_slug(family_name),
                    "brand_model": _pt_brand_model_from_remarks(entry.get("remarks")),
                }
            )
    except Exception:
        for asset_id, entry in _load_asset_list_lookup().items():
            name = clean_text(entry.get("name")) or asset_id
            family_name = _pt_family_name_from_asset_name(name)
            asset_rows.append(
                {
                    "asset_id": asset_id,
                    "name": name,
                    "machine_group": clean_text(entry.get("location")) or "Unknown / Review",
                    "functional_location": clean_text(entry.get("location")),
                    "criticality": clean_text(entry.get("criticality")) or "Unclassified",
                    "asset_family": family_name,
                    "asset_family_id": _pt_slug(family_name),
                }
            )

    asset_profiles = build_all_asset_profiles(asset_rows)
    _pt_augment_profiles_with_brand(asset_profiles, asset_rows)
    family_payload = build_asset_family_profiles(asset_rows)
    result = {
        "assets": asset_rows,
        "asset_lookup": {row["asset_id"]: row for row in asset_rows if row.get("asset_id")},
        "asset_profiles": asset_profiles,
        "family_profiles": family_payload["profiles"],
        "family_lookup": family_payload["lookup"],
        "family_rows": family_payload["rows"],
    }
    _PT_ASSET_CATALOG_CACHE["result"] = result
    _PT_ASSET_CATALOG_CACHE["sig"] = sig
    return result


def join_spare_part_usage_to_mr_wo_if_available(records: list[dict]) -> list[str]:
    errors: list[str] = []
    try:
        wo_lkp, asset_lkp = _pt_build_lookups(_pt_load_wo_records())
        for rec in records:
            matched = wo_lkp.get(rec.get("work_order_id")) or asset_lkp.get(rec.get("asset_id"))
            if matched:
                rec.update(_pt_extract_wo_fields(matched))
                rec["link_status"] = "Linked"
    except Exception as exc:
        errors.append(f"Work order linking failed: {exc}")
    return errors


def _pt_alias_hit(text: str, aliases: list[str]) -> bool:
    tokens = [tok for tok in normalize_spare_part_text(text).split() if tok]
    if not tokens:
        return False
    for alias in aliases:
        parts = [tok for tok in alias.split() if tok]
        if not parts or len(parts) > len(tokens):
            continue
        for index in range(len(tokens) - len(parts) + 1):
            if tokens[index:index + len(parts)] == parts:
                return True
    return False


def _pt_refine_match_source(record: dict, profile: dict, match: dict, family_level: bool = False) -> str:
    source = match.get("matchSource") or "Description / remarks match"
    if family_level:
        return "Asset family match"
    if source == "Translated description match":
        source = "Description / remarks match"
    if source == "Description match":
        tx_text = " ".join(
            str(record.get(field) or "")
            for field in ("original_description", "translated_description", "clean_description")
        )
        wo_text = " ".join(
            str(record.get(field) or "")
            for field in ("wo_description", "wo_translated_description")
        )
        aliases = profile.get("aliases") or []
        if wo_text and _pt_alias_hit(wo_text, aliases) and not _pt_alias_hit(tx_text, aliases):
            return "Related WO/MR description match"
        return "Description / remarks match"
    return source


def classify_spare_part_consumption_record(record: dict, asset_catalog: dict) -> dict:
    asset_lookup = asset_catalog.get("asset_lookup") or {}
    family_lookup = asset_catalog.get("family_lookup") or {}
    asset_matches = match_record_to_asset_profiles(
        record,
        asset_catalog.get("asset_profiles") or {},
        {"include_related": True, "limit": 5},
    )
    family_matches = match_record_to_asset_profiles(
        record,
        asset_catalog.get("family_profiles") or {},
        {"include_related": True, "limit": 5},
    )

    asset_match = asset_matches[0] if asset_matches else None
    resolved_asset = asset_lookup.get(asset_match.get("matchedAssetId")) if asset_match else None
    resolved_family = None
    family_match = None

    if resolved_asset:
        resolved_family = family_lookup.get(resolved_asset.get("asset_family_id"))
    elif family_matches:
        family_match = family_matches[0]
        resolved_family = family_lookup.get(family_match.get("matchedAssetId"))

    match_source = ""
    match_confidence = "Low"
    possible_mismatch = False
    if asset_match and resolved_asset:
        asset_profile = (asset_catalog.get("asset_profiles") or {}).get(resolved_asset.get("asset_id")) or {}
        match_source = _pt_refine_match_source(record, asset_profile, asset_match)
        match_confidence = asset_match.get("confidence") or "Medium"
        possible_mismatch = bool(asset_match.get("possibleAssetCodingMismatch"))
    elif family_match and resolved_family:
        family_profile = (asset_catalog.get("family_profiles") or {}).get(resolved_family.get("asset_family_id")) or {}
        match_source = _pt_refine_match_source(record, family_profile, family_match, family_level=True)
        match_confidence = family_match.get("confidence") or "Low"
        possible_mismatch = bool(family_match.get("possibleAssetCodingMismatch"))

    general_area = _pt_general_area_label(record)
    machine_group = clean_text(
        (resolved_asset or {}).get("machine_group")
        or (resolved_family or {}).get("machine_group")
        or record.get("equipment_type")
    )
    if not match_source:
        if machine_group:
            match_source = "Machine group match"
        elif general_area:
            match_source = "General area record"
        else:
            match_source = "Description / remarks match"

    part_name = clean_text(record.get("translated_description") or record.get("clean_description") or record.get("original_description")) or "Unknown Part"
    part_code = _pt_extract_part_code(record)
    family_name = (
        (resolved_asset or {}).get("asset_family")
        or (resolved_family or {}).get("asset_family")
        or ""
    )
    family_id = (
        (resolved_asset or {}).get("asset_family_id")
        or (resolved_family or {}).get("asset_family_id")
        or ""
    )

    data_quality_flags = []
    if general_area:
        data_quality_flags.append("General area coded")
    if possible_mismatch:
        data_quality_flags.append("Possible asset coding mismatch")
    if not clean_text(record.get("asset_id")):
        data_quality_flags.append("Missing asset ID")
    if not machine_group:
        data_quality_flags.append("Missing machine group")
    if not part_code and not part_name:
        data_quality_flags.append("Missing part code")
    if record.get("quantity_used") is None or record.get("total_consumption") is None:
        data_quality_flags.append("Missing quantity/value")
    if not data_quality_flags:
        data_quality_flags = ["Valid"]

    record_search_terms = [
        clean_text(record.get("asset_id")),
        clean_text(record.get("equipment_name")),
        clean_text((resolved_asset or {}).get("asset_id")),
        clean_text((resolved_asset or {}).get("name")),
        family_name,
        machine_group,
        general_area or "",
        part_code,
        part_name,
        clean_text(record.get("original_description")),
        clean_text(record.get("translated_description")),
        clean_text(record.get("wo_description")),
        clean_text(record.get("wo_translated_description")),
        clean_text(record.get("wo_location")),
    ]
    search_terms = sorted({term for term in record_search_terms if term})

    return {
        "resolved_asset_id": (resolved_asset or {}).get("asset_id") or "",
        "resolved_asset_name": (resolved_asset or {}).get("name") or clean_text(record.get("equipment_name")) or clean_text(record.get("asset_id")),
        "asset_family": family_name,
        "asset_family_id": family_id,
        "machine_group": machine_group,
        "criticality": clean_text((resolved_asset or {}).get("criticality") or record.get("equipment_criticality")) or "Unclassified",
        "general_area": general_area or "",
        "part_code": part_code,
        "part_name": part_name,
        "match_source": match_source,
        "match_confidence": match_confidence,
        "possible_asset_coding_mismatch": possible_mismatch,
        "is_direct_match": bool(resolved_asset and match_source == "Asset ID match" and not possible_mismatch),
        "is_related_match": bool((resolved_asset or resolved_family) and not (resolved_asset and match_source == "Asset ID match" and not possible_mismatch)),
        "data_quality_flags": data_quality_flags,
        "primary_data_quality_flag": data_quality_flags[0] if data_quality_flags else "Valid",
        "mr_wo_reference": clean_text(record.get("work_order_id") or record.get("wo_request_id")),
        "search_text": " ".join(search_terms).lower(),
        "search_terms": search_terms,
        "consumption_keys": {
            "asset": (resolved_asset or {}).get("asset_id") or "",
            "asset_family": family_id or "",
            "machine_group": _pt_slug(machine_group) if machine_group else "",
            "general_area": _pt_slug(general_area) if general_area else "",
            "part": _pt_slug(part_code or part_name),
        },
        "consumption_labels": {
            "asset": (resolved_asset or {}).get("name") or clean_text(record.get("equipment_name")) or clean_text(record.get("asset_id")) or "Unresolved Asset",
            "asset_family": family_name or "Unresolved Family",
            "machine_group": machine_group or "Unclassified",
            "general_area": general_area or "No General Area",
            "part": part_name,
        },
    }


def _new_consumption_group(view_mode: str, group_key: str, label: str) -> dict:
    return {
        "view_mode": view_mode,
        "group_key": group_key,
        "label": label,
        "total_consumption": 0.0,
        "total_qty": 0.0,
        "line_count": 0,
        "unique_parts": set(),
        "asset_ids": set(),
        "asset_labels": {},
        "asset_families": set(),
        "family_labels": {},
        "machine_groups": set(),
        "machine_group_labels": {},
        "top_part_totals": defaultdict(float),
        "top_part_labels": {},
        "top_asset_totals": defaultdict(float),
        "top_family_totals": defaultdict(float),
        "top_group_totals": defaultdict(float),
        "search_terms": set(),
        "confidence_counts": Counter(),
        "match_source_counts": Counter(),
        "data_quality_counts": Counter(),
        "direct_match_count": 0,
        "related_match_count": 0,
        "coding_mismatch_count": 0,
        "general_area_count": 0,
    }


def _pt_top_label(counter_map: dict, label_map: dict) -> str:
    if not counter_map:
        return ""
    top_key = max(counter_map, key=lambda key: (counter_map[key], label_map.get(key, key)))
    return label_map.get(top_key, top_key)


def _pt_group_match_quality(confidence_counts: Counter) -> str:
    if confidence_counts.get("High"):
        return "High"
    if confidence_counts.get("Medium"):
        return "Medium"
    return "Low"


def _aggregate_consumption_view(records: list[dict], view_mode: str) -> list[dict]:
    groups: dict[str, dict] = {}
    for record in records:
        key = ((record.get("consumption_keys") or {}).get(view_mode) or "").strip()
        label = ((record.get("consumption_labels") or {}).get(view_mode) or "").strip()
        if not key or not label:
            continue
        bucket = groups.setdefault(key, _new_consumption_group(view_mode, key, label))
        bucket["total_consumption"] += float(record.get("total_consumption") or 0)
        bucket["total_qty"] += float(record.get("quantity_used") or 0)
        bucket["line_count"] += 1
        part_key = _pt_slug(record.get("part_code") or record.get("part_name"))
        bucket["unique_parts"].add(part_key)
        bucket["top_part_totals"][part_key] += float(record.get("total_consumption") or 0)
        bucket["top_part_labels"][part_key] = record.get("part_name") or record.get("part_code") or "Unknown Part"

        asset_id = record.get("resolved_asset_id") or ""
        if asset_id:
            bucket["asset_ids"].add(asset_id)
            bucket["asset_labels"][asset_id] = record.get("resolved_asset_name") or asset_id
            bucket["top_asset_totals"][asset_id] += float(record.get("total_consumption") or 0)

        family_id = record.get("asset_family_id") or ""
        if family_id:
            bucket["asset_families"].add(family_id)
            bucket["family_labels"][family_id] = record.get("asset_family") or family_id
            bucket["top_family_totals"][family_id] += float(record.get("total_consumption") or 0)

        machine_group = record.get("machine_group") or ""
        if machine_group:
            machine_group_key = _pt_slug(machine_group)
            bucket["machine_groups"].add(machine_group_key)
            bucket["machine_group_labels"][machine_group_key] = machine_group
            bucket["top_group_totals"][machine_group_key] += float(record.get("total_consumption") or 0)

        bucket["search_terms"].update(record.get("search_terms") or [])
        bucket["confidence_counts"][record.get("match_confidence") or "Low"] += 1
        bucket["match_source_counts"][record.get("match_source") or "Unknown"] += 1
        for flag in record.get("data_quality_flags") or []:
            if flag != "Valid":
                bucket["data_quality_counts"][flag] += 1
        if record.get("is_direct_match"):
            bucket["direct_match_count"] += 1
        if record.get("is_related_match"):
            bucket["related_match_count"] += 1
        if record.get("possible_asset_coding_mismatch"):
            bucket["coding_mismatch_count"] += 1
        if record.get("general_area"):
            bucket["general_area_count"] += 1

    rows = []
    for key, bucket in groups.items():
        top_part = _pt_top_label(bucket["top_part_totals"], bucket["top_part_labels"])
        top_asset = _pt_top_label(bucket["top_asset_totals"], bucket["asset_labels"])
        top_family = _pt_top_label(bucket["top_family_totals"], bucket["family_labels"])
        top_group = _pt_top_label(bucket["top_group_totals"], bucket["machine_group_labels"])
        row = {
            "view_mode": view_mode,
            "group_key": key,
            "label": bucket["label"],
            "total_consumption": round(bucket["total_consumption"], 2),
            "total_qty": round(bucket["total_qty"], 2),
            "line_count": bucket["line_count"],
            "unique_parts_count": len(bucket["unique_parts"]),
            "asset_count": len(bucket["asset_ids"]),
            "asset_family_count": len(bucket["asset_families"]),
            "machine_group_count": len(bucket["machine_groups"]),
            "top_part": top_part,
            "top_consuming_asset": top_asset,
            "top_asset_family": top_family,
            "top_machine_group": top_group,
            "match_quality": _pt_group_match_quality(bucket["confidence_counts"]),
            "direct_match_count": bucket["direct_match_count"],
            "related_match_count": bucket["related_match_count"],
            "coding_mismatch_count": bucket["coding_mismatch_count"],
            "general_area_count": bucket["general_area_count"],
            "data_quality_summary": "; ".join(
                f"{label}: {count}"
                for label, count in bucket["data_quality_counts"].most_common(3)
            ),
            "search_text": " ".join(sorted(bucket["search_terms"])).lower(),
        }
        if view_mode == "asset":
            asset_criticality = next(
                (
                    record.get("criticality")
                    for record in records
                    if (record.get("consumption_keys") or {}).get("asset") == key
                    and record.get("criticality")
                ),
                "Unclassified",
            )
            row.update(
                {
                    "asset_id": key,
                    "equipment_name": bucket["label"],
                    "asset_family": top_family or "",
                    "machine_group": top_group or "",
                    "criticality": asset_criticality,
                    "equipment_criticality": asset_criticality,
                }
            )
        elif view_mode == "asset_family":
            row.update(
                {
                    "asset_family_id": key,
                    "asset_family": bucket["label"],
                    "machine_group": top_group or "",
                    "assets_included": len(bucket["asset_ids"]),
                }
            )
        elif view_mode == "machine_group":
            row.update(
                {
                    "machine_group": bucket["label"],
                }
            )
        elif view_mode == "general_area":
            row.update(
                {
                    "general_area": bucket["label"],
                    "top_related_asset": top_asset,
                }
            )
        elif view_mode == "part":
            row.update(
                {
                    "part_key": key,
                    "part_code": next(
                        (
                            record.get("part_code")
                            for record in records
                            if (record.get("consumption_keys") or {}).get("part") == key
                            and record.get("part_code")
                        ),
                        "",
                    ),
                    "part_name": bucket["label"],
                }
            )
        rows.append(row)

    return sorted(rows, key=lambda row: (-row["total_consumption"], row["label"]))


def aggregate_consumption_by_asset(records: list[dict]) -> list[dict]:
    return _aggregate_consumption_view(records, "asset")


def aggregate_consumption_by_asset_family(records: list[dict]) -> list[dict]:
    return _aggregate_consumption_view(records, "asset_family")


def aggregate_consumption_by_machine_group(records: list[dict]) -> list[dict]:
    return _aggregate_consumption_view(records, "machine_group")


def aggregate_consumption_by_general_area(records: list[dict]) -> list[dict]:
    return _aggregate_consumption_view(records, "general_area")


def aggregate_consumption_by_part(records: list[dict]) -> list[dict]:
    return _aggregate_consumption_view(records, "part")


def get_spare_part_usage_drilldown(records: list[dict]) -> list[dict]:
    return sorted(records, key=lambda row: (str(row.get("project_date") or ""), str(row.get("transaction_id") or "")), reverse=True)


def search_spare_part_consumption_smart(rows: list[dict], query: str) -> list[dict]:
    term = normalize_spare_part_text(query)
    if not term:
        return rows
    return [row for row in rows if term in normalize_spare_part_text(row.get("search_text") or row.get("label") or "")]


_COL_PROJECT = 1
_COL_NAME = 3
_COL_TTYPE = 4
_COL_DATE = 5
_COL_TRANS_ID = 6
_COL_LINE_PROP = 7
_COL_DESC = 8
_COL_TOTAL = 9


def _read_project_transactions_source_frame(path: Path):
    suffix = path.suffix.lower()
    if suffix == ".csv":
        try:
            return pd.read_csv(path, header=None, dtype=str, encoding="utf-8-sig")
        except UnicodeDecodeError:
            return pd.read_csv(path, header=None, dtype=str, encoding="latin1")
    sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=str)
    if not sheets:
        raise ValueError("Workbook contains no readable sheets.")
    if "Sheet1" in sheets:
        return sheets["Sheet1"]
    return max(sheets.values(), key=lambda frame: getattr(frame, "shape", [0])[0])


def _build_project_transactions_payload_from_path(path: Path | None) -> dict:
    """Parse a Project actual transactions export and return structured payload."""

    if not path or not path.exists():
        return {
            "status": "missing",
            "error": "Project actual transactions file not uploaded",
            "transactions": [], "top_parts": [], "by_asset": [],
            "manual_review": [], "summary": {}, "charts": {}, "consumption_analysis": {}, "errors": [],
        }

    _PT_CACHE_V = 3  # bump to invalidate stale cache after code changes
    try:
        _al_sig = _file_signature(ASSET_MASTER_PATH)
        _wo_sig = _pt_work_order_sources_signature()
        current_mtime = (_PT_CACHE_V, _file_signature(path), _al_sig, _wo_sig)
        if _PT_CACHE["result"] is not None and _PT_CACHE["mtime"] == current_mtime:
            return _PT_CACHE["result"]
    except OSError:
        current_mtime = None

    if current_mtime is not None:
        persistent_cached = _read_persistent_payload_cache("project_transactions", current_mtime)
        if persistent_cached is not None:
            _PT_CACHE["result"] = persistent_cached
            _PT_CACHE["mtime"] = current_mtime
            return persistent_cached

    try:
        raw = _read_project_transactions_source_frame(path)
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Could not read file: {exc}",
            "transactions": [], "top_parts": [], "by_asset": [],
            "manual_review": [], "summary": {}, "charts": {}, "consumption_analysis": {}, "errors": [str(exc)],
        }

    errors: list[str] = []
    records: list[dict] = []
    non_item_rows = 0

    n_cols = raw.shape[1]

    def _col(row: tuple, idx: int) -> str:
        try:
            v = row[idx + 1] if idx < n_cols else ""  # +1: index is row[0] in itertuples
            s = str(v).strip()
            return s if s not in ("nan", "None") else ""
        except Exception:
            return ""

    for row in raw.itertuples():
        if _col(row, _COL_TTYPE).lower() != "item":
            non_item_rows += 1
            continue
        project = _col(row, _COL_PROJECT)
        if not project or project == "5":
            continue
        name_raw = _col(row, _COL_NAME)
        if not name_raw:
            continue

        date_raw = row[_COL_DATE + 1] if _COL_DATE < n_cols else None  # +1: index offset in itertuples
        trans_id = _col(row, _COL_TRANS_ID)
        line_prop = _col(row, _COL_LINE_PROP)
        desc_raw = _col(row, _COL_DESC)
        total_str = _col(row, _COL_TOTAL)

        work_order_id, asset_id = _pt_parse_name(name_raw)
        quantity, item_desc = _pt_parse_desc(desc_raw)
        project_date = _pt_parse_excel_date(date_raw)
        date_status = "OK" if project_date else "Invalid date"

        try:
            total_consumption = float(total_str.replace(",", "")) if total_str else None
        except ValueError:
            total_consumption = None

        unit_cost = (round(total_consumption / quantity, 2)
                     if (quantity and quantity > 0 and total_consumption is not None) else None)

        has_thai = bool(_THAI_RE.search(item_desc)) if item_desc else False
        translation_status = "No translation needed"
        translated_desc = item_desc or ""

        if has_thai and item_desc:
            try:
                translated = _translate_desc(item_desc)
                if translated and translated != item_desc:
                    translated_desc = translated
                    translation_status = "Translated"
                else:
                    translation_status = "Pending translation"
            except Exception:
                translation_status = "Translation failed"

        clean_desc = _pt_clean_desc(item_desc)
        item_category, confidence, reason = _pt_classify(
            (translated_desc or item_desc or "").lower()
        )
        parse_status = "Review" if (not work_order_id and not asset_id) or quantity is None else "OK"

        records.append({
            "record_uid": trans_id or f"pt-{len(records) + 1}",
            "project": project,
            "work_order_id": work_order_id,
            "asset_id": asset_id,
            "name_raw": name_raw,
            "transaction_type": "Item",
            "project_date": project_date or "",
            "transaction_id": trans_id,
            "line_property": line_prop,
            "quantity_used": quantity,
            "original_description": item_desc or desc_raw,
            "translated_description": translated_desc,
            "clean_description": clean_desc,
            "item_category": item_category,
            "classification_confidence": confidence,
            "classification_reason": reason,
            "total_consumption": total_consumption,
            "unit_cost_estimate": unit_cost,
            "translation_status": translation_status,
            "has_thai": has_thai,
            "parse_status": parse_status,
            "date_status": date_status,
            "link_status": "Unlinked",
            "equipment_name": "", "equipment_type": "",
            "equipment_criticality": "", "maintenance_type": "",
            "wo_actual_start": "", "wo_actual_end": "", "wo_severity": "",
            "wo_request_id": "", "wo_description": "", "wo_translated_description": "", "wo_location": "",
        })

    if not records:
        return {
            "status": "no_data",
            "error": "No spare part consumption rows found",
            "transactions": [], "top_parts": [], "by_asset": [],
            "manual_review": [], "summary": {}, "charts": {}, "consumption_analysis": {}, "errors": errors,
        }

    errors.extend(join_spare_part_usage_to_mr_wo_if_available(records))

    # Enrich equipment fields from Asset_Master.xlsx for any still-unlinked records
    try:
        al = _load_asset_list_lookup()
        for rec in records:
            if rec["asset_id"] and (not rec["equipment_name"] or not rec["equipment_criticality"]):
                info = al.get(rec["asset_id"])
                if info:
                    if not rec["equipment_name"]:
                        rec["equipment_name"] = info["name"]
                    if not rec["equipment_criticality"]:
                        rec["equipment_criticality"] = info["criticality"]
                    if not rec["equipment_type"]:
                        rec["equipment_type"] = info["location"]
    except Exception as exc:
        errors.append(f"Asset list enrichment failed: {exc}")

    asset_catalog = build_spare_part_asset_profiles()
    for rec in records:
        rec.update(classify_spare_part_consumption_record(rec, asset_catalog))
        if not rec.get("equipment_name") and rec.get("resolved_asset_name"):
            rec["equipment_name"] = rec["resolved_asset_name"]
        if not rec.get("equipment_type") and rec.get("machine_group"):
            rec["equipment_type"] = rec["machine_group"]
        if not rec.get("equipment_criticality") and rec.get("criticality"):
            rec["equipment_criticality"] = rec["criticality"]

    total_val = sum(r["total_consumption"] or 0 for r in records)
    unique_wo = len({r["work_order_id"] for r in records if r["work_order_id"]})
    unique_assets = len({r["resolved_asset_id"] or r["asset_id"] for r in records if (r["resolved_asset_id"] or r["asset_id"])})
    unique_parts = len({_pt_slug(r["part_code"] or r["part_name"]) for r in records if (r["part_code"] or r["part_name"])})
    thai_count = sum(1 for r in records if r["has_thai"])
    unlinked = sum(1 for r in records if r["link_status"] in ("Unlinked", "Work order data unavailable"))
    coding_mismatches = sum(1 for r in records if r.get("possible_asset_coding_mismatch"))
    missing_project_date_rows = sum(1 for r in records if not r.get("project_date"))
    missing_work_order_reference_rows = sum(1 for r in records if not r.get("work_order_id"))

    monthly: dict[str, float] = {}
    for r in records:
        mk = (r["project_date"] or "")[:7]
        if mk:
            monthly[mk] = monthly.get(mk, 0) + (r["total_consumption"] or 0)
    monthly_trend = [{"month": k, "total": round(v, 2)} for k, v in sorted(monthly.items())]

    by_desc: dict[str, dict] = {}
    for r in records:
        part_key = _pt_slug(r.get("part_code") or r.get("part_name") or r.get("clean_description") or r.get("original_description"))
        if part_key not in by_desc:
            by_desc[part_key] = {
                "description": r.get("part_name") or r.get("clean_description") or r.get("original_description") or "Unknown",
                "translated": r.get("part_name") or r.get("translated_description") or r.get("clean_description") or "Unknown",
                "part_code": r.get("part_code") or "",
                "category": r["item_category"],
                "total_consumption": 0.0,
                "total_qty": 0.0,
                "wo_ids": set(),
                "asset_ids": set(),
                "costs": [],
                "_asset_breakdown": {},
            }
        by_desc[part_key]["total_consumption"] += r["total_consumption"] or 0
        by_desc[part_key]["total_qty"] += r["quantity_used"] or 0
        if r["work_order_id"]:
            by_desc[part_key]["wo_ids"].add(r["work_order_id"])
        if r["resolved_asset_id"] or r["asset_id"]:
            by_desc[part_key]["asset_ids"].add(r["resolved_asset_id"] or r["asset_id"])
        if r["unit_cost_estimate"] is not None:
            by_desc[part_key]["costs"].append(r["unit_cost_estimate"])

        asset_key = r["resolved_asset_id"] or r["asset_id"] or ""
        asset_breakdown = by_desc[part_key]["_asset_breakdown"]
        if asset_key not in asset_breakdown:
            asset_breakdown[asset_key] = {
                "asset_id": asset_key,
                "equipment_name": r.get("resolved_asset_name") or r.get("equipment_name") or "",
                "total_consumption": 0.0,
                "total_qty": 0.0,
                "wo_ids": set(),
            }
        asset_breakdown[asset_key]["total_consumption"] += r["total_consumption"] or 0
        asset_breakdown[asset_key]["total_qty"] += r["quantity_used"] or 0
        if r["work_order_id"]:
            asset_breakdown[asset_key]["wo_ids"].add(r["work_order_id"])
        if r.get("resolved_asset_name") and not asset_breakdown[asset_key]["equipment_name"]:
            asset_breakdown[asset_key]["equipment_name"] = r["resolved_asset_name"]

    def _fin_ab(ab_raw: dict) -> list:
        result = []
        for value in ab_raw.values():
            wos = value["wo_ids"] - {""}
            result.append(
                {
                    "asset_id": value["asset_id"],
                    "equipment_name": value["equipment_name"] or value["asset_id"] or "Unknown",
                    "wo_count": len(wos),
                    "wo_ids": sorted(wos),
                    "total_qty": round(value["total_qty"], 2),
                    "total_consumption": round(value["total_consumption"], 2),
                }
            )
        return sorted(result, key=lambda row: (-row["wo_count"], -row["total_consumption"]))

    def _fin(item: dict) -> dict:
        costs = item.pop("costs", [])
        wos = item.pop("wo_ids", set())
        assets = item.pop("asset_ids", set())
        ab_raw = item.pop("_asset_breakdown", {})
        return {
            **item,
            "wo_count": len(wos - {""}),
            "asset_count": len(assets - {""}),
            "avg_unit_cost": round(sum(costs) / len(costs), 2) if costs else None,
            "total_consumption": round(item["total_consumption"], 2),
            "total_qty": round(item["total_qty"], 2),
            "asset_breakdown": _fin_ab(ab_raw),
        }

    all_parts = [_fin(dict(value)) for value in by_desc.values()]
    top10_val = sorted(all_parts, key=lambda row: row["total_consumption"], reverse=True)[:10]
    top10_qty = sorted(all_parts, key=lambda row: row["total_qty"], reverse=True)[:10]
    by_part_asset = sorted(all_parts, key=lambda row: (-row["wo_count"], -row["total_consumption"]))

    asset_list = aggregate_consumption_by_asset(records)
    family_list = aggregate_consumption_by_asset_family(records)
    machine_group_list = aggregate_consumption_by_machine_group(records)
    general_area_list = aggregate_consumption_by_general_area(records)
    part_list = aggregate_consumption_by_part(records)

    by_cat: dict[str, float] = {}
    for r in records:
        by_cat[r["item_category"]] = by_cat.get(r["item_category"], 0) + (r["total_consumption"] or 0)
    cat_total = sum(by_cat.values()) or 1
    cat_breakdown = [
        {"category": key, "total": round(value, 2), "pct": round(value / cat_total * 100, 1)}
        for key, value in sorted(by_cat.items(), key=lambda item: item[1], reverse=True)
    ]

    by_machine_group: dict[str, float] = {}
    for r in records:
        if r.get("machine_group"):
            by_machine_group[r["machine_group"]] = by_machine_group.get(r["machine_group"], 0) + (r["total_consumption"] or 0)
    machine_group_breakdown = [
        {"equipment_type": key, "total": round(value, 2)}
        for key, value in sorted(by_machine_group.items(), key=lambda item: item[1], reverse=True)
    ]

    trans_cnt: dict[str, int] = {}
    for r in records:
        trans_cnt[r["translation_status"]] = trans_cnt.get(r["translation_status"], 0) + 1
    trans_breakdown = [{"status": key, "count": value} for key, value in trans_cnt.items()]

    manual_review = [
        r
        for r in records
        if any(flag != "Valid" for flag in (r.get("data_quality_flags") or []))
        or r.get("match_confidence") == "Low"
        or r["link_status"] in ("Unlinked", "Work order data unavailable")
    ]

    consumption_analysis = {
        "filters": {
            "view_modes": [
                {"value": "asset", "label": "By Asset"},
                {"value": "asset_family", "label": "By Asset Family"},
                {"value": "machine_group", "label": "By Machine Group"},
                {"value": "general_area", "label": "By General Area"},
                {"value": "part", "label": "Part Relationship"},
            ],
            "asset_families": [row["asset_family"] for row in family_list if row.get("asset_family")],
            "machine_groups": [row["machine_group"] for row in machine_group_list if row.get("machine_group")],
            "general_areas": [row["general_area"] for row in general_area_list if row.get("general_area")],
            "criticalities": sorted({row.get("equipment_criticality") for row in asset_list if row.get("equipment_criticality")}),
            "match_qualities": ["High", "Medium", "Low"],
        },
        "records": get_spare_part_usage_drilldown(records),
        "groups": {
            "asset": asset_list,
            "asset_family": family_list,
            "machine_group": machine_group_list,
            "general_area": general_area_list,
            "part": part_list,
        },
    }

    result = {
        "status": "ok",
        "errors": errors,
        "transactions": records,
        "top_parts": sorted(all_parts, key=lambda row: row["total_consumption"], reverse=True),
        "by_part_asset": by_part_asset,
        "by_asset": asset_list,
        "manual_review": manual_review[:300],
        "consumption_analysis": consumption_analysis,
        "summary": {
            "total_consumption": round(total_val, 2),
            "transaction_lines": len(records),
            "unique_work_orders": unique_wo,
            "unique_assets": unique_assets,
            "unique_spare_parts": unique_parts,
            "thai_description_count": thai_count,
            "unlinked_count": unlinked,
            "coding_mismatch_count": coding_mismatches,
            "non_item_rows": non_item_rows,
            "missing_project_date_rows": missing_project_date_rows,
            "missing_work_order_reference_rows": missing_work_order_reference_rows,
            "avg_consumption_per_wo": round(total_val / unique_wo, 2) if unique_wo else 0,
        },
        "charts": {
            "monthly_trend": monthly_trend,
            "top10_by_value": top10_val,
            "top10_by_qty": [
                {
                    "description": row["description"],
                    "translated": row["translated"],
                    "category": row["category"],
                    "total_qty": row["total_qty"],
                }
                for row in top10_qty
            ],
            "by_asset": asset_list[:30],
            "category_breakdown": cat_breakdown,
            "equipment_type_breakdown": machine_group_breakdown,
            "translation_status": trans_breakdown,
        },
    }
    _PT_CACHE["result"] = result
    _PT_CACHE["mtime"] = current_mtime
    if current_mtime is not None:
        _write_persistent_payload_cache("project_transactions", current_mtime, result)
    return result


def build_project_transactions_payload() -> dict:
    return _build_project_transactions_payload_from_path(_resolve_project_transactions_source_path())


def _project_transactions_all_years_records_from_path(path: Path) -> tuple[list[dict], list[str]]:
    payload = _build_project_transactions_payload_from_path(path)
    if payload.get("status") != "ok":
        message = payload.get("error") or f"{path.name} could not be parsed for all-years analysis"
        return [], [message]

    records = []
    for row in payload.get("transactions") or []:
        project_date = str(row.get("project_date") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", project_date):
            continue
        records.append({
            "project_date": project_date,
            "year": int(project_date[:4]),
            "month": project_date[:7],
            "transaction_id": row.get("transaction_id"),
            "work_order_id": row.get("work_order_id"),
            "asset_id": row.get("asset_id"),
            "original_description": row.get("original_description"),
            "translated_description": row.get("translated_description"),
            "clean_description": row.get("clean_description"),
            "item_category": row.get("item_category"),
            "quantity_used": row.get("quantity_used"),
            "total_consumption": row.get("total_consumption"),
            "unit_cost_estimate": row.get("unit_cost_estimate"),
            "translation_status": row.get("translation_status"),
            "has_thai": row.get("has_thai"),
            "link_status": row.get("link_status") or "Unlinked",
            "equipment_name": row.get("equipment_name") or "",
            "equipment_type": row.get("equipment_type") or "",
            "equipment_criticality": row.get("equipment_criticality") or "",
        })
    return records, payload.get("errors") or []


# ── All-Years CSV Transactions Parser ────────────────────────────────────────

# Financial year starts in April (month 4). FY label = the calendar year in which
# the financial year ENDS, e.g. Apr 2025 → Mar 2026 is "FY2026".
FY_START_MONTH = 4


def _fiscal_year(year: int, month: int) -> int:
    """Map a calendar (year, month) to its financial-year label."""
    return year + 1 if month >= FY_START_MONTH else year


def _fy_span_label(fy: int) -> str:
    """Human-readable FY span, e.g. 'Apr 2025 to Mar 2026' for fy=2026."""
    return f"Apr {fy - 1} to Mar {fy}"


# CSV column layout (0-indexed, after skipping textbox header rows)
_CSV_COL_DATE = 20
_CSV_COL_TRANS_ID = 21
_CSV_COL_LINE_PROP = 22
_CSV_COL_DESC = 23
_CSV_COL_QTY = 24
_CSV_COL_TOTAL = 25
_CSV_COL_NAME = 41   # "WRKO-00000020: ENUT-240178" — cleanest form


def _csv_parse_date(s: str) -> str | None:
    """Parse m/d/yyyy date string to YYYY-MM-DD."""
    s = (s or "").strip()
    if not s or s in ("nan", "None", "Project date"):
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _csv_col(row: list, idx: int) -> str:
    try:
        v = row[idx] if idx < len(row) else ""
        s = str(v).strip()
        return s if s not in ("nan", "None") else ""
    except Exception:
        return ""


def build_all_years_transactions_payload() -> dict:
    """Parse multi-year Project actual transactions CSV and return yearly comparison."""
    import csv as _csv

    annual_import_paths = _project_transactions_import_history_paths()
    if not CSV_ALL_YEARS_PATH.exists() and not annual_import_paths:
        return {
            "status": "missing",
            "error": "Project actual transactions history not found",
            "years": [], "yearly_summary": [], "monthly_by_year": {},
            "category_by_year": {}, "top_parts_by_year": {}, "errors": [],
        }

    _AY_CACHE_V = 3  # bump to invalidate stale cache after code changes (now FY-keyed)
    try:
        _al_sig = _file_signature(ASSET_MASTER_PATH)
        _wo_sig = _pt_work_order_sources_signature()
        current_mtime = (
            _AY_CACHE_V,
            _file_signature(CSV_ALL_YEARS_PATH) if CSV_ALL_YEARS_PATH.exists() else None,
            tuple(_file_signature(path) for path in annual_import_paths),
            _al_sig,
            _wo_sig,
        )
        if _AY_CACHE["result"] is not None and _AY_CACHE["mtime"] == current_mtime:
            return _AY_CACHE["result"]
    except OSError:
        current_mtime = None

    if current_mtime is not None:
        persistent_cached = _read_persistent_payload_cache("project_transactions_all", current_mtime)
        if persistent_cached is not None:
            _AY_CACHE["result"] = persistent_cached
            _AY_CACHE["mtime"] = current_mtime
            return persistent_cached

    raw_rows = []
    if CSV_ALL_YEARS_PATH.exists():
        try:
            with open(CSV_ALL_YEARS_PATH, encoding="utf-8-sig", errors="replace") as f:
                raw_rows = list(_csv.reader(f))
        except Exception as exc:
            return {
                "status": "error",
                "error": f"Could not read CSV: {exc}",
                "years": [], "yearly_summary": [], "monthly_by_year": {},
                "category_by_year": {}, "top_parts_by_year": {}, "errors": [str(exc)],
            }

    errors: list[str] = []
    records: list[dict] = []
    seen: set[str] = set()

    for row in raw_rows:
        date_raw = _csv_col(row, _CSV_COL_DATE)
        total_raw = _csv_col(row, _CSV_COL_TOTAL)

        # Skip header rows (textbox placeholders or column label rows)
        if not date_raw or date_raw in ("Project date",) or "textbox" in date_raw.lower():
            continue
        if not total_raw or "textbox" in total_raw.lower() or total_raw in ("Total consumption", "Grand total"):
            continue

        project_date = _csv_parse_date(date_raw)
        if not project_date:
            continue

        trans_id = _csv_col(row, _CSV_COL_TRANS_ID)
        desc_raw = _csv_col(row, _CSV_COL_DESC)
        total_str = _csv_col(row, _CSV_COL_TOTAL)
        name_raw = _csv_col(row, _CSV_COL_NAME)

        # Deduplicate: use transaction_id as primary key; fall back to composite for rows without one
        dedup_key = trans_id if trans_id else f"noId|{date_raw[:10]}|{desc_raw[:40]}|{total_str}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        quantity, item_desc = _pt_parse_desc(desc_raw)
        work_order_id, asset_id = _pt_parse_name(name_raw)

        try:
            total_consumption = float(total_str.replace(",", "")) if total_str else None
        except ValueError:
            total_consumption = None

        unit_cost = (round(total_consumption / quantity, 2)
                     if (quantity and quantity > 0 and total_consumption is not None) else None)

        has_thai = bool(_THAI_RE.search(item_desc)) if item_desc else False
        translated_desc = item_desc or ""
        translation_status = "No translation needed"

        if has_thai and item_desc:
            try:
                translated = _translate_desc(item_desc)
                if translated and translated != item_desc:
                    translated_desc = translated
                    translation_status = "Translated"
                else:
                    translation_status = "Pending translation"
            except Exception:
                translation_status = "Translation failed"

        clean_desc = _pt_clean_desc(item_desc)
        item_category, confidence, reason = _pt_classify(
            (translated_desc or item_desc or "").lower()
        )

        records.append({
            "project_date": project_date,
            "year": int(project_date[:4]),
            "month": project_date[:7],
            "transaction_id": trans_id,
            "work_order_id": work_order_id,
            "asset_id": asset_id,
            "original_description": item_desc or desc_raw,
            "translated_description": translated_desc,
            "clean_description": clean_desc,
            "item_category": item_category,
            "quantity_used": quantity,
            "total_consumption": total_consumption,
            "unit_cost_estimate": unit_cost,
            "translation_status": translation_status,
            "has_thai": has_thai,
            "link_status": "Unlinked",
            "equipment_name": "",
            "equipment_type": "",
        })

    imported_annual_errors = []
    annual_seen = {
        (r.get("transaction_id") or "", r.get("project_date") or "", (r.get("clean_description") or r.get("original_description") or "")[:80], r.get("total_consumption"))
        for r in records
    }
    for annual_path in annual_import_paths:
        annual_records, annual_errors = _project_transactions_all_years_records_from_path(annual_path)
        if annual_errors:
            imported_annual_errors.extend([f"{annual_path.name}: {message}" for message in annual_errors])
        for row in annual_records:
            dedup_key = (
                row.get("transaction_id") or "",
                row.get("project_date") or "",
                (row.get("clean_description") or row.get("original_description") or "")[:80],
                row.get("total_consumption"),
            )
            if dedup_key in annual_seen:
                continue
            annual_seen.add(dedup_key)
            records.append(row)

    errors.extend(imported_annual_errors)

    if not records:
        return {
            "status": "no_data",
            "error": "No valid rows found in project transactions history",
            "years": [], "yearly_summary": [], "monthly_by_year": {},
            "category_by_year": {}, "top_parts_by_year": {}, "errors": errors,
        }

    # Link to work order data
    try:
        wo_lkp, asset_lkp = _pt_build_lookups(_pt_load_wo_records())
        for rec in records:
            matched = wo_lkp.get(rec["work_order_id"]) or asset_lkp.get(rec["asset_id"])
            if matched:
                rec.update(_pt_extract_wo_fields(matched))
                rec["link_status"] = "Linked"
    except Exception as exc:
        errors.append(f"WO linking failed: {exc}")

    # Enrich equipment fields from Asset_Master.xlsx for any still-unlinked records
    try:
        al = _load_asset_list_lookup()
        for rec in records:
            if rec["asset_id"] and (not rec["equipment_name"] or not rec["equipment_criticality"]):
                info = al.get(rec["asset_id"])
                if info:
                    if not rec["equipment_name"]:
                        rec["equipment_name"] = info["name"]
                    if not rec["equipment_criticality"]:
                        rec["equipment_criticality"] = info["criticality"]
    except Exception as exc:
        errors.append(f"Asset list enrichment failed: {exc}")

    # Attach financial-year label to every record (calendar year/month preserved).
    for r in records:
        mo = int(r["month"][5:7]) if r.get("month") else 1
        r["fy"] = _fiscal_year(r["year"], mo)

    # "years" is the financial-year axis used by the consumption charts/tabs.
    years = sorted({r["fy"] for r in records})

    # Per-FY summary
    yearly_summary = []
    for yr in years:
        yr_rows = [r for r in records if r["fy"] == yr]
        total = sum(r["total_consumption"] or 0 for r in yr_rows)
        yearly_summary.append({
            "year": yr,
            "fy": yr,
            "fy_label": f"FY{yr}",
            "fy_span": _fy_span_label(yr),
            "total_consumption": round(total, 2),
            "transaction_lines": len(yr_rows),
            "unique_assets": len({r["asset_id"] for r in yr_rows if r["asset_id"]}),
            "unique_work_orders": len({r["work_order_id"] for r in yr_rows if r["work_order_id"]}),
            "unique_parts": len({r["clean_description"] for r in yr_rows if r["clean_description"]}),
        })

    # Monthly trend per FY: {fy: [{month, total}]} — month stays 'YYYY-MM';
    # the frontend orders the axis Apr→Mar.
    monthly_by_year: dict[str, list] = {}
    for yr in years:
        monthly: dict[str, float] = {}
        for r in records:
            if r["fy"] == yr and r["month"]:
                monthly[r["month"]] = monthly.get(r["month"], 0) + (r["total_consumption"] or 0)
        monthly_by_year[str(yr)] = [{"month": k, "total": round(v, 2)}
                                     for k, v in sorted(monthly.items())]

    # Category breakdown per FY: {fy: [{category, total, pct}]}
    category_by_year: dict[str, list] = {}
    for yr in years:
        by_cat: dict[str, float] = {}
        for r in records:
            if r["fy"] == yr:
                by_cat[r["item_category"]] = by_cat.get(r["item_category"], 0) + (r["total_consumption"] or 0)
        cat_total = sum(by_cat.values()) or 1
        category_by_year[str(yr)] = [
            {"category": k, "total": round(v, 2), "pct": round(v / cat_total * 100, 1)}
            for k, v in sorted(by_cat.items(), key=lambda x: x[1], reverse=True)
        ]

    # Parts per FY with full aggregation
    top_parts_by_year: dict[str, list] = {}
    for yr in years:
        by_desc: dict[str, dict] = {}
        for r in records:
            if r["fy"] != yr:
                continue
            k = r["clean_description"] or r["original_description"] or "Unknown"
            if k not in by_desc:
                by_desc[k] = {
                    "description": k,
                    "translated": r["translated_description"] or k,
                    "category": r["item_category"],
                    "total": 0.0, "qty": 0.0,
                    "wo_ids": set(), "asset_ids": set(), "costs": [],
                }
            by_desc[k]["total"] += r["total_consumption"] or 0
            by_desc[k]["qty"] += r["quantity_used"] or 0
            if r["work_order_id"]:
                by_desc[k]["wo_ids"].add(r["work_order_id"])
            if r["asset_id"]:
                by_desc[k]["asset_ids"].add(r["asset_id"])
            if r["unit_cost_estimate"] is not None:
                by_desc[k]["costs"].append(r["unit_cost_estimate"])
        top_parts_by_year[str(yr)] = [
            {
                "description": v["description"],
                "translated": v["translated"],
                "category": v["category"],
                "total": round(v["total"], 2),
                "qty": round(v["qty"], 2),
                "wo_count": len(v["wo_ids"]),
                "asset_count": len(v["asset_ids"]),
                "avg_unit_cost": round(sum(v["costs"]) / len(v["costs"]), 2) if v["costs"] else None,
            }
            for v in sorted(by_desc.values(), key=lambda x: x["total"], reverse=True)
        ]

    # Year-over-year consumption change between consecutive years.
    # (Consumption rising is not necessarily good, so this is "consumption %", not "growth".)
    yoy_growth = []
    for i in range(1, len(yearly_summary)):
        prev = yearly_summary[i - 1]
        curr = yearly_summary[i]
        prev_total = prev["total_consumption"]
        diff = round(curr["total_consumption"] - (prev_total or 0), 2)
        if not prev_total:
            # Previous FY had no consumption — % is undefined; show as "New".
            yoy_growth.append({"from": prev["year"], "to": curr["year"], "growth_pct": None,
                               "consumption_pct": None, "consumption_diff": diff, "label": "New"})
        else:
            pct = round(diff / prev_total * 100, 1)
            yoy_growth.append({"from": prev["year"], "to": curr["year"], "growth_pct": pct,
                               "consumption_pct": pct, "consumption_diff": diff,
                               "label": f"{'+' if pct > 0 else ''}{pct}%"})

    result = {
        "status": "ok",
        "errors": errors,
        "years": years,
        "fy_start_month": FY_START_MONTH,
        "total_records": len(records),
        "yearly_summary": yearly_summary,
        "monthly_by_year": monthly_by_year,
        "category_by_year": category_by_year,
        "top_parts_by_year": top_parts_by_year,
        "yoy_growth": yoy_growth,
        "transactions": [
            {
                "project_date": r["project_date"],
                "year": r["year"],
                "fy": r["fy"],
                "month": r["month"],
                "transaction_id": r["transaction_id"],
                "work_order_id": r["work_order_id"],
                "asset_id": r["asset_id"],
                "original_description": r["original_description"],
                "translated_description": r["translated_description"],
                "clean_description": r["clean_description"],
                "item_category": r["item_category"],
                "quantity_used": r["quantity_used"],
                "total_consumption": r["total_consumption"],
                "unit_cost_estimate": r["unit_cost_estimate"],
                "link_status": r["link_status"],
                "equipment_name": r["equipment_name"],
            }
            for r in records
        ],
    }
    _AY_CACHE["result"] = result
    _AY_CACHE["mtime"] = current_mtime
    if current_mtime is not None:
        _write_persistent_payload_cache("project_transactions_all", current_mtime, result)
    return result


# ── External Purchase Orders (Gen PO in D365 Rev.01) ─────────────────────────

_EPO_CACHE: dict = {"result": None, "mtime": None}

_UNIT_NORM_MAP: dict[str, str] = {
    "PCS": "PCS", "PC": "PCS", "PCS.": "PCS", "PC.": "PCS", "Pcs.": "PCS", "pcs.": "PCS",
    "JOB": "JOB", "JOB.": "JOB", "Job": "JOB", "Job.": "JOB", "job.": "JOB",
    "DRUM": "DRUM", "DRM": "DRUM", "DRM.": "DRUM", "DRUM.": "DRUM", "Drum.": "DRUM",
    "Drm.": "DRUM", "DURM.": "DRUM",
    "SET": "SET", "SET.": "SET", "Set": "SET",
    "BOX": "BOX", "BOX.": "BOX", "Box": "BOX",
    "BOT": "BOT", "BOT.": "BOT", "bot": "BOT",
    "BAG": "BAG", "BAG.": "BAG", "Bag.": "BAG", "กระสอบ": "BAG", "Sack.": "BAG", "Sack": "BAG",
    "KG": "KG", "KG.": "KG", "Kg.": "KG",
    "NO": "NO", "NO.": "NO",
    "ROLL": "ROLL", "Roll": "ROLL", "roll": "ROLL",
    "PACK": "PACK", "PACK.": "PACK", "Pack.": "PACK",
    "EA.": "EA",
}


def _epo_norm_unit(raw: str) -> str:
    s = str(raw or "").strip()
    if not s or s == "nan":
        return ""
    return _UNIT_NORM_MAP.get(s, s.upper())


def _epo_parse_float(raw: str) -> "float | None":
    s = str(raw or "").strip().replace(",", "")
    if not s or s == "nan":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _epo_parse_date(raw) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s or s == "nan":
        return ""
    return _pt_parse_excel_date(s) or s[:10]


def _epo_col(row, col_name: str) -> str:
    if col_name not in row.index:
        return ""
    v = str(row[col_name]).strip()
    return v if v not in ("nan", "None", "") else ""


_TYPE_CLEAN = {"Expent  cost": "Expent Cost", "CAPEX  (Budget)": "CAPEX Budget",
               "CAPEX  (Unbudget)": "CAPEX Unbudget"}


# Manual Thai → English glossary for common spare-part descriptions appearing in
# the Gen PO data. Used as a final fallback after the automatic translator so
# users always see English in the dashboard.
_EPO_THAI_GLOSSARY = {
    "กระสอบ": "Sack/Bag",
    "ข้อต่อตรง": "Straight connector",
    "แคล้มก้ามปูยึดท่อ": "Pipe clamp",
    "อุปกรณ์ทองแดง": "Copper fittings",
    "หลอดไฟฟลูออเรสเซนต์": "Fluorescent lamp",
    "เกลือล้างเรซิน": "Resin cleaning salt",
    "ถ้วยเซรามิก": "Ceramic cup/nozzle",
    "ลวดป้อนอาร์กอน": "Argon welding wire",
    "รีแพร์แคล้ม": "Repair clamp",
    "เคเบิ้ลไทร์": "Cable tie",
    "สายยางผ้าใบ": "Reinforced rubber hose",
}


_SERVICE_KEYWORDS = (
    "labour", "labor", "service", "transport", "cleaning", "civil",
    "annual", "admin", "safety", "ppe",
)


def _epo_apply_thai_glossary(text: str) -> str:
    """Substitute known Thai phrases with English equivalents."""
    if not text:
        return text
    out = text
    for thai, eng in _EPO_THAI_GLOSSARY.items():
        if thai in out:
            out = out.replace(thai, eng)
    return out


def _epo_classify_item(item_number: str, description: str, group_of_cost: str) -> str:
    """Classify a Gen PO line as Stock, Non-Stock, or Service.

    Rules:
      • Item number starts with SFST34            → Stock (inventory)
      • Item number starts with SFST81 / SFST82   → Non-Stock (externally bought)
      • Description / group contains service kws  → Service
      • Otherwise                                 → Other
    """
    code = (item_number or "").strip().upper()
    if code.startswith("SFST34"):
        return "Stock"
    if code.startswith("SFST81") or code.startswith("SFST82"):
        return "Non-Stock"
    haystack = f"{description or ''} {group_of_cost or ''}".lower()
    if any(kw in haystack for kw in _SERVICE_KEYWORDS):
        return "Service"
    return "Other"


def _epo_parse_int_days(raw) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if not s or s.lower() == "nan":
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def build_external_po_payload() -> dict:
    data_dir_path = Path(__file__).resolve().parent.parent / "data"
    future_paths, future_source_status = _resolve_future_sources(data_dir_path)
    _EPO_CACHE_V = 2
    try:
        current_mtime = (
            _EPO_CACHE_V,
            _stage_po_source_signatures(),
            *(_file_signature(path) for path in future_paths.values()),
            _file_signature(GEN_PO_SPARE_PARTS_PATH),
        )
        if _EPO_CACHE["result"] is not None and _EPO_CACHE["mtime"] == current_mtime:
            return _EPO_CACHE["result"]
    except OSError:
        current_mtime = None

    if current_mtime is not None:
        persistent_cached = _read_persistent_payload_cache("external_po", current_mtime)
        if persistent_cached is not None:
            _EPO_CACHE["result"] = persistent_cached
            _EPO_CACHE["mtime"] = current_mtime
            return persistent_cached

    spare_payload = build_spare_parts_payload()
    source_records = (spare_payload.get("po_classification") or {}).get("records") or []
    if not source_records:
        source_meta = future_source_status.get("po_list", {})
        return {
            "status": "missing",
            "error": source_meta.get("message") or "Gen PO file not uploaded",
            "records": [],
            "summary": {},
            "data_quality": [],
            "filters": {},
        }

    records: list[dict] = []
    for source in source_records:
        desc_raw = clean_text(source.get("original_description") or source.get("description"))
        desc_out = _epo_apply_thai_glossary(clean_text(source.get("translated_description") or source.get("description")))
        group_raw = clean_text(source.get("group_of_cost"))
        group_out = _epo_apply_thai_glossary(clean_text(source.get("translated_group_of_cost") or source.get("group_of_cost")))
        vendor_clean = clean_text(source.get("vendor_name") or source.get("supplier"))
        total_price_val = _clean_numeric(source.get("total_cost"))
        lead_time_days = _clean_numeric(source.get("lead_time_days"))
        actual_lead_days = _clean_numeric(source.get("actual_lead_days"))
        delay_days = _clean_numeric(source.get("delay_days"))
        delivery_flag = clean_text(source.get("delivery_flag")) or ("Pending" if not source.get("completed") else "Delivered")
        is_pending = delivery_flag == "Pending"
        has_thai_desc = bool(_THAI_RE.search(desc_raw))
        row_flags: list[str] = []
        if delivery_flag == "Delayed":
            row_flags.append("delayed")
        if is_pending:
            row_flags.append("pending")
        if lead_time_days is not None and lead_time_days > 60:
            row_flags.append("long_lead")
        if total_price_val is not None and total_price_val > 50000:
            row_flags.append("high_value")
        if has_thai_desc:
            row_flags.append("thai_desc")
        if source.get("used_po_date_as_gr_fallback"):
            row_flags.append("gr_date_fallback")
        records.append({
            "stage": clean_text(source.get("source_stage")) or "Unmapped Stage",
            "pr_no": clean_text(source.get("work_order_id")),
            "po_no": clean_text(source.get("po_number")),
            "grn_no": clean_text(source.get("grn_no")),
            "item_number": clean_text(source.get("code")),
            "description_raw": desc_raw,
            "description": desc_out,
            "has_thai": has_thai_desc,
            "group_of_cost_raw": group_raw,
            "group_of_cost": group_out,
            "pd_machine": clean_text(source.get("pd_machine")),
            "type_of_cost": _TYPE_CLEAN.get(clean_text(source.get("type_of_cost")), clean_text(source.get("type_of_cost"))),
            "qty": _clean_numeric(source.get("quantity_ordered")),
            "unit": _epo_norm_unit(clean_text(source.get("unit"))),
            "price_unit": _clean_numeric(source.get("unit_cost")),
            "total_price": total_price_val,
            "vendor": vendor_clean,
            "vendor_raw": vendor_clean,
            "status": clean_text(source.get("grn_status")),
            "date_pr": "",
            "date_po": _date_iso(source.get("po_date")),
            "date_grn": _date_iso(source.get("goods_received_date")),
            "effective_gr_date": _date_iso(source.get("goods_received_date")) or (_date_iso(source.get("po_date")) if source.get("used_po_date_as_gr_fallback") else ""),
            "lead_time": lead_time_days,
            "actual_lead": actual_lead_days,
            "kpi": clean_text(source.get("kpi_status")),
            "note": clean_text(source.get("note")),
            "classification": clean_text(source.get("purchase_classification") or source.get("classification")) or PURCHASE_CLASS_OTHER,
            "delivery_flag": delivery_flag,
            "delay_days": delay_days,
            "is_pending": is_pending,
            "row_flags": row_flags,
            "used_po_date_as_gr_fallback": bool(source.get("used_po_date_as_gr_fallback")),
        })

    total_spend = sum(r["total_price"] or 0 for r in records if r["total_price"] is not None)
    open_po_value = round(sum(r["total_price"] or 0 for r in records if r["delivery_flag"] == "Pending"), 2)
    spend_by_type: dict[str, float] = {}
    for r in records:
        t = r["type_of_cost"] or "Unclassified"
        spend_by_type[t] = spend_by_type.get(t, 0) + (r["total_price"] or 0)

    unique_pos = len({r["po_no"] for r in records if r["po_no"]})
    awaiting = sum(1 for r in records if r["delivery_flag"] == "Pending")
    kpi_rows = [r for r in records if r["kpi"]]
    over_kpi = sum(1 for r in kpi_rows if "over" in (r["kpi"] or "").lower())
    over_rate = round(over_kpi / len(kpi_rows) * 100, 1) if kpi_rows else 0

    quality_rules = [
        ("missing_item_number", "Item number missing", "Review description-based classification"),
        ("missing_total_price", "Total price missing", "Enter price / line amount in the source"),
        ("missing_vendor", "Vendor missing", "Assign the supplier or vendor"),
        ("missing_received_date", "Received row missing GR date", "Enter Date receive bill for completed deliveries"),
        ("gr_date_fallback", "GR date fell back to PO date", "Confirm the actual Date receive bill"),
        ("missing_po_date", "PO date missing", "Confirm Date Gen PO"),
        ("unclassified_rows", "Other / Unclassified rows", "Review purchase classification"),
    ]
    quality_by_rule: dict[str, list] = {rule_id: [] for rule_id, _, _ in quality_rules}
    for index, row in enumerate(records):
        if not row["item_number"]:
            quality_by_rule["missing_item_number"].append(index)
        if row["total_price"] is None:
            quality_by_rule["missing_total_price"].append(index)
        if row["po_no"] and not row["vendor"]:
            quality_by_rule["missing_vendor"].append(index)
        if row["delivery_flag"] != "Pending" and not row["date_grn"]:
            quality_by_rule["missing_received_date"].append(index)
        if row["used_po_date_as_gr_fallback"]:
            quality_by_rule["gr_date_fallback"].append(index)
        if not row["date_po"]:
            quality_by_rule["missing_po_date"].append(index)
        if row["classification"] == PURCHASE_CLASS_OTHER:
            quality_by_rule["unclassified_rows"].append(index)
    total_flagged = len({idx for idxs in quality_by_rule.values() for idx in idxs})
    data_quality = [
        {
            "rule": rule_id,
            "label": label,
            "action": action,
            "count": len(quality_by_rule[rule_id]),
            "row_indices": quality_by_rule[rule_id],
        }
        for rule_id, label, action in quality_rules
        if quality_by_rule[rule_id]
    ]

    delivered = [r for r in records if r["delivery_flag"] in ("Ontime", "Delayed")]
    on_time_rows = [r for r in delivered if r["delivery_flag"] == "Ontime"]
    delayed_rows = [r for r in delivered if r["delivery_flag"] == "Delayed"]
    pending_rows = [r for r in records if r["delivery_flag"] == "Pending"]
    on_time_rate = round(len(on_time_rows) / len(delivered) * 100, 1) if delivered else 0.0
    avg_delay = round(sum(r["delay_days"] for r in delayed_rows if r["delay_days"] is not None) / len([r for r in delayed_rows if r["delay_days"] is not None]), 1) if [r for r in delayed_rows if r["delay_days"] is not None] else 0.0
    completed_with_lead = [r for r in records if r["actual_lead"] is not None]
    avg_lead_time = round(sum(r["actual_lead"] for r in completed_with_lead) / len(completed_with_lead), 1) if completed_with_lead else None

    sup_agg: dict[str, dict] = defaultdict(lambda: {
        "total_pos": 0,
        "on_time": 0,
        "delayed": 0,
        "pending": 0,
        "delay_days_sum": 0.0,
        "delay_count": 0,
        "lead_days_sum": 0.0,
        "lead_count": 0,
        "spend": 0.0,
        "open_value": 0.0,
    })
    for r in records:
        vendor_key = r["vendor"] or "Unknown"
        bucket = sup_agg[vendor_key]
        bucket["total_pos"] += 1
        bucket["spend"] += r["total_price"] or 0
        if r["delivery_flag"] == "Ontime":
            bucket["on_time"] += 1
        elif r["delivery_flag"] == "Delayed":
            bucket["delayed"] += 1
            if r["delay_days"] is not None:
                bucket["delay_days_sum"] += r["delay_days"]
                bucket["delay_count"] += 1
        elif r["delivery_flag"] == "Pending":
            bucket["pending"] += 1
            bucket["open_value"] += r["total_price"] or 0
        if r["actual_lead"] is not None:
            bucket["lead_days_sum"] += r["actual_lead"]
            bucket["lead_count"] += 1

    supplier_performance = []
    for vendor, bucket in sup_agg.items():
        evaluated = bucket["on_time"] + bucket["delayed"]
        rate = round(bucket["on_time"] / evaluated * 100, 1) if evaluated else None
        avg_d = round(bucket["delay_days_sum"] / bucket["delay_count"], 1) if bucket["delay_count"] else 0.0
        avg_lead = round(bucket["lead_days_sum"] / bucket["lead_count"], 1) if bucket["lead_count"] else None
        supplier_performance.append({
            "vendor": vendor,
            "total_pos": bucket["total_pos"],
            "on_time": bucket["on_time"],
            "delayed": bucket["delayed"],
            "pending": bucket["pending"],
            "avg_delay": avg_d,
            "avg_lead_time": avg_lead,
            "on_time_rate": rate,
            "spend": round(bucket["spend"], 2),
            "open_value": round(bucket["open_value"], 2),
        })
    supplier_performance.sort(key=lambda row: (-row["spend"], row["vendor"]))

    cat_agg: dict[str, dict] = defaultdict(lambda: {"count": 0, "spend": 0.0, "on_time": 0, "delayed": 0})
    for r in records:
        bucket = cat_agg[r["classification"]]
        bucket["count"] += 1
        bucket["spend"] += r["total_price"] or 0
        if r["delivery_flag"] == "Ontime":
            bucket["on_time"] += 1
        elif r["delivery_flag"] == "Delayed":
            bucket["delayed"] += 1
    category_summary = []
    for cls in PURCHASE_CLASS_ORDER:
        if cls not in cat_agg:
            continue
        bucket = cat_agg[cls]
        evaluated = bucket["on_time"] + bucket["delayed"]
        rate = round(bucket["on_time"] / evaluated * 100, 1) if evaluated else None
        category_summary.append({
            "classification": cls,
            "count": bucket["count"],
            "spend": round(bucket["spend"], 2),
            "on_time_rate": rate,
        })

    services_by_group: dict[str, dict] = defaultdict(lambda: {"count": 0, "spend": 0.0, "on_time": 0, "delayed": 0})
    for r in records:
        if r["classification"] != PURCHASE_CLASS_SERVICE:
            continue
        group_key = r["group_of_cost"] or "Unclassified"
        bucket = services_by_group[group_key]
        bucket["count"] += 1
        bucket["spend"] += r["total_price"] or 0
        if r["delivery_flag"] == "Ontime":
            bucket["on_time"] += 1
        elif r["delivery_flag"] == "Delayed":
            bucket["delayed"] += 1
    service_groups = []
    for group_key, bucket in services_by_group.items():
        evaluated = bucket["on_time"] + bucket["delayed"]
        rate = round(bucket["on_time"] / evaluated * 100, 1) if evaluated else None
        service_groups.append({
            "group": group_key,
            "count": bucket["count"],
            "spend": round(bucket["spend"], 2),
            "on_time_rate": rate,
        })
    service_groups.sort(key=lambda row: (-row["spend"], row["group"]))

    monthly_spend: dict[str, float] = defaultdict(float)
    monthly_received: dict[str, float] = defaultdict(float)
    monthly_open: dict[str, float] = defaultdict(float)
    for r in records:
        if r["date_po"] and len(r["date_po"]) >= 7:
            monthly_spend[r["date_po"][:7]] += r["total_price"] or 0
            if r["delivery_flag"] == "Pending":
                monthly_open[r["date_po"][:7]] += r["total_price"] or 0
        effective_gr_date = r.get("effective_gr_date") or r.get("date_grn")
        if effective_gr_date and len(effective_gr_date) >= 7 and r["delivery_flag"] != "Pending":
            monthly_received[effective_gr_date[:7]] += r["total_price"] or 0
    monthly_trend = [{"month": key, "spend": round(value, 2)} for key, value in sorted(monthly_spend.items())]
    monthly_received_value = [{"month": key, "value": round(value, 2)} for key, value in sorted(monthly_received.items())]
    monthly_open_value = [{"month": key, "value": round(value, 2)} for key, value in sorted(monthly_open.items())]

    top_vendors = sorted(
        ({"vendor": vendor, "spend": round(bucket["spend"], 2)} for vendor, bucket in sup_agg.items()),
        key=lambda row: -row["spend"],
    )[:10]
    top_delayed_vendors = sorted(
        (
            {
                "vendor": vendor,
                "delayed": bucket["delayed"],
                "avg_delay": round(bucket["delay_days_sum"] / bucket["delay_count"], 1) if bucket["delay_count"] else 0.0,
                "open_value": round(bucket["open_value"], 2),
            }
            for vendor, bucket in sup_agg.items()
            if bucket["delayed"] or bucket["pending"]
        ),
        key=lambda row: (-row["delayed"], -row["avg_delay"], -row["open_value"]),
    )[:10]

    result = {
        "status": "ok",
        "records": records,
        "summary": {
            "total_spend": round(total_spend, 2),
            "open_po_value": open_po_value,
            "spend_by_type": sorted(
                [{"type": key, "total": round(value, 2)} for key, value in spend_by_type.items()],
                key=lambda row: -row["total"],
            ),
            "unique_pos": unique_pos,
            "total_rows": len(records),
            "awaiting_delivery": awaiting,
            "over_delivery_rate": over_rate,
            "total_flagged": total_flagged,
            "on_time_count": len(on_time_rows),
            "delayed_count": len(delayed_rows),
            "pending_count": len(pending_rows),
            "evaluated_count": len(delivered),
            "on_time_rate": on_time_rate,
            "avg_delay_days": avg_delay,
            "average_lead_time_days": avg_lead_time,
        },
        "supplier_performance": supplier_performance,
        "category_summary": category_summary,
        "service_groups": service_groups,
        "monthly_trend": monthly_trend,
        "monthly_received_value": monthly_received_value,
        "monthly_open_value": monthly_open_value,
        "top_vendors": top_vendors,
        "top_delayed_vendors": top_delayed_vendors,
        "data_quality": data_quality,
        "source_status": spare_payload.get("data_sources") or {},
        "filters": {
            "types_of_cost": sorted({r["type_of_cost"] for r in records if r["type_of_cost"]}),
            "groups_of_cost": sorted({r["group_of_cost"] for r in records if r["group_of_cost"]}),
            "statuses": sorted({r["status"] for r in records if r["status"]}),
            "vendors": sorted({r["vendor"] for r in records if r["vendor"]}),
            "machines": sorted({r["pd_machine"] for r in records if r["pd_machine"]}),
            "classifications": [classification for classification in PURCHASE_CLASS_ORDER if classification in {r["classification"] for r in records}],
            "delivery_flags": ["Ontime", "Delayed", "Delivered", "Pending"],
            "stages": sorted({r["stage"] for r in records if r["stage"]}),
        },
    }
    _EPO_CACHE["result"] = result
    _EPO_CACHE["mtime"] = current_mtime
    if current_mtime is not None:
        _write_persistent_payload_cache("external_po", current_mtime, result)
    return result


def _asset_intel_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _asset_intel_date_in_range(value, date_from=None, date_to=None) -> bool:
    date_value = _date_iso(value) or clean_text(value)
    if not date_value:
        return True
    if date_from and date_value[:10] < str(date_from)[:10]:
        return False
    if date_to and date_value[:10] > str(date_to)[:10]:
        return False
    return True


def _asset_intel_safe_float(value) -> float:
    try:
        if value is None or value == "":
            return 0.0
        numeric = float(value)
        if math.isnan(numeric):
            return 0.0
        return numeric
    except (TypeError, ValueError):
        return 0.0


def _asset_intel_unique(values) -> list[str]:
    return sorted({clean_text(value) for value in values if clean_text(value)})


def _asset_intel_slug(text: str) -> str:
    return _pt_slug(text)


def _asset_intel_text(row: dict, *keys: str) -> str:
    if not keys:
        keys = tuple(row.keys())
    return " ".join(clean_text(row.get(key)) for key in keys if clean_text(row.get(key)))


def _asset_intel_confidence_rank(value: str) -> int:
    return {"High": 3, "Medium": 2, "Low": 1}.get(clean_text(value), 0)


def _asset_intel_options(asset_catalog: dict, spare_payload: dict | None = None, pt_payload: dict | None = None) -> dict:
    spare_payload = spare_payload or {}
    pt_payload = pt_payload or {}
    asset_rows = asset_catalog.get("assets") or []
    po_rows = spare_payload.get("po_classification", {}).get("records") or []
    pt_filters = pt_payload.get("consumption_analysis", {}).get("filters") or {}
    return {
        "assets": [
            {
                "asset_id": row.get("asset_id"),
                "asset_name": row.get("name"),
                "asset_family": row.get("asset_family"),
                "machine_group": row.get("machine_group"),
            }
            for row in asset_rows[:500]
        ],
        "asset_families": _asset_intel_unique(
            [row.get("asset_family") for row in asset_catalog.get("family_rows") or []]
            or pt_filters.get("asset_families")
            or []
        ),
        "machine_groups": _asset_intel_unique(
            [row.get("machine_group") for row in asset_rows]
            + list(pt_filters.get("machine_groups") or [])
        ),
        "suppliers": _asset_intel_unique(row.get("vendor_name") or row.get("supplier") for row in po_rows),
    }


def _asset_intel_profile_aliases(profile: dict | None) -> list[str]:
    if not profile:
        return []
    aliases = []
    for alias in profile.get("aliases") or []:
        clean_alias = clean_text(alias)
        if clean_alias:
            aliases.append(clean_alias)
    return aliases[:30]


def _asset_intel_match_source_for_context(match: dict, context: str, record: dict | None = None) -> str:
    source = clean_text((match or {}).get("matchSource")) or "Description match"
    if context == "po":
        if source in {"Description match", "Translated description match", "Related keyword match"}:
            return "PO description match"
        if source == "Asset name match" and clean_text((record or {}).get("pd_machine")):
            return "PO asset / machine match"
    if context == "store":
        if source in {"Description match", "Translated description match"}:
            if clean_text((record or {}).get("wo_description") or (record or {}).get("wo_translated_description")):
                return "Related WO/MR match"
            return "Store transaction match"
    if context == "work_order" and source == "Related keyword match":
        return "Description match"
    if source == "Description match" and context == "work_order":
        return "Description match"
    if source == "Translated description match" and context == "work_order":
        return "Translated description match"
    return source


def _asset_intel_general_area_or_missing(asset_id: str) -> bool:
    return not clean_text(asset_id) or _pt_is_general_area_value(asset_id)


def _asset_intel_data_quality_flags(record: dict, match: dict | None, source: str, context: str) -> list[str]:
    flags: list[str] = []
    asset_id = clean_text(record.get("asset_id") or record.get("recorded_asset_id"))
    if not asset_id:
        flags.append("Missing asset ID")
    elif _pt_is_general_area_value(asset_id):
        flags.append("General area coded")
    if (match or {}).get("possibleAssetCodingMismatch") or record.get("possible_asset_coding_mismatch"):
        flags.append("Possible asset coding mismatch")
    if source in {"Description match", "Translated description match", "PO description match", "Store transaction match", "Related WO/MR match"}:
        flags.append("Found through description")
    if context == "store" and not clean_text(record.get("work_order_id") or record.get("wo_request_id")):
        flags.append("No linked WO/MR reference")
    if context == "po" and not clean_text(record.get("code")):
        flags.append("Missing part code")
    return list(dict.fromkeys(flags)) or ["Direct match"]


def _asset_intel_public_flag(flags: list[str]) -> str:
    public = [flag for flag in flags if flag and flag != "Direct match"]
    return "; ".join(public[:3]) if public else "Direct match"


def _asset_intel_status_group(status: str) -> str:
    text = normalize_spare_part_text(status)
    if any(token in text for token in ("finish", "finished", "confirm", "confirmed", "complete", "completed", "closed", "ended")):
        return "finished"
    if any(token in text for token in ("open", "progress", "started", "pending", "created", "active", "scheduled")):
        return "open"
    return "other"


def _asset_intel_part_key(*values) -> str:
    for value in values:
        text = normalize_spare_part_text(value)
        if text:
            return text
    return ""


def _asset_intel_build_target(
    query: str | None = None,
    asset_id: str | None = None,
    asset_name: str | None = None,
    asset_family: str | None = None,
    machine_group: str | None = None,
    asset_catalog: dict | None = None,
) -> dict:
    asset_catalog = asset_catalog or build_spare_part_asset_profiles()
    asset_lookup = asset_catalog.get("asset_lookup") or {}
    asset_profiles = asset_catalog.get("asset_profiles") or {}
    family_lookup = asset_catalog.get("family_lookup") or {}
    family_profiles = asset_catalog.get("family_profiles") or {}
    asset_rows = asset_catalog.get("assets") or []

    query_text = clean_text(query or asset_name or asset_id or asset_family or machine_group)
    asset_id_text = clean_text(asset_id)
    family_text = clean_text(asset_family)
    group_text = clean_text(machine_group)
    norm_query = normalize_spare_part_text(query_text)

    if asset_id_text:
        for row_id, row in asset_lookup.items():
            if normalize_spare_part_text(row_id) == normalize_spare_part_text(asset_id_text):
                profile = asset_profiles.get(row_id) or build_asset_profile(row)
                return {
                    "mode": "asset",
                    "query": query_text,
                    "query_norm": norm_query,
                    "profile": profile,
                    "asset_ids": {row_id},
                    "asset_rows": [row],
                    "family_profile": None,
                    "machine_group": row.get("machine_group") or "",
                }

    if family_text:
        family_norm = normalize_spare_part_text(family_text)
        for family_id, row in family_lookup.items():
            if family_norm in {
                normalize_spare_part_text(family_id),
                normalize_spare_part_text(row.get("asset_family")),
            }:
                included_ids = set(row.get("included_asset_ids") or [])
                return {
                    "mode": "family",
                    "query": family_text,
                    "query_norm": family_norm,
                    "profile": family_profiles.get(family_id),
                    "asset_ids": included_ids,
                    "asset_rows": [asset for asset in asset_rows if asset.get("asset_id") in included_ids],
                    "family_profile": family_profiles.get(family_id),
                    "machine_group": row.get("machine_group") or "",
                }

    if group_text:
        group_norm = normalize_spare_part_text(group_text)
        group_assets = [row for row in asset_rows if normalize_spare_part_text(row.get("machine_group")) == group_norm]
        return {
            "mode": "machine_group",
            "query": group_text,
            "query_norm": group_norm,
            "profile": build_asset_profile({"asset_id": _asset_intel_slug(group_text), "name": group_text, "machine_group": group_text}),
            "asset_ids": {row.get("asset_id") for row in group_assets if row.get("asset_id")},
            "asset_rows": group_assets,
            "family_profile": None,
            "machine_group": group_text,
        }

    if norm_query:
        exact_asset = next(
            (
                row
                for row in asset_rows
                if norm_query in {
                    normalize_spare_part_text(row.get("asset_id")),
                    normalize_spare_part_text(row.get("name")),
                }
            ),
            None,
        )
        if exact_asset:
            row_id = exact_asset.get("asset_id")
            return {
                "mode": "asset",
                "query": query_text,
                "query_norm": norm_query,
                "profile": asset_profiles.get(row_id) or build_asset_profile(exact_asset),
                "asset_ids": {row_id},
                "asset_rows": [exact_asset],
                "family_profile": None,
                "machine_group": exact_asset.get("machine_group") or "",
            }

        query_record = {
            "asset_id": query_text,
            "machine_name": query_text,
            "description_original": query_text,
            "translated_description": query_text,
        }
        asset_matches = match_record_to_asset_profiles(query_record, asset_profiles, {"include_related": True, "limit": 1})
        if asset_matches:
            matched_id = asset_matches[0].get("matchedAssetId")
            row = asset_lookup.get(matched_id)
            if row:
                return {
                    "mode": "asset",
                    "query": query_text,
                    "query_norm": norm_query,
                    "profile": asset_profiles.get(matched_id) or build_asset_profile(row),
                    "asset_ids": {matched_id},
                    "asset_rows": [row],
                    "family_profile": None,
                    "machine_group": row.get("machine_group") or "",
                }

        exact_family = next(
            (
                (family_id, row)
                for family_id, row in family_lookup.items()
                if (
                    norm_query in {
                        normalize_spare_part_text(row.get("asset_family")),
                        normalize_spare_part_text(family_id),
                    }
                    or (
                        len(norm_query) >= 4
                        and norm_query in normalize_spare_part_text(row.get("asset_family"))
                    )
                )
            ),
            None,
        )
        if exact_family:
            family_id, row = exact_family
            included_ids = set(row.get("included_asset_ids") or [])
            return {
                "mode": "family",
                "query": query_text,
                "query_norm": norm_query,
                "profile": family_profiles.get(family_id),
                "asset_ids": included_ids,
                "asset_rows": [asset for asset in asset_rows if asset.get("asset_id") in included_ids],
                "family_profile": family_profiles.get(family_id),
                "machine_group": row.get("machine_group") or "",
            }

        family_matches = match_record_to_asset_profiles(query_record, family_profiles, {"include_related": True, "limit": 1})
        if family_matches:
            family_id = family_matches[0].get("matchedAssetId")
            row = family_lookup.get(family_id)
            if row:
                included_ids = set(row.get("included_asset_ids") or [])
                return {
                    "mode": "family",
                    "query": query_text,
                    "query_norm": norm_query,
                    "profile": family_profiles.get(family_id),
                    "asset_ids": included_ids,
                    "asset_rows": [asset for asset in asset_rows if asset.get("asset_id") in included_ids],
                    "family_profile": family_profiles.get(family_id),
                    "machine_group": row.get("machine_group") or "",
                }

        group_match = next(
            (
                row.get("machine_group")
                for row in asset_rows
                if norm_query == normalize_spare_part_text(row.get("machine_group"))
                or (
                    len(norm_query) >= 4
                    and norm_query in normalize_spare_part_text(row.get("machine_group"))
                )
            ),
            None,
        )
        if group_match:
            group_assets = [row for row in asset_rows if row.get("machine_group") == group_match]
            return {
                "mode": "machine_group",
                "query": query_text,
                "query_norm": norm_query,
                "profile": build_asset_profile({"asset_id": _asset_intel_slug(group_match), "name": group_match, "machine_group": group_match}),
                "asset_ids": {row.get("asset_id") for row in group_assets if row.get("asset_id")},
                "asset_rows": group_assets,
                "family_profile": None,
                "machine_group": group_match,
            }

    return {
        "mode": "search" if query_text else "empty",
        "query": query_text,
        "query_norm": norm_query,
        "profile": build_asset_profile({"asset_id": "", "name": query_text}) if query_text else None,
        "asset_ids": set(),
        "asset_rows": [],
        "family_profile": None,
        "machine_group": "",
    }


def _asset_intel_selected_asset(target: dict) -> dict:
    mode = target.get("mode")
    asset_rows = target.get("asset_rows") or []
    first_asset = asset_rows[0] if asset_rows else {}
    profile = target.get("profile") or {}
    family_profile = target.get("family_profile") or {}
    family_name = clean_text(first_asset.get("asset_family") or family_profile.get("assetFamilyName"))
    if mode == "family":
        family_name = family_name or clean_text(target.get("query"))
    return {
        "assetId": clean_text(first_asset.get("asset_id") if mode == "asset" else ""),
        "assetName": clean_text(first_asset.get("name") if mode == "asset" else target.get("query")) or "Search required",
        "assetFamily": family_name,
        "machineGroup": clean_text(first_asset.get("machine_group") or target.get("machine_group")),
        "criticality": clean_text(first_asset.get("criticality")),
        "selectionMode": mode,
        "aliases": _asset_intel_profile_aliases(profile),
        "includedAssetCount": len(asset_rows),
        "includedAssets": [
            {"asset_id": row.get("asset_id"), "asset_name": row.get("name")}
            for row in asset_rows[:50]
        ],
    }


def _asset_intel_match_record(
    record: dict,
    target: dict,
    context: str,
    include_related_matches: bool = True,
    include_low_confidence: bool = False,
) -> dict | None:
    mode = target.get("mode")
    if mode == "empty":
        return None

    record_asset_id = clean_text(record.get("resolved_asset_id") or record.get("asset_id") or record.get("recorded_asset_id"))
    record_asset_norm = normalize_spare_part_text(record_asset_id)
    selected_asset_ids = {normalize_spare_part_text(asset_id) for asset_id in (target.get("asset_ids") or set()) if asset_id}
    query_norm = target.get("query_norm") or ""
    # Memoise the normalised search text on the (cached) record: normalising every
    # record's text was re-run on every query for thousands of rows — the main
    # remaining hot spot. The record objects are reused across queries, so this
    # runs once per record then is free.
    search_text = record.get("_asset_intel_search_norm")
    if search_text is None:
        search_text = normalize_spare_part_text(record.get("_asset_intel_search_text") or _asset_intel_text(record))
        if isinstance(record, dict):
            record["_asset_intel_search_norm"] = search_text

    if mode == "search":
        if not query_norm or query_norm not in search_text:
            return None
        confidence = "Medium" if len(query_norm.split()) > 1 or len(query_norm) >= 4 else "Low"
        if confidence == "Low" and not include_low_confidence:
            return None
        source = "Supplier match" if context == "po" and query_norm in normalize_spare_part_text(record.get("supplier") or record.get("vendor_name")) else (
            "PO description match" if context == "po" else "Store transaction match" if context == "store" else "Description match"
        )
        return {
            "matchSource": source,
            "confidence": confidence,
            "possibleAssetCodingMismatch": _asset_intel_general_area_or_missing(record.get("asset_id") or record.get("recorded_asset_id")),
        }

    if selected_asset_ids and record_asset_norm in selected_asset_ids:
        return {
            "matchSource": "Asset ID match",
            "confidence": "High",
            "possibleAssetCodingMismatch": False,
        }

    profile = target.get("profile")
    matches = []
    if profile:
        matches = match_record_to_asset_profiles(record, [profile], {"include_related": True, "limit": 1})

    if not matches and mode in {"family", "machine_group"}:
        asset_catalog = build_spare_part_asset_profiles()
        asset_profiles = asset_catalog.get("asset_profiles") or {}
        target_profiles = [
            asset_profiles[asset_id]
            for asset_id in (target.get("asset_ids") or set())
            if asset_id in asset_profiles
        ]
        matches = match_record_to_asset_profiles(record, target_profiles, {"include_related": True, "limit": 1})

    if matches:
        match = dict(matches[0])
        source = _asset_intel_match_source_for_context(match, context, record)
        if mode == "family" and source not in {"Asset ID match", "Asset name match"}:
            source = "Asset family match" if context != "po" else "PO description match"
        if mode == "machine_group" and source not in {"Asset ID match", "Asset name match"}:
            source = "Machine group match" if context != "po" else "PO description match"
        direct = source == "Asset ID match" or (source == "Asset name match" and match.get("confidence") == "High")
        if match.get("confidence") == "Low" and not include_low_confidence:
            return None
        if not include_related_matches and not direct:
            return None
        match["matchSource"] = source
        return match

    if mode == "machine_group":
        group_norm = normalize_spare_part_text(target.get("machine_group") or target.get("query"))
        row_group_norm = normalize_spare_part_text(record.get("machine_group") or record.get("equipment_type") or record.get("asset_family"))
        if group_norm and row_group_norm == group_norm:
            if not include_low_confidence:
                return None
            return {
                "matchSource": "Machine group match",
                "confidence": "Low",
                "possibleAssetCodingMismatch": _asset_intel_general_area_or_missing(record.get("asset_id") or record.get("recorded_asset_id")),
            }

    if query_norm and include_related_matches and query_norm in search_text:
        confidence = "Low"
        if confidence == "Low" and not include_low_confidence:
            return None
        return {
            "matchSource": "Description match" if context == "work_order" else "PO description match" if context == "po" else "Store transaction match",
            "confidence": confidence,
            "possibleAssetCodingMismatch": _asset_intel_general_area_or_missing(record.get("asset_id") or record.get("recorded_asset_id")),
        }

    return None


def _asset_intel_normalize_work_order(rec: dict) -> dict:
    wo_fields = _pt_extract_wo_fields(rec)
    asset_id = _pt_pick(rec, "asset_id", "Asset ID", "AssetId", "AssetID", "machine_code", "PD Machine")
    asset_name = (
        wo_fields.get("equipment_name")
        or _pt_pick(rec, "asset_name", "Asset Name", "machine_name", "MachineName", "Name", "Equipment")
        or asset_id
    )
    description = wo_fields.get("wo_description") or _pt_pick(rec, "Maintenance request", "Description", "description", "Notes", "Remarks")
    translated = wo_fields.get("wo_translated_description") or _pt_pick(rec, "Translated description", "TranslatedDescription")
    date_value = (
        wo_fields.get("wo_actual_start")
        or _pt_pick(rec, "Actual start", "Actual Start", "Created date", "Created Date", "created_date", "Request Date")
    )
    mr_number = _pt_pick(rec, "maintenance_request_id", "Maintenance request", "Maintenance Request", "MR number", "MR No.", "Request ID", "RequestId")
    wo_number = _pt_pick(rec, "maintenance_order_id", "Work order", "Work Order", "WorkOrderId", "WO number", "WO No.", "WO ID")
    status = _pt_pick(rec, "current_lifecycle_state", "Current lifecycle state", "Current Lifecycle State", "status", "Status")
    normalized = {
        "date": _date_iso(date_value) or clean_text(date_value),
        "mr_number": mr_number,
        "wo_number": wo_number,
        "asset_id": asset_id,
        "recorded_asset_id": asset_id,
        "asset_name": asset_name,
        "machine_name": asset_name,
        "equipment_name": asset_name,
        "functional_location": wo_fields.get("wo_location") or _pt_pick(rec, "Functional location", "Functional Location", "Location", "Area"),
        "description": description,
        "description_original": description,
        "translated_description": translated,
        "status": status,
        "maintenance_request_type": _pt_pick(rec, "Maintenance request type", "maintenance_request_type", "Request Type"),
        "service_level": wo_fields.get("wo_severity"),
        "actual_start": _date_iso(wo_fields.get("wo_actual_start")) or wo_fields.get("wo_actual_start"),
        "actual_end": _date_iso(wo_fields.get("wo_actual_end")) or wo_fields.get("wo_actual_end"),
    }
    normalized["_asset_intel_search_text"] = _asset_intel_text(
        normalized,
        "asset_id", "asset_name", "functional_location", "description", "translated_description", "mr_number", "wo_number", "status"
    )
    return normalized


# Normalising every WO record (~4,600) is pure-CPU but heavy (many alias lookups
# + regex), and it ran on every analysis — the ~21s hot spot. The records are
# deterministic and _pt_load_wo_records() is cached, so we normalise once and
# reuse, keyed on the cached records' identity (a new list = data changed).
_ASSET_INTEL_WO_NORM_CACHE = {"key": None, "records": None}


def _asset_intel_normalized_wo_records() -> list[dict]:
    recs = _pt_load_wo_records()
    key = id(recs)
    cache = _ASSET_INTEL_WO_NORM_CACHE
    if cache["key"] == key and cache["records"] is not None:
        return cache["records"]
    normalized = [_asset_intel_normalize_work_order(rec) for rec in recs]
    cache["key"] = key
    cache["records"] = normalized
    return normalized


def find_related_work_orders_for_asset(
    target: dict,
    date_from=None,
    date_to=None,
    include_related_matches=True,
    include_low_confidence=False,
) -> list[dict]:
    rows: list[dict] = []
    for row in _asset_intel_normalized_wo_records():
        if not _asset_intel_date_in_range(row.get("date") or row.get("actual_start"), date_from, date_to):
            continue
        match = _asset_intel_match_record(row, target, "work_order", include_related_matches, include_low_confidence)
        if not match:
            continue
        source = match.get("matchSource") or "Description match"
        flags = _asset_intel_data_quality_flags(row, match, source, "work_order")
        rows.append({
            "date": row.get("date") or row.get("actual_start"),
            "mr_number": row.get("mr_number"),
            "wo_number": row.get("wo_number"),
            "recorded_asset_id": row.get("asset_id"),
            "recorded_asset_name": row.get("asset_name") or row.get("functional_location"),
            "functional_location": row.get("functional_location"),
            "description": row.get("translated_description") or row.get("description"),
            "original_description": row.get("description"),
            "status": row.get("status") or "Unclassified",
            "match_source": source,
            "match_confidence": match.get("confidence") or "Low",
            "data_quality_flag": _asset_intel_public_flag(flags),
            "data_quality_flags": flags,
            "possible_asset_coding_mismatch": bool(match.get("possibleAssetCodingMismatch")),
            "is_direct_match": source == "Asset ID match",
        })
    return sorted(rows, key=lambda row: str(row.get("date") or ""), reverse=True)


def _asset_intel_store_match_record(row: dict) -> dict:
    record = dict(row)
    record["asset_id"] = clean_text(row.get("asset_id") or row.get("resolved_asset_id"))
    record["machine_name"] = clean_text(row.get("equipment_name") or row.get("resolved_asset_name") or row.get("asset_id"))
    record["description_original"] = clean_text(row.get("original_description") or row.get("clean_description") or row.get("part_name"))
    record["translated_description"] = clean_text(row.get("translated_description") or row.get("part_name"))
    record["remarks"] = _asset_intel_text(row, "project_id", "transaction_id", "line_property", "work_order_id", "supplier")
    record["_asset_intel_search_text"] = _asset_intel_text(
        row,
        "asset_id", "resolved_asset_id", "equipment_name", "resolved_asset_name", "asset_family", "machine_group",
        "part_code", "part_name", "original_description", "translated_description", "clean_description", "work_order_id",
        "wo_request_id", "wo_description", "wo_translated_description", "project_id", "transaction_id",
    )
    return record


# Same idea as the WO cache: normalise store/PO rows once and reuse (keyed on the
# cached source list's identity), instead of re-normalising thousands of rows on
# every analysis. Returns (source_row, normalized_match_record) pairs.
_ASSET_INTEL_STORE_NORM_CACHE = {"key": None, "pairs": None}
_ASSET_INTEL_PO_NORM_CACHE = {"key": None, "pairs": None}


def _asset_intel_normalized_store_rows():
    payload = build_project_transactions_payload()
    source_rows = payload.get("transactions") or payload.get("consumption_analysis", {}).get("records") or []
    cache = _ASSET_INTEL_STORE_NORM_CACHE
    key = id(source_rows)
    if cache["key"] == key and cache["pairs"] is not None:
        return cache["pairs"]
    pairs = [(s, _asset_intel_store_match_record(s)) for s in source_rows]
    cache["key"] = key
    cache["pairs"] = pairs
    return pairs


def _asset_intel_normalized_po_rows():
    source_rows = (build_spare_parts_payload().get("po_classification", {}) or {}).get("records") or []
    cache = _ASSET_INTEL_PO_NORM_CACHE
    key = id(source_rows)
    if cache["key"] == key and cache["pairs"] is not None:
        return cache["pairs"]
    pairs = [(s, _asset_intel_po_match_record(s)) for s in source_rows]
    cache["key"] = key
    cache["pairs"] = pairs
    return pairs


def find_spare_part_transactions_for_asset(
    target: dict,
    date_from=None,
    date_to=None,
    include_related_matches=True,
    include_low_confidence=False,
) -> list[dict]:
    rows: list[dict] = []
    for source, record in _asset_intel_normalized_store_rows():
        if not _asset_intel_date_in_range(source.get("project_date"), date_from, date_to):
            continue
        match = _asset_intel_match_record(record, target, "store", include_related_matches, include_low_confidence)
        if not match:
            continue
        source_label = match.get("matchSource") or source.get("match_source") or "Store transaction match"
        flags = _asset_intel_data_quality_flags(record, match, source_label, "store")
        rows.append({
            "date": source.get("project_date"),
            "item_code": source.get("part_code") or _pt_extract_part_code(source) or source.get("item_code"),
            "part_name": source.get("part_name") or source.get("translated_description") or source.get("clean_description") or source.get("original_description"),
            "quantity": source.get("quantity_used"),
            "value": source.get("total_consumption"),
            "recorded_asset_project": clean_text(source.get("asset_id") or source.get("project_id") or source.get("equipment_name")),
            "resolved_asset_id": source.get("resolved_asset_id"),
            "resolved_asset_name": source.get("resolved_asset_name"),
            "asset_family": source.get("asset_family"),
            "machine_group": source.get("machine_group"),
            "related_wo_mr": clean_text(source.get("work_order_id") or source.get("wo_request_id") or source.get("mr_wo_reference")),
            "transaction_id": source.get("transaction_id"),
            "match_source": source_label,
            "match_confidence": match.get("confidence") or source.get("match_confidence") or "Low",
            "data_quality_flag": _asset_intel_public_flag(flags),
            "data_quality_flags": flags,
            "possible_asset_coding_mismatch": bool(match.get("possibleAssetCodingMismatch") or source.get("possible_asset_coding_mismatch")),
            "is_direct_match": source_label == "Asset ID match",
        })
    return sorted(rows, key=lambda row: str(row.get("date") or ""), reverse=True)


def _asset_intel_po_match_record(row: dict) -> dict:
    record = {
        "asset_id": clean_text(row.get("asset_id") or row.get("pd_machine")),
        "machine_name": clean_text(row.get("pd_machine") or row.get("translated_pd_machine")),
        "description_original": _asset_intel_text(row, "original_description", "description", "clean_description", "code"),
        "translated_description": _asset_intel_text(row, "translated_description", "clean_description"),
        "code": clean_text(row.get("code")),
        "supplier": clean_text(row.get("supplier") or row.get("vendor_name")),
        "vendor_name": clean_text(row.get("vendor_name") or row.get("supplier")),
        "remarks": _asset_intel_text(
            row,
            "supplier", "vendor_name", "group_of_cost", "translated_group_of_cost", "classification", "classification_reason", "po_number",
        ),
        "pd_machine": row.get("pd_machine"),
        "_asset_intel_search_text": _asset_intel_text(
            row,
            "po_number", "code", "original_description", "description", "translated_description", "clean_description",
            "supplier", "vendor_name", "pd_machine", "translated_pd_machine", "group_of_cost", "translated_group_of_cost",
        ),
    }
    return record


def find_purchase_orders_for_asset(
    target: dict,
    date_from=None,
    date_to=None,
    include_related_matches=True,
    include_low_confidence=False,
) -> list[dict]:
    # Match EVERY PO row by alias/description (no spare-classification pre-filter:
    # many real rows — Robot Coupe / CL50 motors, OPTIBELT belts — are "Manual
    # Review"). Two stages so a whole purchase order is captured, not just the one
    # line whose text names the asset:
    #   1) line-level: a PO line directly matches the asset alias/description.
    #   2) PO-level propagation: every OTHER line under a PO number that had a
    #      matched line is included as a "Same PO asset-context match".
    candidates = []          # (source, record, po_date)
    direct_match = {}        # id(source) -> match dict
    matched_po_numbers = set()
    for source, record in _asset_intel_normalized_po_rows():
        po_date = source.get("po_date") or source.get("goods_received_date")
        if not _asset_intel_date_in_range(po_date, date_from, date_to):
            continue
        candidates.append((source, record, po_date))
        match = _asset_intel_match_record(record, target, "po", include_related_matches, include_low_confidence)
        if match:
            direct_match[id(source)] = match
            po_num = clean_text(source.get("po_number"))
            if po_num:
                matched_po_numbers.add(po_num)

    # For PO-level propagation, exclude lines that clearly belong to a DIFFERENT
    # specific asset (e.g. a shared cleaning PO that also lists "Bratt Pan No.1"
    # or "Combi Oven No.2-3" — those lines are not this asset's history). Only the
    # lines on already-matched POs are checked, so this stays cheap.
    _all_profiles = list((build_spare_part_asset_profiles().get("asset_profiles") or {}).values())
    _selected_ids = {normalize_spare_part_text(a) for a in (target.get("asset_ids") or set()) if a}

    rows: list[dict] = []
    for source, record, po_date in candidates:
        match = direct_match.get(id(source))
        po_num = clean_text(source.get("po_number"))
        if match:
            source_label = match.get("matchSource") or "PO description match"
        elif po_num and po_num in matched_po_numbers:
            # Another line on this PO matched the asset. Include this line too —
            # unless it clearly names a different specific asset (number-aware).
            other = match_record_to_asset_profiles(record, _all_profiles, {"include_related": False, "limit": 1}) if _all_profiles else []
            if other:
                other_id = normalize_spare_part_text(other[0].get("matchedAssetId") or "")
                if other_id and other_id not in _selected_ids:
                    continue
            match = {
                "confidence": "Medium",
                "possibleAssetCodingMismatch": _asset_intel_general_area_or_missing(
                    source.get("asset_id") or source.get("pd_machine")),
            }
            source_label = "Same PO asset-context match"
        else:
            continue
        flags = _asset_intel_data_quality_flags(record, match, source_label, "po")
        supplier = clean_text(source.get("vendor_name") or source.get("supplier")) or "Unmatched"
        part_description = clean_text(source.get("translated_description") or source.get("clean_description") or source.get("original_description") or source.get("description"))
        rows.append({
            "po_date": po_date,
            "po_number": source.get("po_number"),
            "supplier": supplier,
            "part_description": part_description,
            "item_code": source.get("code"),
            "quantity": source.get("quantity_ordered") or source.get("quantity_received"),
            "value": source.get("total_cost"),
            "related_asset_alias": clean_text(source.get("pd_machine") or source.get("asset_id") or target.get("query")),
            "classification": source.get("classification"),
            "match_source": source_label,
            "match_confidence": match.get("confidence") or "Low",
            "data_quality_flag": _asset_intel_public_flag(flags),
            "data_quality_flags": flags,
            "possible_asset_coding_mismatch": bool(match.get("possibleAssetCodingMismatch")),
            "is_direct_match": source_label in ("Asset ID match", "PO description match", "Supplier match"),
            "is_same_po_match": source_label == "Same PO asset-context match",
        })
    return sorted(rows, key=lambda row: str(row.get("po_date") or ""), reverse=True)


def aggregate_suppliers_for_asset(purchase_parts: list[dict]) -> list[dict]:
    buckets: dict[str, dict] = {}
    for row in purchase_parts:
        supplier = clean_text(row.get("supplier")) or "Unmatched"
        bucket = buckets.setdefault(
            supplier,
            {
                "supplier": supplier,
                "parts": Counter(),
                "total_po_value": 0.0,
                "po_line_count": 0,
                "latest_purchase_date": None,
            },
        )
        part = clean_text(row.get("part_description") or row.get("item_code")) or "Unmatched item"
        bucket["parts"][part] += 1
        bucket["total_po_value"] += _asset_intel_safe_float(row.get("value"))
        bucket["po_line_count"] += 1
        if row.get("po_date") and (bucket["latest_purchase_date"] is None or row["po_date"] > bucket["latest_purchase_date"]):
            bucket["latest_purchase_date"] = row["po_date"]

    summary = []
    for bucket in buckets.values():
        summary.append({
            "supplier": bucket["supplier"],
            "parts_supplied": [part for part, _ in bucket["parts"].most_common(5)],
            "parts_supplied_text": "; ".join(part for part, _ in bucket["parts"].most_common(5)),
            "total_po_value": round(bucket["total_po_value"], 2),
            "po_line_count": bucket["po_line_count"],
            "latest_purchase_date": bucket["latest_purchase_date"],
        })
    return sorted(summary, key=lambda row: (-row["total_po_value"], row["supplier"]))


def find_suppliers_for_asset(purchase_parts: list[dict]) -> list[dict]:
    return aggregate_suppliers_for_asset(purchase_parts)


def calculate_asset_parts_data_confidence(related_work_orders: list[dict], spare_parts_used: list[dict], purchase_parts: list[dict]) -> dict:
    rows = [*related_work_orders, *spare_parts_used, *purchase_parts]
    if not rows:
        return {"label": "Low", "score": 0, "basis": "No related records found"}
    counts = Counter(row.get("match_confidence") or "Low" for row in rows)
    weighted = counts["High"] * 100 + counts["Medium"] * 65 + counts["Low"] * 35
    score = round(weighted / max(1, len(rows)))
    if score >= 78:
        label = "High"
    elif score >= 50:
        label = "Medium"
    else:
        label = "Low"
    return {
        "label": label,
        "score": score,
        "basis": f"{counts['High']} high, {counts['Medium']} medium, {counts['Low']} low-confidence matches",
    }


def build_asset_parts_summary(
    related_work_orders: list[dict],
    spare_parts_used: list[dict],
    purchase_parts: list[dict],
    suppliers: list[dict],
) -> dict:
    wo_status_counts = Counter(_asset_intel_status_group(row.get("status")) for row in related_work_orders)
    used_part_counter = Counter()
    used_part_values = Counter()
    for row in spare_parts_used:
        key = clean_text(row.get("part_name") or row.get("item_code")) or "Unknown Part"
        used_part_counter[key] += _asset_intel_safe_float(row.get("quantity"))
        used_part_values[key] += _asset_intel_safe_float(row.get("value"))
    top_used_part = ""
    if used_part_values:
        top_used_part = max(used_part_values, key=lambda key: (used_part_values[key], used_part_counter[key], key))

    purchase_parts_unique = {
        _asset_intel_part_key(row.get("item_code"), row.get("part_description"))
        for row in purchase_parts
        if _asset_intel_part_key(row.get("item_code"), row.get("part_description"))
    }
    store_part_keys = {
        _asset_intel_part_key(row.get("item_code"), row.get("part_name"))
        for row in spare_parts_used
        if _asset_intel_part_key(row.get("item_code"), row.get("part_name"))
    }
    purchase_only_count = sum(
        1
        for row in purchase_parts
        if _asset_intel_part_key(row.get("item_code"), row.get("part_description"))
        and _asset_intel_part_key(row.get("item_code"), row.get("part_description")) not in store_part_keys
    )
    latest_supplier = next((row.get("supplier") for row in sorted(purchase_parts, key=lambda item: str(item.get("po_date") or ""), reverse=True) if row.get("supplier")), "")
    confidence = calculate_asset_parts_data_confidence(related_work_orders, spare_parts_used, purchase_parts)
    all_rows = [*related_work_orders, *spare_parts_used, *purchase_parts]
    direct_wo = sum(1 for row in related_work_orders if row.get("match_source") == "Asset ID match")
    description_wo = len(related_work_orders) - direct_wo
    return {
        "relatedWorkOrderCount": len(related_work_orders),
        "directWorkOrderMatches": direct_wo,
        "descriptionWorkOrderMatches": description_wo,
        "openInProgressWorkOrders": wo_status_counts["open"],
        "finishedConfirmedWorkOrders": wo_status_counts["finished"],
        "sparePartTransactionCount": len(spare_parts_used),
        "totalSparePartQuantity": round(sum(_asset_intel_safe_float(row.get("quantity")) for row in spare_parts_used), 3),
        "totalSparePartValue": round(sum(_asset_intel_safe_float(row.get("value")) for row in spare_parts_used), 2),
        "uniqueSpareParts": len(store_part_keys),
        "topUsedPart": top_used_part,
        "purchaseLineCount": len(purchase_parts),
        "totalPurchaseValue": round(sum(_asset_intel_safe_float(row.get("value")) for row in purchase_parts), 2),
        "uniquePurchasedParts": len(purchase_parts_unique),
        "latestPurchaseDate": max((row.get("po_date") for row in purchase_parts if row.get("po_date")), default=""),
        "supplierCount": len(suppliers),
        "mainSupplier": suppliers[0].get("supplier") if suppliers else "",
        "latestSupplierUsed": latest_supplier,
        "supplierListAvailable": bool(suppliers),
        "possibleCodingMismatchCount": sum(1 for row in all_rows if row.get("possible_asset_coding_mismatch")),
        "missingAssetIdRecords": sum(1 for row in all_rows if "Missing asset ID" in (row.get("data_quality_flags") or [])),
        "descriptionOnlyRecords": sum(1 for row in all_rows if "Found through description" in (row.get("data_quality_flags") or [])),
        "poOnlyPartRecords": purchase_only_count,
        "confidence": confidence["label"],
        "confidenceScore": confidence["score"],
        "confidenceBasis": confidence["basis"],
    }


def _asset_intel_data_gaps(summary: dict, related_work_orders: list[dict], spare_parts_used: list[dict], purchase_parts: list[dict]) -> list[str]:
    gaps: list[str] = []
    if related_work_orders and not spare_parts_used:
        gaps.append("WO/MR records found, but no matching store consumption found.")
    if purchase_parts and not spare_parts_used:
        gaps.append("Purchase records found in Gen PO, but no actual store issue transaction found.")
    if spare_parts_used and any(not clean_text(row.get("related_wo_mr")) for row in spare_parts_used):
        gaps.append("Spare part usage found, but no linked WO/MR reference available.")
    if summary.get("directWorkOrderMatches", 0) == 0 and related_work_orders:
        gaps.append("No direct asset ID work orders found.")
    if summary.get("descriptionOnlyRecords", 0):
        gaps.append("Records were found through description matching, not direct Asset ID.")
    if summary.get("poOnlyPartRecords", 0):
        gaps.append("Some purchase records were found in Gen PO but not in actual store issue transactions.")
    if not related_work_orders and not spare_parts_used and not purchase_parts:
        gaps.append("No related WO/MR, store consumption, or Gen PO records were found for the current search.")
    return gaps


def build_asset_parts_intelligence_context(
    query: str | None = None,
    asset_id: str | None = None,
    asset_name: str | None = None,
    asset_family: str | None = None,
    machine_group: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    include_related_matches=True,
    include_low_confidence=False,
) -> dict:
    asset_catalog = build_spare_part_asset_profiles()
    spare_payload = build_spare_parts_payload()
    pt_payload = build_project_transactions_payload()
    target = _asset_intel_build_target(query, asset_id, asset_name, asset_family, machine_group, asset_catalog)
    options = _asset_intel_options(asset_catalog, spare_payload, pt_payload)
    include_related = _asset_intel_bool(include_related_matches, True)
    include_low = _asset_intel_bool(include_low_confidence, False)

    if target.get("mode") == "empty":
        return {
            "status": "ready",
            "selectedAsset": _asset_intel_selected_asset(target),
            "relatedWorkOrders": [],
            "sparePartsUsed": [],
            "purchaseParts": [],
            "suppliers": [],
            "supplierSummary": [],
            "summary": build_asset_parts_summary([], [], [], []),
            "dataGaps": ["Search for an asset, family, machine group, part, or supplier to analyse related records."],
            "options": options,
            "meta": {"readOnly": True, "rowLimit": 300, "truncated": {}},
        }

    related_work_orders = find_related_work_orders_for_asset(target, date_from, date_to, include_related, include_low)
    spare_parts_used = find_spare_part_transactions_for_asset(target, date_from, date_to, include_related, include_low)
    purchase_parts = find_purchase_orders_for_asset(target, date_from, date_to, include_related, include_low)
    supplier_summary = aggregate_suppliers_for_asset(purchase_parts)
    summary = build_asset_parts_summary(related_work_orders, spare_parts_used, purchase_parts, supplier_summary)
    row_limit = 300
    return {
        "status": "ok",
        "selectedAsset": _asset_intel_selected_asset(target),
        "relatedWorkOrders": related_work_orders[:row_limit],
        "sparePartsUsed": spare_parts_used[:row_limit],
        "purchaseParts": purchase_parts[:row_limit],
        "suppliers": supplier_summary,
        "supplierSummary": supplier_summary,
        "summary": summary,
        "dataGaps": _asset_intel_data_gaps(summary, related_work_orders, spare_parts_used, purchase_parts),
        "options": options,
        "meta": {
            "readOnly": True,
            "rowLimit": row_limit,
            "truncated": {
                "relatedWorkOrders": len(related_work_orders) > row_limit,
                "sparePartsUsed": len(spare_parts_used) > row_limit,
                "purchaseParts": len(purchase_parts) > row_limit,
            },
            "filters": {
                "query": query,
                "assetId": asset_id,
                "assetName": asset_name,
                "assetFamily": asset_family,
                "machineGroup": machine_group,
                "dateFrom": date_from,
                "dateTo": date_to,
                "includeRelatedMatches": include_related,
                "includeLowConfidence": include_low,
            },
        },
    }


def _clear_spare_related_caches():
    _SPARE_PARTS_CACHE.clear()
    _PT_CACHE["result"] = None
    _PT_CACHE["mtime"] = None
    _AY_CACHE["result"] = None
    _AY_CACHE["mtime"] = None
    _EPO_CACHE["result"] = None
    _EPO_CACHE["mtime"] = None
    _clear_persistent_payload_cache()


def _stage_uploaded_file(file_storage, fallback_stem: str) -> Path:
    filename = os.path.basename(getattr(file_storage, "filename", "") or "")
    extension = Path(filename).suffix.lower()
    if extension not in SPARE_IMPORT_EXTENSIONS:
        raise ValueError("Unsupported file type. Upload a CSV, XLSX, or XLS export.")
    temp_dir = DEFAULT_DATA_DIR / "_upload_tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = _spare_import_safe_stem(filename, fallback_stem)
    temp_path = temp_dir / f"{safe_stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{extension}"
    file_storage.save(temp_path)
    return temp_path


def _remove_canonical_variants(stem: str):
    for suffix in (".xlsx", ".xls", ".csv"):
        candidate = DEFAULT_DATA_DIR / f"{stem}{suffix}"
        if candidate.exists():
            try:
                candidate.unlink()
            except OSError:
                pass


def _promote_uploaded_file(temp_path: Path, canonical_base: Path, archive_dir: Path | None = None, archive_stem: str | None = None) -> Path:
    _remove_canonical_variants(canonical_base.stem)
    final_path = canonical_base.with_suffix(temp_path.suffix.lower())
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.replace(final_path)
    if archive_dir is not None:
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_name = f"{archive_stem or canonical_base.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{final_path.suffix.lower()}"
        shutil.copy2(final_path, archive_dir / archive_name)
    return final_path


def _inventory_validation_summary(records):
    flagged_rows = sum(1 for row in records if row.get("data_quality_flags"))
    duplicate_rows = sum(1 for row in records if "Duplicate Part Number" in (row.get("data_quality_flags") or []))
    return {
        "rows": len(records),
        "flagged_rows": flagged_rows,
        "duplicate_rows": duplicate_rows,
        "missing_minimum_rows": sum(1 for row in records if row.get("min_stock") is None),
        "missing_maximum_rows": sum(1 for row in records if row.get("max_stock") is None),
        "inventory_value_available": any(row.get("stock_value") is not None for row in records),
    }


def _external_po_validation_summary(records):
    return {
        "rows": len(records),
        "manual_review_rows": sum(1 for row in records if row.get("classification") == PURCHASE_CLASS_OTHER),
        "missing_total_price_rows": sum(1 for row in records if row.get("total_cost") is None),
        "missing_item_code_rows": sum(1 for row in records if not clean_text(row.get("code"))),
        "translation_failed_rows": sum(1 for row in records if row.get("translation_status") == "Translation failed"),
        "stocked_matches": sum(1 for row in records if row.get("classification") == PURCHASE_CLASS_STOCK),
    }


def _project_transactions_validation_summary(payload):
    transactions = payload.get("transactions") or []
    years = sorted({str(row.get("project_date", ""))[:4] for row in transactions if re.fullmatch(r"\d{4}", str(row.get("project_date", ""))[:4])})
    return {
        "rows": len(transactions),
        "years": years,
        "manual_review_rows": len(payload.get("manual_review") or []),
        "linked_rows": sum(1 for row in transactions if row.get("link_status") == "Linked"),
        "translation_failed_rows": sum(1 for row in transactions if row.get("translation_status") == "Translation failed"),
    }


def import_spare_inventory_file(file_storage):
    temp_path = None
    try:
        temp_path = _stage_uploaded_file(file_storage, "spare_parts_master")
        # Quick structural check only — full classification runs on the next GET.
        try:
            df = pd.read_excel(str(temp_path), sheet_name=0)
        except Exception as exc:
            raise ValueError(f"Could not read inventory file: {exc}") from exc
        if df.empty:
            raise ValueError("No recognizable inventory rows were found in the uploaded template.")
        row_count = len(df)
        final_path = _promote_uploaded_file(temp_path, SPARE_IMPORT_CANONICAL_FILES["inventory"])
        _clear_spare_related_caches()
        request_spare_db_sync()
        return {
            "ok": True,
            "message": f"Imported {row_count} inventory row(s).",
            "file": final_path.name,
        }
    except Exception as exc:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        return {"ok": False, "message": f"Inventory file could not be imported: {exc}"}


def import_external_po_file(file_storage):
    temp_path = None
    try:
        temp_path = _stage_uploaded_file(file_storage, "po_list")
        # Quick structural check only — skip cross-referencing other source files.
        # Full reconciliation runs on the next GET request from the disk cache.
        try:
            df = pd.read_excel(str(temp_path), sheet_name=0)
        except Exception as exc:
            raise ValueError(f"Could not read Gen PO file: {exc}") from exc
        if df.empty:
            raise ValueError("No recognizable Gen PO rows were found in the uploaded template.")
        row_count = len(df)
        final_path = _promote_uploaded_file(temp_path, SPARE_IMPORT_CANONICAL_FILES["external_po"])
        _clear_spare_related_caches()
        request_spare_db_sync()
        return {
            "ok": True,
            "message": f"Imported {row_count} Gen PO row(s).",
            "file": final_path.name,
        }
    except Exception as exc:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        return {"ok": False, "message": f"Gen PO file could not be imported: {exc}"}


def _quick_validate_project_transactions(path: Path) -> None:
    """Fast structural check — just verify the file is readable and contains Item rows.
    Does NOT run asset classification so it completes in under a second."""
    try:
        raw = _read_project_transactions_source_frame(path)
    except Exception as exc:
        raise ValueError(f"Could not read file: {exc}") from exc
    if raw is None or raw.empty:
        raise ValueError("The file appears to be empty.")
    item_col = _COL_TTYPE
    if item_col < raw.shape[1]:
        has_item = (raw.iloc[:, item_col].astype(str).str.strip().str.lower() == "item").any()
    else:
        has_item = False
    if not has_item:
        raise ValueError("No 'Item' transaction rows found. Check you uploaded the correct Project actual transactions export.")


def _path_mtime_iso(path: Path | None) -> str | None:
    if not path:
        return None
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return None


def get_project_transactions_import_status() -> dict:
    """Lightweight file-based status; never parses the full consumption dataset."""
    path = _resolve_project_transactions_source_path()
    if not path:
        return {
            "uploaded": False,
            "file_name": None,
            "imported_at": None,
            "transaction_count": None,
            "message": "Project transactions file not uploaded.",
        }

    transaction_count = None
    try:
        current_sig = (_PT_CACHE.get("mtime") or (None,))[1] if isinstance(_PT_CACHE.get("mtime"), tuple) else None
        cached_sig = _file_signature(path)
        cached_payload = _PT_CACHE.get("result")
        if current_sig == cached_sig and isinstance(cached_payload, dict):
            transaction_count = len(cached_payload.get("transactions") or [])
    except Exception:
        transaction_count = None

    return {
        "uploaded": True,
        "file_name": path.name,
        "imported_at": _path_mtime_iso(path),
        "transaction_count": transaction_count,
        "message": f"Using {path.name}",
    }


def import_project_transactions_file(file_storage):
    temp_path = None
    try:
        temp_path = _stage_uploaded_file(file_storage, "project_transactions_current")
        # Single read: validate structure + count rows without running asset
        # classification or translation. Full payload build happens on the next
        # GET request and is stored in the disk cache.
        raw = _read_project_transactions_source_frame(temp_path)
        if raw is None or raw.empty:
            raise ValueError("The file appears to be empty.")
        if _COL_TTYPE < raw.shape[1]:
            item_mask = raw.iloc[:, _COL_TTYPE].astype(str).str.strip().str.lower() == "item"
            if not item_mask.any():
                raise ValueError("No 'Item' transaction rows found. Check you uploaded the correct Project actual transactions export.")
            row_count = int(item_mask.sum())
        else:
            row_count = len(raw)
        final_path = _promote_uploaded_file(
            temp_path,
            SPARE_IMPORT_CANONICAL_FILES["project_transactions"],
            archive_dir=PROJECT_TRANSACTIONS_IMPORT_DIR,
            archive_stem=_spare_import_safe_stem(getattr(file_storage, "filename", "") or "project_transactions_current", "project_transactions_current"),
        )
        _clear_spare_related_caches()
        request_spare_db_sync()
        return {
            "ok": True,
            "message": f"Imported {row_count} spare-parts consumption row(s).",
            "file": final_path.name,
        }
    except Exception as exc:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        return {"ok": False, "message": f"Project transactions file could not be imported: {exc}"}


def get_maintenance_import_status():
    _, future_source_status = _resolve_future_sources(DEFAULT_DATA_DIR)
    stage_rows, stage_status = _load_stage_goods_received_rows()

    from downtime_service import get_work_order_import_status  # local import to avoid heavier module work at import time

    work_order_status = get_work_order_import_status()
    project_path = _resolve_project_transactions_source_path()
    annual_import_paths = _project_transactions_import_history_paths()
    all_years_available = CSV_ALL_YEARS_PATH.exists() or bool(annual_import_paths)

    def _future_source(key, label, template):
        source = future_source_status.get(key, {})
        return {
            "label": label,
            "template": template,
            "available": bool(source.get("available")),
            "uploaded": bool(source.get("uploaded")),
            "using_fallback": bool(source.get("using_fallback")),
            "file_name": source.get("file_name"),
            "message": source.get("message") or ("File loaded" if source.get("available") else "File not loaded"),
            "validation": {},
        }

    sources = {
        "inventory": _future_source("spare_parts_master", "Inventory", "Item list for spare parts / Dynamics inventory export"),
        "external_po": _future_source("po_list", "External Parts", "Gen PO in D365 Rev.01 export"),
        "project_transactions": {
            "label": "Spare Parts Consumption",
            "template": "Project annual transactions export",
            "available": bool(project_path),
            "file_name": project_path.name if project_path else None,
            "message": f"Using {project_path.name}" if project_path else "Project transactions file not uploaded",
            "validation": {},
        },
        "work_orders": {
            "label": "Downtime Work Orders",
            "template": "Downtime work order export",
            "available": bool(work_order_status.get("using_uploaded_imports")),
            "file_name": (work_order_status.get("sources") or [{}])[0].get("name"),
            "message": "Work order import loaded" if work_order_status.get("using_uploaded_imports") else "Work order import not loaded",
            "validation": {"source_count": work_order_status.get("source_count", 0)},
        },
        "all_years_history": {
            "label": "All Years History",
            "template": "Multi-year project transactions history",
            "available": all_years_available,
            "file_name": CSV_ALL_YEARS_PATH.name if CSV_ALL_YEARS_PATH.exists() else (annual_import_paths[-1].name if annual_import_paths else None),
            "message": "Historical analysis ready" if all_years_available else "All-years history not available",
            "validation": {},
        },
    }

    stage_files = [status.get("file_name") for status in stage_status.values() if status.get("available") and status.get("file_name")]
    if stage_rows and stage_files:
        sources["external_po"].update({
            "available": True,
            "uploaded": True,
            "file_name": ", ".join(stage_files),
            "message": f"Using stage-aware Gen PO sources: {', '.join(stage_files)}",
            "validation": {"rows": len(stage_rows), "source_count": len(stage_files)},
        })

    flags = []
    if not sources["inventory"].get("available"):
        flags.append({"level": "error", "title": "Inventory missing", "message": "Upload the inventory export to populate current stock KPIs and comparison cards."})
    if not sources["external_po"].get("available"):
        flags.append({"level": "error", "title": "Gen PO missing", "message": "Upload the external parts export to populate PO classification, spend, and vendor cards."})
    if not sources["project_transactions"].get("available"):
        flags.append({"level": "warning", "title": "Consumption import missing", "message": "Spare-parts consumption tables and annual analysis need a Project annual transactions export."})
    if not sources["work_orders"].get("available"):
        flags.append({"level": "warning", "title": "Downtime work orders missing", "message": "Spare-parts consumption to work-order linking is limited until the Downtime work order import is loaded."})
    years = sources["all_years_history"].get("validation", {}).get("years") or []
    if years and len(years) == 1:
        flags.append({"level": "info", "title": "Single-year analysis", "message": f"Only {years[0]} is currently available in all-years analysis. Future annual imports will be kept in history for comparison."})

    return {"sources": sources, "flags": flags}
