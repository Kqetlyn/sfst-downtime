"""
Downtime Dashboard — Flask backend.
Serves the Downtime page and its required API endpoints.
"""

from datetime import datetime
from flask import Flask, jsonify, redirect, send_from_directory, request
import os

import db as _db

from downtime_service import (
    build_downtime_payload,
    build_mtbf_work_order_history_payload,
    get_work_order_import_status,
    import_work_order_file,
)
from spare_parts_service import build_project_transactions_payload

mira_bp = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "frontend"))
DATA_DIR = os.environ.get("DATA_DIR") or os.path.abspath(os.path.join(BASE_DIR, "..", "data"))
os.makedirs(DATA_DIR, exist_ok=True)
ASSET_MASTER_RELATIVE_PATH = os.path.join("master", "Asset_Master.xlsx")

app = Flask(__name__, static_folder=FRONTEND_DIR)
APP_VERSION = "2026-06-25-downtime-standalone-1"
_BACKEND_START = datetime.now()

import json as _json
import time as _time
import threading as _threading
import gzip as _gzip
import hashlib as _hashlib

_CACHE_DIR = os.path.join(DATA_DIR, "_dashboard_cache")
try:
    os.makedirs(_CACHE_DIR, exist_ok=True)
except Exception:
    pass
_CACHE_TTL = 600.0
_BUILD_LOCKS = {}
_BUILD_LOCKS_GUARD = _threading.Lock()
_REFRESH_TARGETS = []


def _cache_path(key):
    return os.path.join(_CACHE_DIR, _hashlib.md5(repr(key).encode("utf-8")).hexdigest() + ".json.gz")


def _cache_fresh(path, ttl):
    try:
        return os.path.exists(path) and (_time.time() - os.path.getmtime(path)) < ttl
    except OSError:
        return False


def _write_cache(key, builder):
    gz = _gzip.compress(_json.dumps(builder(), default=str).encode("utf-8"), 5)
    path = _cache_path(key)
    try:
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(gz)
        os.replace(tmp, path)
    except Exception:
        pass
    return gz


def _gzip_resp(gz, accepts_gzip):
    if accepts_gzip:
        resp = app.response_class(gz, mimetype="application/json")
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(gz))
        resp.headers["Vary"] = "Accept-Encoding"
        return resp
    return app.response_class(_gzip.decompress(gz), mimetype="application/json")


def _cached_json(key, builder, ttl=_CACHE_TTL):
    accepts = "gzip" in request.headers.get("Accept-Encoding", "").lower()
    path = _cache_path(key)
    if _cache_fresh(path, ttl):
        try:
            with open(path, "rb") as fh:
                return _gzip_resp(fh.read(), accepts)
        except OSError:
            pass
    with _BUILD_LOCKS_GUARD:
        lock = _BUILD_LOCKS.setdefault(key, _threading.Lock())
    with lock:
        if _cache_fresh(path, ttl):
            try:
                with open(path, "rb") as fh:
                    return _gzip_resp(fh.read(), accepts)
            except OSError:
                pass
        gz = _write_cache(key, builder)
    return _gzip_resp(gz, accepts)


def _register_refresh(key, builder):
    _REFRESH_TARGETS.append((key, builder))


def _background_refresher():
    def loop():
        while True:
            for key, builder in list(_REFRESH_TARGETS):
                try:
                    _write_cache(key, builder)
                except Exception:
                    pass
            _time.sleep(max(60.0, _CACHE_TTL * 0.5))
    _threading.Thread(target=loop, name="cache-refresher", daemon=True).start()


def _invalidate_route_cache():
    try:
        for fn in os.listdir(_CACHE_DIR):
            try:
                os.remove(os.path.join(_CACHE_DIR, fn))
            except OSError:
                pass
    except Exception:
        pass
    try:
        import importlib
        getattr(importlib.import_module("downtime_service"), "_DOWNTIME_CACHE").clear()
    except Exception:
        pass


_MUTATION_PREFIXES = ("/api/downtime/import",)


@app.after_request
def _clear_cache_after_mutation(response):
    try:
        if request.method == "POST" and any(request.path.startswith(p) for p in _MUTATION_PREFIXES):
            _invalidate_route_cache()
    except Exception:
        pass
    return response


@app.after_request
def _gzip_large_responses(response):
    try:
        if (
            response.status_code != 200
            or response.direct_passthrough
            or "Content-Encoding" in response.headers
            or "gzip" not in request.headers.get("Accept-Encoding", "").lower()
        ):
            return response
        data = response.get_data()
        if len(data) < 2048:
            return response
        compressed = _gzip.compress(data, 5)
        response.set_data(compressed)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(compressed))
        response.headers["Vary"] = "Accept-Encoding"
    except Exception:
        pass
    return response


