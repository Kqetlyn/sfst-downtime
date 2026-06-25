"""
Smart Asset Matching / Asset Resolver — reusable for ALL assets.

Many WO/MR records are not assigned to the correct Asset ID in the source data:
they sit under a general area (Production Low Risk, Kitchen, Facility, …) even
though the description clearly names a specific asset. This module builds an
asset profile for every asset and resolves which WO/MR records belong to it via:

  1. exact Asset ID
  2. exact normalized asset name
  3. generated aliases in description / translated description / machine name
  4. functional-location support
  5. related keywords (low confidence)

with number-aware, word-boundary matching so "Combi 1" never matches "Combi 10".

It NEVER mutates source records — it returns match metadata only. Profiles are
built once and cached; record text is normalised once per call.

Public API (mirrors the requested helper names):
  normalize_text(text)
  generate_asset_aliases(profile_inputs)        -> set[str]
  generate_exclude_patterns(profile_inputs)     -> set[str]  (sibling numbers)
  build_asset_profile(asset)                     -> dict
  build_all_asset_profiles(asset_list)           -> {assetId: profile}
  record_matches_asset(record, profile)          -> match dict | None
  match_record_to_asset_profiles(record, profiles, options)
  get_asset_match_source(record, profile)        -> str
  get_asset_match_confidence(record, profile)    -> str
  filter_records_for_selected_asset(records, profile, options)
  search_records_with_smart_asset_matching(records, query, profiles)
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

# ── confidence + source vocab ────────────────────────────────────────────────
CONF_HIGH = "High"
CONF_MEDIUM = "Medium"
CONF_LOW = "Low"

SRC_ASSET_ID = "Asset ID match"
SRC_ASSET_NAME = "Asset name match"
SRC_DESCRIPTION = "Description match"
SRC_TRANSLATED = "Translated description match"
SRC_FUNCTIONAL = "Functional location match"
SRC_KEYWORD = "Related keyword match"
SRC_MISMATCH_FLAG = "Possible asset coding mismatch"

# Words that carry no identifying signal — dropped from aliases/keywords.
_STOPWORDS = {
    "the", "and", "of", "for", "no", "number", "unit", "set", "system",
    "machine", "equipment", "line",  # "line" kept in number context via aliases
}

# Asset IDs / area codes that mean "no specific asset" (general-area buckets).
_GENERAL_AREA_TOKENS = {
    "production", "low", "high", "risk", "kitchen", "facility", "area", "general",
    "utilities", "utility", "packing", "cooking", "preparation", "assembly",
    "washing", "support", "warehouse", "store", "wo-asset", "n/a", "na", "none",
}

# Acronym families that are common shop-floor abbreviations (auto-detected from
# initials, but listed so single-word names still acronym sensibly).
_ALWAYS_ACRONYM = True


# ── normalisation ────────────────────────────────────────────────────────────
def normalize_text(text) -> str:
    """Lowercase, strip punctuation, collapse spaces, drop 'no'/'#'/'unit' noise
    so 'No.1', 'No 1', '#1' and '1' are equivalent. Returns a space-joined,
    number-aware token string."""
    return " ".join(_tokenize(text))


def _tokenize(text) -> list[str]:
    """Token list with letter/digit boundaries split and number noise dropped.

    'Combi1' -> ['combi','1'];  'Combi Oven No.1' -> ['combi','oven','1'];
    'SBF1' -> ['sbf','1']. Pure-digit tokens are stripped of leading zeros so
    '01' == '1'. The words 'no'/'number'/'#' before a digit are removed.
    """
    if text is None:
        return []
    s = str(text).lower()
    # split letters<->digits so joined forms match spaced forms
    s = re.sub(r"(?<=[a-z])(?=\d)", " ", s)
    s = re.sub(r"(?<=\d)(?=[a-z])", " ", s)
    # punctuation -> space
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = []
    for tok in s.split():
        if tok in {"no", "number"}:
            continue  # 'no 1' -> '1'
        if tok.isdigit():
            tok = tok.lstrip("0") or "0"
        tokens.append(tok)
    return tokens


def _split_name_number(name: str):
    """Return (base_tokens, number_str|None) for an asset name, e.g.
    'Combi Oven No.1' -> (['combi','oven'], '1')."""
    toks = _tokenize(name)
    number = None
    base = []
    for t in toks:
        if t.isdigit():
            number = t  # last trailing number wins
        else:
            base.append(t)
    return base, number


# ── alias / exclude generation ───────────────────────────────────────────────
def generate_asset_aliases(name: str) -> set[str]:
    """Auto-generate normalized alias token-strings from an asset name.

    Each alias is a normalized space-joined token sequence (the matcher checks it
    as a contiguous run of tokens). Includes full name, first-word+number,
    acronym+number, and (number-only is intentionally NOT emitted — too broad)."""
    base, number = _split_name_number(name)
    base = [b for b in base if b and b not in {"the", "of", "and"}]
    aliases: set[str] = set()
    if not base:
        return aliases

    def with_num(seq: list[str]) -> list[str]:
        return seq + ([number] if number else [])

    full = with_num(base)
    aliases.add(" ".join(full))                       # combi oven 1
    aliases.add(" ".join(with_num(base[:1])))         # combi 1   (first word + number)
    if len(base) >= 2:
        aliases.add(" ".join(with_num(base[:2])))     # combi oven 1 (already) / two words + num

    # Acronym from initials of significant words (>=2 words), e.g. Spiral Blast
    # Freezer -> sbf;  Air Blast Freezer -> abf.
    sig = [b for b in base if b not in _STOPWORDS]
    if _ALWAYS_ACRONYM and len(sig) >= 2:
        acronym = "".join(w[0] for w in sig)
        if len(acronym) >= 2:
            aliases.add(" ".join(with_num([acronym])))   # sbf 1

    # Common "<family> freezer <n>" / "blast freezer <n>" style reductions:
    # keep the last significant family word + number too (e.g. 'freezer 1').
    if len(sig) >= 2 and number:
        aliases.add(" ".join([sig[-1], number]))         # freezer 1, conveyor 3
        # last two significant words + number (blast freezer 1)
        if len(sig) >= 2:
            aliases.add(" ".join(sig[-2:] + [number]))

    # Drop aliases that are just a stopword + number (e.g. 'line 1' alone is too
    # broad) UNLESS the base is a single word (then it's the asset's identity).
    cleaned = set()
    for a in aliases:
        toks = a.split()
        if len(toks) == 2 and toks[0] in _STOPWORDS:
            continue
        cleaned.add(a)
    return {a for a in cleaned if a.strip()}


def generate_exclude_patterns(name: str) -> set[str]:
    """Sibling-number alias strings that must NOT match this asset.

    For 'Combi Oven 1' this yields the same aliases but with numbers 2..19, so a
    description that says 'combi oven 10' cannot be pulled onto asset 1. Token
    matching already prevents 1->10 substring hits; this is the explicit guard
    the spec asks for (nearby asset numbers)."""
    base, number = _split_name_number(name)
    if not number:
        return set()
    out: set[str] = set()
    siblings = {str(n) for n in range(2, 20)} | {"10", "11", "12", "13", "14", "15"}
    siblings.discard(number)
    for sib in siblings:
        renamed = " ".join([b for b in base]) + " " + sib
        for alias in generate_asset_aliases(renamed.strip()):
            # only keep sibling aliases that actually end in the sibling number
            if alias.split() and alias.split()[-1] == sib.lstrip("0"):
                out.add(alias)
    return out


# ── profile building ─────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _load_overrides() -> dict:
    # Curated config (version-controlled) — lives next to the code, not in the
    # gitignored data/ dir. Maps assetId -> {aliases, excludePatterns, relatedKeywords}.
    path = Path(__file__).resolve().parent / "asset_alias_overrides.json"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def build_asset_profile(asset: dict) -> dict:
    """Build a matching profile for one asset.

    `asset` keys (any missing are tolerated): asset_id, name/asset_name,
    machine_group, functional_location/location, criticality, equipment_type.
    """
    asset_id = str(asset.get("asset_id") or asset.get("assetId") or "").strip()
    name = (asset.get("name") or asset.get("asset_name") or asset.get("assetName")
            or asset.get("mappedAssetName") or asset.get("label") or "").strip()
    machine_group = (asset.get("machine_group") or asset.get("mappedMainAssetGroup") or "").strip()
    functional_location = (asset.get("functional_location") or asset.get("location")
                           or asset.get("mappedLocation") or asset.get("mappedSystemArea") or "").strip()

    aliases = generate_asset_aliases(name) if name else set()
    exclude = generate_exclude_patterns(name) if name else set()

    # related keywords = significant base words (no number) — low-confidence only.
    base, number = _split_name_number(name)
    related = {b for b in base if b not in _STOPWORDS and len(b) > 2}

    # manual overrides (applied AFTER auto-generation)
    ov = _load_overrides().get(asset_id) or {}
    for a in ov.get("aliases", []):
        aliases.add(normalize_text(a))
    for e in ov.get("excludePatterns", []):
        exclude.add(normalize_text(e))
    for k in ov.get("relatedKeywords", []):
        related.add(normalize_text(k))

    return {
        "assetId": asset_id,
        "assetIdNorm": normalize_text(asset_id),
        "canonicalName": name,
        "nameNorm": normalize_text(name),
        "nameTokens": _tokenize(name),
        "number": number,
        "machineGroup": machine_group,
        "functionalLocation": functional_location,
        "flNorm": normalize_text(functional_location),
        "aliases": sorted(aliases),
        "excludePatterns": sorted(exclude),
        "relatedKeywords": sorted(related),
    }


def build_all_asset_profiles(asset_list: list[dict]) -> dict:
    return {p["assetId"]: p for p in (build_asset_profile(a) for a in asset_list) if p["assetId"]}


# ── matching primitives ──────────────────────────────────────────────────────
def _contains_sequence(tokens: list[str], seq_tokens: list[str]) -> bool:
    """True if seq_tokens appears as a contiguous run in tokens (exact tokens, so
    '1' never matches '10')."""
    n, m = len(tokens), len(seq_tokens)
    if m == 0 or m > n:
        return False
    for i in range(n - m + 1):
        if tokens[i:i + m] == seq_tokens:
            return True
    return False


def _any_alias_hit(tokens: list[str], aliases: list[str]) -> bool:
    return any(_contains_sequence(tokens, a.split()) for a in aliases)


def _is_general_area(asset_id: str) -> bool:
    norm = _tokenize(asset_id)
    if not norm:
        return True
    # an EN..-###### style code is a real asset id; otherwise treat word-y ids as areas
    if re.match(r"^en[a-z]{2}", "".join(norm[:1]) + (norm[1] if len(norm) > 1 else "")):
        return False
    if re.match(r"^en[a-z]{2}-", asset_id.strip().lower()):
        return False
    return all(t in _GENERAL_AREA_TOKENS or t.isdigit() for t in norm)


def _record_fields(record: dict) -> dict:
    """Pull the matchable text fields from a WO/MR record (read-only)."""
    description_parts = [
        record.get("description_original"),
        record.get("description"),
        record.get("remarks"),
        record.get("wo_description"),
    ]
    translated_parts = [
        record.get("translated_description"),
        record.get("wo_translated_description"),
    ]
    return {
        "asset_id": str(record.get("asset_id") or record.get("assetId") or "").strip(),
        "machine_name": str(record.get("machine_equipment_name") or record.get("raw_machine_name")
                            or record.get("machine_name") or record.get("asset_name")
                            or record.get("equipment_name") or ""),
        "description": " ".join(str(value or "").strip() for value in description_parts if str(value or "").strip()),
        "translated": " ".join(str(value or "").strip() for value in translated_parts if str(value or "").strip()),
        "functional_location": str(record.get("raw_functional_location") or record.get("raw_location")
                                   or record.get("area") or record.get("functional_location")
                                   or record.get("wo_location") or ""),
    }


def _prepare_record_match_context(record: dict) -> dict:
    fields = _record_fields(record)
    return {
        "fields": fields,
        "recordAssetNorm": normalize_text(fields["asset_id"]),
        "nameTokens": _tokenize(fields["machine_name"]),
        "descriptionTokens": _tokenize(fields["description"]),
        "translatedTokens": _tokenize(fields["translated"]),
        "functionalLocationTokens": _tokenize(fields["functional_location"]),
    }


def _match_profile_against_context(profile: dict, context: dict) -> dict | None:
    f = context["fields"]
    rec_asset_norm = context["recordAssetNorm"]

    if profile["assetIdNorm"] and rec_asset_norm == profile["assetIdNorm"]:
        return _match(profile, SRC_ASSET_ID, CONF_HIGH, mismatch=False)

    name_tokens = context["nameTokens"]
    desc_tokens = context["descriptionTokens"]
    trans_tokens = context["translatedTokens"]
    fl_tokens = context["functionalLocationTokens"]

    # exclude-pattern guard: if a sibling-number alias clearly matches in name or
    # description, this record is about a DIFFERENT asset in the family.
    excl = profile["excludePatterns"]
    if excl and not _any_alias_hit(name_tokens + desc_tokens + trans_tokens, profile["aliases"]):
        if _any_alias_hit(name_tokens + desc_tokens + trans_tokens, excl):
            return None

    asset_id_is_area = _is_general_area(f["asset_id"])
    # a coding mismatch = matched by text but the record's own asset_id is a
    # different real asset OR a general area.
    def mismatch_flag() -> bool:
        if not f["asset_id"]:
            return True
        return rec_asset_norm != profile["assetIdNorm"]

    # 2) exact asset name in the machine-name field -> High
    if profile["nameTokens"] and _contains_sequence(name_tokens, profile["nameTokens"]):
        return _match(profile, SRC_ASSET_NAME, CONF_HIGH, mismatch=mismatch_flag())

    # 3) alias hits (number-aware) -> Medium
    if profile["aliases"]:
        if _any_alias_hit(name_tokens, profile["aliases"]):
            return _match(profile, SRC_ASSET_NAME, CONF_MEDIUM, mismatch=mismatch_flag())
        if _any_alias_hit(desc_tokens, profile["aliases"]):
            return _match(profile, SRC_DESCRIPTION, CONF_MEDIUM, mismatch=mismatch_flag())
        if _any_alias_hit(trans_tokens, profile["aliases"]):
            return _match(profile, SRC_TRANSLATED, CONF_MEDIUM, mismatch=mismatch_flag())

    # 4) functional location + some description support -> Medium/Low
    if profile["flNorm"] and fl_tokens and _contains_sequence(fl_tokens, _tokenize(profile["functionalLocation"])):
        # FL alone is weak; only a match if a related keyword also appears.
        if profile["relatedKeywords"] and _any_keyword(desc_tokens + trans_tokens + name_tokens, profile["relatedKeywords"]):
            return _match(profile, SRC_FUNCTIONAL, CONF_LOW, mismatch=mismatch_flag())

    # 5) related keyword only (no number) -> Low
    if profile["relatedKeywords"] and _any_keyword(desc_tokens + trans_tokens, profile["relatedKeywords"]):
        # require the asset to have NO number, or this is too broad
        if not profile["number"]:
            return _match(profile, SRC_KEYWORD, CONF_LOW, mismatch=mismatch_flag())

    return None


def record_matches_asset(record: dict, profile: dict) -> dict | None:
    """Return match metadata if `record` belongs to the asset, else None.

    Result: {matchedAssetId, matchedAssetName, matchSource, confidence,
             possibleAssetCodingMismatch}. Never mutates `record`.
    """
    return _match_profile_against_context(profile, _prepare_record_match_context(record))


def match_record_to_asset_profiles(record: dict, profiles: dict | list[dict],
                                   options: dict | None = None) -> list[dict]:
    """Return every asset-profile match for a record, ranked best-first.

    The record text is normalised and tokenised once, then reused across every
    profile check. This is the efficient shared path for analytics pages that
    need to resolve one record against the full asset catalog.
    """
    options = options or {}
    include_low = bool(options.get("include_related"))
    limit = options.get("limit")
    iterable = profiles.values() if isinstance(profiles, dict) else profiles
    context = _prepare_record_match_context(record)
    results = []
    for profile in iterable:
        match = _match_profile_against_context(profile, context)
        if not match:
            continue
        if match["confidence"] == CONF_LOW and not include_low:
            continue
        results.append(match)
    results.sort(
        key=lambda row: (
            -_CONF_RANK.get(row["confidence"], 0),
            1 if row.get("possibleAssetCodingMismatch") else 0,
            row.get("matchedAssetName") or row.get("matchedAssetId") or "",
        )
    )
    if isinstance(limit, int) and limit > 0:
        return results[:limit]
    return results


def _any_keyword(tokens: list[str], keywords: list[str]) -> bool:
    tokset = set(tokens)
    return any(all(part in tokset for part in kw.split()) for kw in keywords)


def _match(profile, source, confidence, mismatch) -> dict:
    return {
        "matchedAssetId": profile["assetId"],
        "matchedAssetName": profile["canonicalName"],
        "matchSource": source,
        "confidence": confidence,
        "possibleAssetCodingMismatch": bool(mismatch),
    }


def get_asset_match_source(record: dict, profile: dict) -> str | None:
    m = record_matches_asset(record, profile)
    return m["matchSource"] if m else None


def get_asset_match_confidence(record: dict, profile: dict) -> str | None:
    m = record_matches_asset(record, profile)
    return m["confidence"] if m else None


# ── selected-asset filtering + search ────────────────────────────────────────
_CONF_RANK = {CONF_HIGH: 3, CONF_MEDIUM: 2, CONF_LOW: 1}


def filter_records_for_selected_asset(records: list[dict], profile: dict, options: dict | None = None) -> list[dict]:
    """Return records that belong to the asset, each shallow-copied with a
    `smartMatch` key. Low-confidence matches only when
    options['include_related'] is True. Never mutates the originals."""
    options = options or {}
    include_low = bool(options.get("include_related"))
    out = []
    for rec in records:
        m = record_matches_asset(rec, profile)
        if not m:
            continue
        if m["confidence"] == CONF_LOW and not include_low:
            continue
        enriched = dict(rec)
        enriched["smartMatch"] = m
        out.append(enriched)
    out.sort(key=lambda r: _CONF_RANK.get(r["smartMatch"]["confidence"], 0), reverse=True)
    return out


def search_records_with_smart_asset_matching(records: list[dict], query: str, profiles: dict,
                                             options: dict | None = None) -> list[dict]:
    """Smart free-text search across records using the query as an ad-hoc asset
    profile (so 'Combi 1' / 'SBF 1' / 'ABF 2' work) plus matching any known
    profile whose alias the query names."""
    options = options or {}
    q = normalize_text(query)
    if not q:
        return []
    # ad-hoc profile from the raw query (treats it like an asset name)
    query_profile = build_asset_profile({"asset_id": "", "name": query})
    # also collect known profiles the query refers to (alias/name/id)
    target_profiles = [query_profile]
    q_tokens = _tokenize(query)
    for p in profiles.values():
        if p["assetIdNorm"] and p["assetIdNorm"] == q:
            target_profiles.append(p)
        elif p["aliases"] and _any_alias_hit(q_tokens, p["aliases"]):
            target_profiles.append(p)

    seen, out = set(), []
    for rec in records:
        key = id(rec)
        for p in target_profiles:
            m = record_matches_asset(rec, p)
            if m and (m["confidence"] != CONF_LOW or options.get("include_related")):
                if key not in seen:
                    seen.add(key)
                    enriched = dict(rec)
                    enriched["smartMatch"] = m
                    out.append(enriched)
                break
    return out


def summarize_selected_asset(matched: list[dict], profile: dict) -> dict:
    """KPI summary for the selected-asset view (counts only; read-only)."""
    direct = sum(1 for r in matched if r["smartMatch"]["matchSource"] == SRC_ASSET_ID)
    related = len(matched) - direct
    mismatches = sum(1 for r in matched if r["smartMatch"].get("possibleAssetCodingMismatch"))
    return {
        "assetId": profile["assetId"],
        "assetName": profile["canonicalName"],
        "totalMatched": len(matched),
        "directAssetIdMatches": direct,
        "relatedMatches": related,
        "possibleCodingMismatches": mismatches,
        "byConfidence": {
            CONF_HIGH: sum(1 for r in matched if r["smartMatch"]["confidence"] == CONF_HIGH),
            CONF_MEDIUM: sum(1 for r in matched if r["smartMatch"]["confidence"] == CONF_MEDIUM),
            CONF_LOW: sum(1 for r in matched if r["smartMatch"]["confidence"] == CONF_LOW),
        },
        "summaryText": (
            f"{len(matched)} WO/MR records found for this asset. "
            f"{direct} are direct Asset ID matches and {related} are related records "
            f"detected from descriptions or names."
        ),
    }
