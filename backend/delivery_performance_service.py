"""Supplier Delivery Performance — calculated from Gen PO only.

GRN-PO date (Day) logic:
  >= 0  → received; actual delivery days = that value;
          delay = max(0, actual_days - lead_time); on-time or late
  < 0 or blank/None → open/not received;
          days_waiting = today - Date Gen PO;
          delay = max(0, days_waiting - lead_time); open not-due or overdue
  Missing Date Gen PO or Lead time → "Data issue" (excluded from on-time denominator)
"""
from __future__ import annotations

import datetime
from spare_parts_views import get_goods_received_rows


def _today() -> datetime.date:
    return datetime.date.today()


def _parse_date(val) -> datetime.date | None:
    if not val:
        return None
    try:
        return datetime.date.fromisoformat(str(val)[:10])
    except (ValueError, TypeError):
        return None


def _classify_row(row: dict, today: datetime.date) -> dict:
    date_gen_po = _parse_date(row.get("date_gen_po"))
    lead_time = row.get("lead_time_days")
    grn_po_days = row.get("grn_po_days")

    if date_gen_po is None or lead_time is None:
        return {
            "status_category": "data_issue",
            "status": "Data issue",
            "received": False,
            "actual_days": None,
            "days_waiting": None,
            "delay_days": None,
        }

    if grn_po_days is not None and float(grn_po_days) >= 0:
        actual_days = float(grn_po_days)
        delay = max(0.0, actual_days - float(lead_time))
        on_time = delay == 0
        return {
            "status_category": "received_ontime" if on_time else "received_late",
            "status": "Received on time" if on_time else "Received late",
            "received": True,
            "actual_days": actual_days,
            "days_waiting": None,
            "delay_days": delay,
        }

    days_waiting = (today - date_gen_po).days
    delay = max(0.0, float(days_waiting) - float(lead_time))
    overdue = delay > 0
    return {
        "status_category": "open_overdue" if overdue else "open_notdue",
        "status": "Open overdue" if overdue else "Open not due",
        "received": False,
        "actual_days": None,
        "days_waiting": days_waiting,
        "delay_days": delay,
    }


def _apply_filters(rows: list[dict], stage, category, year, month, financial_view) -> list[dict]:
    result = []
    for row in rows:
        if stage and stage not in (None, "all") and row.get("stage") != stage:
            continue
        if category and category not in (None, "all") and row.get("category") != category:
            continue
        if year and year not in (None, "all") and row.get("year") != year:
            continue
        if month and month not in (None, "all") and row.get("month") != month:
            continue
        ft = row.get("financial_type", "")
        if financial_view == "engineering_opex" and ft != "OPEX":
            continue
        if financial_view == "engineering_capex" and ft != "CAPEX":
            continue
        result.append(row)
    return result


def _vendor_status(on_time_pct, overdue_open: int, deliverable_count: int) -> str:
    if deliverable_count == 0:
        return "Data Issue"
    if on_time_pct is None:
        return "Data Issue"
    if on_time_pct >= 90 and overdue_open == 0:
        return "Good"
    if on_time_pct >= 70:
        return "Monitor"
    return "Attention"