@app.after_request
def apply_cache_headers(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response




# ── Health / admin ────────────────────────────────────────────────────────────

@app.route("/api/health")
def api_health():
    import downtime_service as _dt
    ollama_enabled = (
        os.environ.get("LLM_PROVIDER", "").lower() == "ollama"
        or os.environ.get("OLLAMA_ENABLED", "").lower() in {"1", "true", "yes"}
    )
    return jsonify({
        "status": "ok",
        "version": APP_VERSION,
        "startTime": _BACKEND_START.isoformat(),
        "uptimeSeconds": round((datetime.now() - _BACKEND_START).total_seconds()),
        "data": {
            "mrDataLoaded": bool(getattr(_dt, "_WO_LOAD_CACHE", {}).get("payload")),
            "assetMasterPresent": os.path.exists(os.path.join(DATA_DIR, ASSET_MASTER_RELATIVE_PATH)),
        },
        "caches": {
            "downtimeWarm": bool(getattr(_dt, "_DOWNTIME_CACHE", None)),
            "assetProfilesCached": _ASSET_PROFILE_CACHE.get("profiles") is not None,
        },
        "ollama": {
            "enabled": ollama_enabled,
            "baseUrl": os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            "model": os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"),
        },
    })


@app.route("/api/refresh-data", methods=["POST", "GET"])
def api_refresh_data():
    _invalidate_route_cache()
    return jsonify({
        "ok": True,
        "message": "Caches cleared. Fresh data will be loaded on the next request.",
        "clearedAt": datetime.now().isoformat(),
    })


@app.route("/api/db/status")
def db_status():
    return jsonify(_db.get_db_status())


@app.route("/api/db/sync-asset-master", methods=["POST"])
def db_sync_asset_master():
    result = _db.sync_asset_master_from_file(DATA_DIR)
    return jsonify(result), (200 if result.get("ok") else 500)


# ── Frontend routes ───────────────────────────────────────────────────────────

@app.route("/")
@app.route("/Downtime")
@app.route("/Downtime/index.html")
def downtime_root():
    return send_from_directory(os.path.join(FRONTEND_DIR, "Downtime"), "index.html")


@app.route("/<path:path>")
def frontend_files(path):
    return send_from_directory(FRONTEND_DIR, path)


# ── Asset list ────────────────────────────────────────────────────────────────

from asset_mapping import load_asset_mapping, build_refrigeration_tree, get_asset_mapping_meta
import asset_resolver

_ASSET_PROFILE_CACHE = {"signature": None, "profiles": None}


def _slim_profile(profile):
    return {
        "assetId": profile["assetId"],
        "canonicalName": profile["canonicalName"],
        "nameTokens": profile["nameTokens"],
        "number": profile["number"],
        "aliases": profile["aliases"],
        "relatedKeywords": profile["relatedKeywords"],
        "functionalLocation": profile["functionalLocation"],
        "machineGroup": profile["machineGroup"],
    }


def get_cached_asset_profiles(mapping, signature):
    if _ASSET_PROFILE_CACHE["signature"] == signature and _ASSET_PROFILE_CACHE["profiles"] is not None:
        return _ASSET_PROFILE_CACHE["profiles"]
    inputs = []
    for group in mapping.get("groups", []):
        for entry in group.get("asset_entries", []):
            inputs.append({
                "asset_id": entry.get("asset_id"),
                "name": entry.get("mappedAssetName") or entry.get("asset_display_name"),
                "machine_group": entry.get("mappedMainAssetGroup") or group.get("machine_group"),
                "functional_location": entry.get("mappedLocation") or entry.get("mappedSystemArea") or group.get("location"),
            })
    full = asset_resolver.build_all_asset_profiles(inputs)
    profiles = {aid: _slim_profile(p) for aid, p in full.items()}
    _ASSET_PROFILE_CACHE["signature"] = signature
    _ASSET_PROFILE_CACHE["profiles"] = profiles
    return profiles


@app.route("/api/asset-list")
def asset_list_api():
    try:
        mapping = load_asset_mapping(DATA_DIR)
        if not mapping["available"]:
            return jsonify({"machines": [], "error": mapping["message"]}), 404
        machines = []
        for group in mapping["groups"]:
            assets = [
                {
                    "asset_id": e["asset_id"],
                    "label": e["asset_display_name"],
                    "mappedStage": e.get("mappedStage"),
                    "mappedAssetName": e.get("mappedAssetName") or e.get("asset_display_name"),
                    "mappedMainAssetGroup": e.get("mappedMainAssetGroup") or group.get("mappedMainAssetGroup"),
                    "mappedMachineGroup": e.get("mappedMachineGroup") or "",
                    "mappedSubAssetGroup": e.get("mappedSubAssetGroup"),
                    "mappedLocation": e.get("mappedLocation") or group.get("mappedLocation"),
                    "mappedSystemArea": e.get("mappedSystemArea"),
                    "mappingStatus": e.get("mappingStatus"),
                }
                for e in group.get("asset_entries", [])
            ]
            machines.append({
                "machine_name": group["machine_group"],
                "location": group["location"],
                "criticality": group["criticality"],
                "mappedStage": group.get("mappedStage"),
                "mappedMainAssetGroup": group.get("mappedMainAssetGroup") or group["machine_group"],
                "mappedSubAssetGroup": group.get("mappedSubAssetGroup"),
                "mappedLocation": group.get("mappedLocation") or group["location"],
                "mappedSystemArea": group.get("mappedSystemArea"),
                "mappingStatus": group.get("mappingStatus"),
                "asset_count": len(assets),
                "assets": assets,
            })
        meta = get_asset_mapping_meta(DATA_DIR)
        return jsonify({
            "machines": machines,
            "refrigeration_tree": build_refrigeration_tree(mapping),
            "asset_profiles": get_cached_asset_profiles(mapping, meta.get("last_synced")),
            "meta": meta,
        })
    except Exception as exc:
        return jsonify({"machines": [], "error": str(exc)}), 500


# ── Downtime routes ───────────────────────────────────────────────────────────

@app.route("/api/downtime")
def downtime_data():
    period = request.args.get("period")
    month = request.args.get("month")
    start = request.args.get("start")
    end = request.args.get("end")
    stage = request.args.get("stage")
    work_orders_only = str(request.args.get("work_orders_only", "")).strip().lower() in {"1", "true", "yes", "on"}
    return _cached_json(
        ("downtime", period, month, start, end, work_orders_only, stage),
        lambda: build_downtime_payload(period, month, start, end, work_orders_only=work_orders_only, stage=stage),
    )


@app.route("/api/downtime/import-work-orders", methods=["GET", "POST"])
def downtime_import_work_orders():
    if request.method == "GET":
        return jsonify(get_work_order_import_status())
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "message": "No work order file uploaded."}), 400
    replace = str(request.form.get("replace", "true")).strip().lower() not in {"0", "false", "no"}
    result = import_work_order_file(upload, replace=replace)
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/downtime/mtbf-history")
def downtime_mtbf_history():
    return jsonify(build_mtbf_work_order_history_payload(stage=request.args.get("stage")))


