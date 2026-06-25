/*
 * Smart Asset Matcher (frontend) — thin counterpart to backend/asset_resolver.py.
 *
 * Alias / acronym generation is done server-side (validated) and shipped in the
 * /api/asset-list `asset_profiles` map. This module only NORMALISES record text
 * and checks the shipped aliases with number-aware, word-boundary token matching
 * (so "Combi 1" never matches "Combi 10/11/12"). Read-only: it returns match
 * metadata and never mutates a record.
 *
 * Exposed as window.AssetMatcher.
 */
(function () {
    "use strict";

    var CONF = { HIGH: "High", MEDIUM: "Medium", LOW: "Low" };
    var SRC = {
        ID: "Asset ID match",
        NAME: "Asset name match",
        DESC: "Description match",
        TRANS: "Translated description match",
        FL: "Functional location match",
        KEYWORD: "Related keyword match",
    };
    var CONF_RANK = { High: 3, Medium: 2, Low: 1 };
    var GENERAL_AREA = {
        production: 1, low: 1, high: 1, risk: 1, kitchen: 1, facility: 1, area: 1,
        general: 1, utilities: 1, utility: 1, packing: 1, cooking: 1, preparation: 1,
        assembly: 1, washing: 1, support: 1, warehouse: 1, store: 1, "wo-asset": 1,
        na: 1, none: 1,
    };

    // ── normalisation (mirror of asset_resolver._tokenize) ───────────────────
    function tokenize(text) {
        if (text === null || text === undefined) return [];
        var s = String(text).toLowerCase();
        s = s.replace(/([a-z])(?=\d)/g, "$1 ");   // letters -> digits boundary
        s = s.replace(/(\d)(?=[a-z])/g, "$1 ");    // digits -> letters boundary
        s = s.replace(/[^a-z0-9]+/g, " ");          // punctuation -> space
        var out = [];
        var parts = s.split(/\s+/);
        for (var i = 0; i < parts.length; i++) {
            var tok = parts[i];
            if (!tok || tok === "no" || tok === "number") continue;
            if (/^\d+$/.test(tok)) {
                tok = tok.replace(/^0+/, "") || "0";
            }
            out.push(tok);
        }
        return out;
    }

    function normalizeText(text) {
        return tokenize(text).join(" ");
    }

    function containsSequence(tokens, seq) {
        var n = tokens.length, m = seq.length;
        if (m === 0 || m > n) return false;
        for (var i = 0; i <= n - m; i++) {
            var ok = true;
            for (var j = 0; j < m; j++) {
                if (tokens[i + j] !== seq[j]) { ok = false; break; }
            }
            if (ok) return true;
        }
        return false;
    }

    function anyAliasHit(tokens, aliasStrings) {
        for (var i = 0; i < aliasStrings.length; i++) {
            if (containsSequence(tokens, aliasStrings[i].split(" "))) return true;
        }
        return false;
    }

    function anyKeyword(tokens, keywords) {
        var set = {};
        for (var i = 0; i < tokens.length; i++) set[tokens[i]] = 1;
        for (var k = 0; k < keywords.length; k++) {
            var parts = keywords[k].split(" "), ok = true;
            for (var p = 0; p < parts.length; p++) { if (!set[parts[p]]) { ok = false; break; } }
            if (ok) return true;
        }
        return false;
    }

    function isGeneralArea(assetId) {
        var id = String(assetId || "").trim();
        if (/^en[a-z]{2}-/i.test(id)) return false;
        var toks = tokenize(id);
        if (!toks.length) return true;
        for (var i = 0; i < toks.length; i++) {
            if (!GENERAL_AREA[toks[i]] && !/^\d+$/.test(toks[i])) return false;
        }
        return true;
    }

    // ── record context (normalise once per record; cached without mutating) ──
    var _ctxCache = (typeof WeakMap === "function") ? new WeakMap() : null;

    function cachedContext(record) {
        if (!_ctxCache) return recordContext(record);
        var c = _ctxCache.get(record);
        if (!c) { c = recordContext(record); _ctxCache.set(record, c); }
        return c;
    }

    function recordContext(record) {
        return {
            assetIdNorm: normalizeText(record.asset_id || record.assetId || ""),
            assetIdRaw: String(record.asset_id || record.assetId || ""),
            nameTokens: tokenize(record.machine_equipment_name || record.raw_machine_name || record.machine_name || record.asset_name || ""),
            descTokens: tokenize(record.description_original || record.description || ""),
            transTokens: tokenize(record.translated_description || ""),
            flTokens: tokenize(record.raw_functional_location || record.raw_location || record.area || record.functional_location || ""),
        };
    }

    function matchContext(ctx, profile) {
        var idNorm = normalizeText(profile.assetId);
        var mismatch = function () {
            if (!ctx.assetIdRaw) return true;
            return ctx.assetIdNorm !== idNorm;
        };
        // 1) exact Asset ID
        if (idNorm && ctx.assetIdNorm === idNorm) {
            return mk(profile, SRC.ID, CONF.HIGH, false);
        }
        var nameTokens = profile.nameTokens || [];
        var aliases = profile.aliases || [];
        var related = profile.relatedKeywords || [];

        // 2) exact asset name in machine-name field
        if (nameTokens.length && containsSequence(ctx.nameTokens, nameTokens)) {
            return mk(profile, SRC.NAME, CONF.HIGH, mismatch());
        }
        // 3) alias hits (number-aware) -> Medium
        if (aliases.length) {
            if (anyAliasHit(ctx.nameTokens, aliases)) return mk(profile, SRC.NAME, CONF.MEDIUM, mismatch());
            if (anyAliasHit(ctx.descTokens, aliases)) return mk(profile, SRC.DESC, CONF.MEDIUM, mismatch());
            if (anyAliasHit(ctx.transTokens, aliases)) return mk(profile, SRC.TRANS, CONF.MEDIUM, mismatch());
        }
        // 4) related keyword only (no number in name) -> Low
        if (related.length && !profile.number && anyKeyword(ctx.descTokens.concat(ctx.transTokens), related)) {
            return mk(profile, SRC.KEYWORD, CONF.LOW, mismatch());
        }
        return null;
    }

    function mk(profile, source, confidence, mismatch) {
        return {
            matchedAssetId: profile.assetId,
            matchedAssetName: profile.canonicalName,
            matchSource: source,
            confidence: confidence,
            possibleAssetCodingMismatch: !!mismatch,
        };
    }

    function recordMatchesAsset(record, profile) {
        return matchContext(recordContext(record), profile);
    }

    // ── selected-asset filtering ─────────────────────────────────────────────
    function filterRecordsForSelectedAsset(records, profile, options) {
        options = options || {};
        var includeLow = !!options.includeRelated;
        var out = [];
        for (var i = 0; i < records.length; i++) {
            var m = matchContext(cachedContext(records[i]), profile);
            if (!m) continue;
            if (m.confidence === CONF.LOW && !includeLow) continue;
            out.push(Object.assign({}, records[i], { smartMatch: m }));
        }
        out.sort(function (a, b) { return (CONF_RANK[b.smartMatch.confidence] || 0) - (CONF_RANK[a.smartMatch.confidence] || 0); });
        return out;
    }

    function summarizeSelectedAsset(matched, profile) {
        var direct = 0, mismatches = 0, c = { High: 0, Medium: 0, Low: 0 };
        for (var i = 0; i < matched.length; i++) {
            var sm = matched[i].smartMatch;
            if (sm.matchSource === SRC.ID) direct++;
            if (sm.possibleAssetCodingMismatch) mismatches++;
            c[sm.confidence] = (c[sm.confidence] || 0) + 1;
        }
        var related = matched.length - direct;
        return {
            assetId: profile.assetId,
            assetName: profile.canonicalName,
            totalMatched: matched.length,
            directAssetIdMatches: direct,
            relatedMatches: related,
            possibleCodingMismatches: mismatches,
            byConfidence: c,
            summaryText: matched.length + " WO/MR records found for this asset. " + direct +
                " are direct Asset ID matches and " + related +
                " are related records detected from descriptions or names.",
        };
    }

    // ── smart search across all profiles ─────────────────────────────────────
    function searchRecords(records, query, profiles, options) {
        options = options || {};
        var q = normalizeText(query);
        if (!q) return [];
        var qTokens = tokenize(query);
        // an ad-hoc profile so "Combi 1" / "SBF 1" work even without a known asset
        var adhoc = { assetId: "", canonicalName: query, nameTokens: qTokens, number: lastNumber(qTokens), aliases: [qTokens.join(" ")], relatedKeywords: [] };
        var targets = [adhoc];
        var ids = Object.keys(profiles);
        for (var i = 0; i < ids.length; i++) {
            var p = profiles[ids[i]];
            if (normalizeText(p.assetId) === q) targets.push(p);
            else if ((p.aliases || []).length && anyAliasHit(qTokens, p.aliases)) targets.push(p);
        }
        var seen = {}, out = [];
        for (var r = 0; r < records.length; r++) {
            var ctx = cachedContext(records[r]);
            for (var t = 0; t < targets.length; t++) {
                var m = matchContext(ctx, targets[t]);
                if (m && (m.confidence !== CONF.LOW || options.includeRelated)) {
                    var key = records[r].work_order_id || ("idx" + r);
                    if (!seen[key]) { seen[key] = 1; out.push(Object.assign({}, records[r], { smartMatch: m })); }
                    break;
                }
            }
        }
        return out;
    }

    function lastNumber(tokens) {
        for (var i = tokens.length - 1; i >= 0; i--) { if (/^\d+$/.test(tokens[i])) return tokens[i]; }
        return null;
    }

    window.AssetMatcher = {
        CONF: CONF, SRC: SRC,
        normalizeText: normalizeText,
        tokenize: tokenize,
        recordContext: recordContext,
        recordMatchesAsset: recordMatchesAsset,
        filterRecordsForSelectedAsset: filterRecordsForSelectedAsset,
        summarizeSelectedAsset: summarizeSelectedAsset,
        searchRecords: searchRecords,
        isGeneralArea: isGeneralArea,
    };
})();