def build_delivery_performance(stage=None, category=None, year=None, month=None, financial_view=None) -> dict:
    all_rows, _ = get_goods_received_rows()
    rows = _apply_filters(all_rows, stage, category, year, month, financial_view)
    today = _today()

    classified = [{**row, **_classify_row(row, today)} for row in rows]

    data_issue_rows = [r for r in classified if r["status_category"] == "data_issue"]
    received_rows = [r for r in classified if r["received"]]
    open_rows = [r for r in classified if not r["received"] and r["status_category"] != "data_issue"]
    overdue_open_rows = [r for r in classified if r["status_category"] == "open_overdue"]
    on_time_rows = [r for r in received_rows if r["status_category"] == "received_ontime"]

    on_time_rate = (len(on_time_rows) / len(received_rows) * 100) if received_rows else None
    all_delays = [r["delay_days"] for r in classified if r["delay_days"] is not None and r["delay_days"] > 0]
    avg_delay = (sum(all_delays) / len(all_delays)) if all_delays else 0.0

    # Vendor aggregation
    vendor_map: dict[str, dict] = {}
    for r in classified:
        vendor = r.get("vendor") or "Unknown"
        if vendor not in vendor_map:
            vendor_map[vendor] = {
                "vendor": vendor, "po_value": 0.0, "po_lines": 0,
                "received": 0, "open": 0, "overdue_open": 0,
                "on_time_count": 0, "received_for_rate": 0,
                "actual_days_list": [], "lead_time_list": [], "delay_days_list": [],
                "data_issue_count": 0, "deliverable_count": 0,
            }
        v = vendor_map[vendor]
        v["po_lines"] += 1
        v["po_value"] += float(r.get("total_price") or 0)
        cat = r["status_category"]
        if cat == "data_issue":
            v["data_issue_count"] += 1
        else:
            v["deliverable_count"] += 1
            if r["received"]:
                v["received"] += 1
                v["received_for_rate"] += 1
                if cat == "received_ontime":
                    v["on_time_count"] += 1
                if r["actual_days"] is not None:
                    v["actual_days_list"].append(r["actual_days"])
            else:
                v["open"] += 1
                if cat == "open_overdue":
                    v["overdue_open"] += 1
            if r.get("lead_time_days") is not None:
                v["lead_time_list"].append(float(r["lead_time_days"]))
            if r["delay_days"] is not None:
                v["delay_days_list"].append(r["delay_days"])

    vendor_table = []
    for v in vendor_map.values():
        rec_rate = v["received_for_rate"]
        on_time_pct = round(v["on_time_count"] / rec_rate * 100, 1) if rec_rate > 0 else None
        avg_actual = round(sum(v["actual_days_list"]) / len(v["actual_days_list"]), 1) if v["actual_days_list"] else None
        avg_lead = round(sum(v["lead_time_list"]) / len(v["lead_time_list"]), 1) if v["lead_time_list"] else None
        avg_d = round(sum(v["delay_days_list"]) / len(v["delay_days_list"]), 1) if v["delay_days_list"] else None
        vendor_table.append({
            "vendor": v["vendor"],
            "po_value": round(v["po_value"], 2),
            "po_lines": v["po_lines"],
            "received": v["received"],
            "open": v["open"],
            "overdue_open": v["overdue_open"],
            "on_time_pct": on_time_pct,
            "avg_actual_days": avg_actual,
            "avg_lead_time": avg_lead,
            "avg_delay_days": avg_d,
            "status": _vendor_status(on_time_pct, v["overdue_open"], v["deliverable_count"]),
        })

    # Sort: overdue open desc, avg delay desc, po value desc
    vendor_table.sort(key=lambda x: (-(x["overdue_open"] or 0), -(x["avg_delay_days"] or 0), -(x["po_value"] or 0)))

    watchlist = [
        {
            "vendor": r.get("vendor") or "--",
            "po_no": r.get("po_no") or "--",
            "description": r.get("description") or "--",
            "date_gen_po": r.get("date_gen_po") or "--",
            "lead_time": r.get("lead_time_days"),
            "days_waiting": r.get("days_waiting"),
            "delay_days": r.get("delay_days"),
            "total_price": r.get("total_price"),
            "status": r.get("status"),
        }
        for r in overdue_open_rows
    ]
    watchlist.sort(key=lambda x: -(x["delay_days"] or 0))

    return {
        "kpis": {
            "po_lines_tracked": len(classified),
            "received_count": len(received_rows),
            "open_count": len(open_rows),
            "overdue_open_count": len(overdue_open_rows),
            "on_time_rate_pct": round(on_time_rate, 1) if on_time_rate is not None else None,
            "avg_delay_days": round(avg_delay, 1),
            "data_issue_count": len(data_issue_rows),
        },
        "vendor_table": vendor_table[:50],
        "watchlist": watchlist[:100],
    }