@app.route("/api/maintenance/project_transactions")
def maintenance_project_transactions():
    return jsonify(build_project_transactions_payload())


@app.route("/api/page-sync/<page_key>")
def page_sync(page_key):
    last_synced = None
    if (page_key or "").strip().lower() == "downtime":
        sources = get_work_order_import_status().get("sources") or []
        last_synced = max(
            (s.get("last_modified") for s in sources if s.get("last_modified")),
            default=None,
        )
    return jsonify({"page": page_key, "last_synced": last_synced})


# ── Entry point ───────────────────────────────────────────────────────────────

def _free_port(port):
    import subprocess
    import signal
    try:
        my_pid = str(os.getpid())
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
            for line in out.splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1] if parts else ""
                    if pid.isdigit() and pid not in (my_pid, "0"):
                        subprocess.run(["taskkill", "/f", "/pid", pid], capture_output=True)
        else:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True).stdout
            for pid in out.split():
                if pid.isdigit() and pid != my_pid:
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except OSError:
                        pass
    except Exception:
        pass


def _start_cache_warming():
    _invalidate_route_cache()

    try:
        db_path = _db.init_db()
        print(f"[db] SQLite ready: {db_path}")
    except Exception as _db_exc:
        print(f"[db] WARNING: could not initialise SQLite — {_db_exc}")

    def _sync_asset_master():
        try:
            result = _db.sync_asset_master_from_file(DATA_DIR)
            print(f"[db] {result['message']}")
        except Exception as exc:
            print(f"[db] Asset Master sync error: {exc}")

    _threading.Thread(target=_sync_asset_master, name="db-asset-sync", daemon=True).start()

    _register_refresh(("downtime", None, None, None, None, False, None), build_downtime_payload)
    _background_refresher()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5005))
    debug = os.environ.get("FLASK_DEBUG", "0") not in {"0", "false", "no"}
    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        _free_port(port)
    if os.environ.get("WERKZEUG_RUN_MAIN") or not debug:
        _start_cache_warming()

    if not debug and os.environ.get("USE_WAITRESS", "1").lower() not in {"0", "false", "no"}:
        try:
            from waitress import serve
            print(f"Downtime server (waitress) on http://localhost:{port}")
            serve(app, host="0.0.0.0", port=port, threads=8)
            raise SystemExit(0)
        except ImportError:
            pass

    print(f"Downtime server (Flask dev) on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
