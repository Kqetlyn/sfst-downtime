let downtimePayload = null;
let downtimeCachePayload = null;
const chartRefs = {};
const workOrderSlaWarningKeys = new Set();
const DOWNTIME_EMBED_MODE = !!window.DOWNTIME_EMBED_MODE;
const DOWNTIME_STAGE_ALL = "all";
const MACHINE_EXPLORER_ASSET_RENDER_LIMIT = 250;
const MACHINE_HISTORY_RENDER_LIMIT = 500;
let downtimeEmbedHeightObserverStarted = false;

function postEmbeddedHeight() {
    if (!DOWNTIME_EMBED_MODE || window.parent === window) return;
    const root = document.documentElement;
    const body = document.body;
    const height = Math.max(
        root?.scrollHeight || 0,
        body?.scrollHeight || 0,
        root?.offsetHeight || 0,
        body?.offsetHeight || 0,
        900
    );
    window.parent.postMessage({ type: "maintenance-downtime-height", height }, window.location.origin);
}

function scheduleEmbeddedHeightPost() {
    if (!DOWNTIME_EMBED_MODE) return;
    window.requestAnimationFrame(() => postEmbeddedHeight());
}

function scheduleLowPriorityWork(callback, timeout = 500) {
    if (typeof window.requestIdleCallback === "function") {
        return { type: "idle", id: window.requestIdleCallback(callback, { timeout }) };
    }
    return { type: "timeout", id: window.setTimeout(callback, 80) };
}

function cancelLowPriorityWork(handle) {
    if (!handle) return;
    if (handle.type === "idle" && typeof window.cancelIdleCallback === "function") {
        window.cancelIdleCallback(handle.id);
        return;
    }
    window.clearTimeout(handle.id);
}

function startEmbeddedHeightSync() {
    if (!DOWNTIME_EMBED_MODE || downtimeEmbedHeightObserverStarted) return;
    downtimeEmbedHeightObserverStarted = true;

    const attachObservers = () => {
        scheduleEmbeddedHeightPost();

        if (typeof ResizeObserver !== "undefined" && document.body) {
            const resizeObserver = new ResizeObserver(() => scheduleEmbeddedHeightPost());
            resizeObserver.observe(document.body);
        }

        if (typeof MutationObserver !== "undefined" && document.body) {
            const mutationObserver = new MutationObserver(() => scheduleEmbeddedHeightPost());
            mutationObserver.observe(document.body, {
                childList: true,
                subtree: true,
                attributes: true,
                characterData: true,
            });
        }
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", attachObservers, { once: true });
    } else {
        attachObservers();
    }

    window.addEventListener("load", scheduleEmbeddedHeightPost);
    window.addEventListener("resize", scheduleEmbeddedHeightPost);
}

function syncTopicMirrors() {
    document.querySelectorAll("[data-topic-mirror]").forEach((target) => {
        const sourceId = target.dataset.topicMirror;
        const source = sourceId ? document.getElementById(sourceId) : null;
        if (!source) return;
        target.textContent = source.textContent || "--";
    });
}

function resizeVisibleCharts() {
    window.requestAnimationFrame(() => {
        Object.values(chartRefs).forEach((chart) => {
            if (chart && typeof chart.resize === "function") chart.resize();
        });
    });
}

function relocateOperationalTopicSections() {
    const yearlyTarget = document.getElementById("yearly-history-target");
    const historicalSection = document.querySelector(".historical-trend-section");
    if (yearlyTarget && historicalSection && historicalSection.parentElement !== yearlyTarget) {
        historicalSection.classList.remove("pending-topic-move");
        historicalSection.classList.add("topic-moved-section");
        yearlyTarget.appendChild(historicalSection);
    }

    const duplicateTarget = document.getElementById("data-reliability-duplicate-target");
    const duplicateSection = document.getElementById("dup-wo-section");
    if (duplicateTarget && duplicateSection && duplicateSection.parentElement !== duplicateTarget) {
        duplicateSection.classList.remove("pending-topic-move");
        duplicateSection.classList.add("topic-moved-section");
        duplicateTarget.appendChild(duplicateSection);
    }
}

function setDashboardTopic(topicKey) {
    const nextTopic = topicKey || selectedDashboardTopic || "mr-tracking";
    selectedDashboardTopic = nextTopic;
    document.querySelectorAll("[data-topic-target]").forEach((button) => {
        const active = button.dataset.topicTarget === nextTopic;
        button.classList.toggle("active", active);
        button.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll("[data-topic-panel]").forEach((panel) => {
        panel.classList.toggle("hidden", panel.dataset.topicPanel !== nextTopic);
    });
    syncTopicMirrors();
    resizeVisibleCharts();
    scheduleEmbeddedHeightPost();
}

function wireDashboardTopicControls() {
    relocateOperationalTopicSections();
    document.querySelectorAll("[data-topic-target]").forEach((button) => {
        button.addEventListener("click", () => setDashboardTopic(button.dataset.topicTarget));
    });
    document.querySelectorAll("[data-analysis-toggle]").forEach((button) => {
        button.addEventListener("click", () => {
            const panel = button.closest("[data-topic-panel]");
            if (!panel) return;
            const open = panel.classList.toggle("analysis-open");
            button.setAttribute("aria-expanded", open ? "true" : "false");
            button.textContent = open ? "Hide Analysis" : "View Analysis";
            syncTopicMirrors();
            resizeVisibleCharts();
            scheduleEmbeddedHeightPost();
        });
    });
    setDashboardTopic(selectedDashboardTopic);
}

// Machine Explorer state
let assetListData = [];
let assetProfiles = {};            // {assetId: slim profile} from /api/asset-list for smart matching
let includeRelatedMatches = false; // "Include possible related matches" toggle (low-confidence)
let openWorkOrdersData = [];
let openWorkOrdersLoaded = false;
let assetListLoaded = false;
let assetListLoadFailed = false;
let selectedMachineName = null;
let selectedAssetId = null;
const PM_CM_REVIEW_STORAGE_KEY = "downtime.preventiveCorrectiveReviewDecisions.v1";
let machineExplorerSearch = "";
let machineExplorerSort = "most_wo_mr";
let mtbfHistoryPayload = null;
let mtbfHistoryPromise = null;
let selectedMttrCriticality = "";
let allWorkOrderRowsCache = null;
let allWorkOrderRowsPromise = null;
// ── Page-level Production Equipment / Utilities / Unclassified category filter ──
// Mirrors backend asset_mapping._GROUP_TO_CATEGORY so old cached rows (without an
// equipment_category field) still classify correctly.
let selectedEquipmentCategory = "all"; // all | Production Equipment | Utilities | Unclassified
const EQUIP_GROUP_TO_CATEGORY = {
    "Production Equipment": "Production Equipment",
    "Utilities / Support": "Utilities",
    "Utilities": "Utilities",
    "Refrigeration": "Utilities",
    "Facility / Building": "Unclassified",
    "Unknown / Review": "Unclassified",
};
function getRowEquipmentCategory(row) {
    const c = row && row.equipment_category;
    if (c) return c;
    return EQUIP_GROUP_TO_CATEGORY[String((row && row.machine_group) || "").trim()] || "Unclassified";
}
function applyCategoryFilter(rows) {
    if (!Array.isArray(rows) || selectedEquipmentCategory === "all") return rows || [];
    return rows.filter((r) => getRowEquipmentCategory(r) === selectedEquipmentCategory);
}
// Category-scoped all-year rows — single chokepoint replacing the many
// `getCategoryScopedAllRows()` reads.
function getCategoryScopedAllRows() {
    return applyCategoryFilter(allWorkOrderRowsCache || getFallbackAllWorkOrderRows());
}
let mrMovementSelectedYear = "";
let mrMovementUserSelectedYear = false;
let mrCarryoverFilter = "all";
let mrCarryoverSort = "duration_desc";
let mrTrackingEquipmentFilter = "all";
let mrTrackingSelectedYear = "";
let mrTrackingSelectedMonth = "";
let mrMachineFilter = "all";
let woSlaYearFilter = "";
let woSlaMonthFilter = "all";
let preventiveCorrectiveListFilter = "review";
let preventiveCorrectiveFinancialYearFilter = "";
let preventiveCorrectiveAnalysisTypeFilter = "preventive";
let preventiveCorrectiveAnalysisPeriodFilter = "full";
let preventiveCorrectiveAnalysisYearModeFilter = "financial";
let preventiveCorrectiveReviewDecisions = loadPreventiveCorrectiveReviewDecisions();
let deferredMrMovementHandle = null;

// ── Missing-data / data-cleansing review layer ───────────────────────────────
// Editable cleansing fields (status / remark / follow-up) are stored SEPARATELY
// from the raw imported WO/MR data in localStorage, so the original source is
// never modified. The missing-data counts always come from the raw SLA model.
const DATA_CLEANSING_STORAGE_KEY = "downtime.dataCleansingReview.v1";
const CLEANSING_STATUS_OPTIONS = ["Open", "Checking", "Confirmed Data Issue", "Valid Record", "To Exclude", "Corrected"];
const CLEANSING_CLEARED_STATUSES = new Set(["Corrected", "Valid Record", "To Exclude"]);
const MISSING_FIELD_TYPES = ["Missing Actual Start", "Missing Actual End", "Missing Asset", "Invalid Date", "Missing Duration", "Others"];
let dataCleansingReview = loadDataCleansingReview();
let lastWorkOrderSlaModel = null;
let missingDataFilters = { severity: "all", fieldType: "all", slaStatus: "all", machineGroup: "all", search: "", cleared: "all" };
let deferredAllYearWorkOrderHandle = null;
let deferredAllYearDynamicsHandle = null;
let allYearWorkOrderRenderToken = 0;
let duplicateWoAnalysisHandle = null;
let duplicateWoAnalysisToken = 0;
let duplicateWorkOrderGroupsCache = { pm: [], sameDay: [], recurring: [] };
let downtimeRefreshInFlight = false;
let lastDowntimeRefreshAt = 0;
let downtimeStageFilter = DOWNTIME_STAGE_ALL;
let selectedDashboardTopic = "mr-tracking";
let machineExplorerSelectedAssetId = "";
let machineExplorerSelectedGroup = "All Groups";
let machineHistoryViewMode = "selected";        // "selected" = single asset, "all" = all assets in current filters
let machineHistorySort = "latest_wo_mr";        // Step 3 WO/MR history table sort
let machineHistoryYearFilter = "";
let machineHistoryMonthFilter = "";
let machineHistoryDateFrom = "";
let machineHistoryDateTo = "";
let machineHistorySearch = "";
let machineExplorerRefrigSubgroup = "";          // active subgroup key inside Refrigeration
let machineExplorerAssetCriticalityFilter = "";
let machineExplorerAckFilter = "";
// Spare part trend data cache (loaded once, reused on scope change)
let cmcSpareTrendData = null;

// MR Comparison & Trend Analysis state
let cmcMode = "month";
let cmcScope = "all";      // Asset/Machine Group Scope (all | Critical | Non-Critical / Facility | Production Equipment | Refrigeration | …)
let cmcYearView = "compare"; // "compare" = Year A vs Year B | "all" = Show All Years
let cmcMonthA = "";
let cmcMonthB = "";
let cmcYearA = "";
let cmcYearB = "";
let cmcCustomAStart = "";
let cmcCustomAEnd = "";
let cmcCustomBStart = "";
let cmcCustomBEnd = "";
let machineExplorerRefrigCondenserGroupId = "";   // condenser ID selected in CDE tree (group mode)
let machineExplorerRefrigExpandedCondensers = new Set(); // condensers expanded in the CDE tree

const CRITICALITY_ORDER = ["Critical", "Non-Critical / Facility"];
const CRITICALITY_COLORS = {
    "Critical": "#ef4444",
    "Non-Critical / Facility": "#64748b",
};
// Criticality labels treated as "Critical" in all dashboard filters.
// Utilities and Refrigeration are operational/production-critical and must appear
// when the user selects the "Critical" filter in MTTR / MTBF sections.
const PRODUCTION_CRITICAL_LABELS = new Set([
    "critical", "semi critical", "production critical",
    "utility", "utilities", "utility support", "utilities support",
    "refrigeration",
]);
const NON_PRODUCTION_LABELS = new Set([
    "non critical facility",
    "facility non critical",
    "facility",
    "non critical",
    "support",
    "support system",
    "support systems",
    "non production",
]);
// Machine group names (from Asset_Master "Main Asset Group") that are
// operational-critical. Used as a fallback when the row's criticality field is
// blank or set to the generic backend default.
const CRITICAL_MACHINE_GROUPS = new Set([
    "Production Equipment",
    "Utilities",
    "Utilities / Support",
    "Refrigeration",
]);
const MR_MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MR_FINANCIAL_YEAR_START_MONTH = 4; // April
const MR_FINANCIAL_MONTH_ORDER = Array.from({ length: 12 }, (_, index) => (MR_FINANCIAL_YEAR_START_MONTH - 1 + index) % 12);
const MR_FINANCIAL_MONTH_LABELS = MR_FINANCIAL_MONTH_ORDER.map((monthIndex) => MR_MONTH_LABELS[monthIndex]);
function getMrFinancialYearStart(date) {
    if (!(date instanceof Date) || Number.isNaN(date.getTime())) return null;
    return date.getMonth() + 1 >= MR_FINANCIAL_YEAR_START_MONTH ? date.getFullYear() : date.getFullYear() - 1;
}
function getMrFinancialYearRange(financialYearStart) {
    const startYear = Number(financialYearStart);
    return {
        start: new Date(startYear, MR_FINANCIAL_YEAR_START_MONTH - 1, 1, 0, 0, 0, 0),
        end: new Date(startYear + 1, MR_FINANCIAL_YEAR_START_MONTH - 1, 1, 0, 0, 0, 0),
    };
}
function isDateInMrFinancialYear(date, financialYearStart) {
    if (!(date instanceof Date) || Number.isNaN(date.getTime())) return false;
    const { start, end } = getMrFinancialYearRange(financialYearStart);
    return date >= start && date < end;
}
function getMrFinancialYearLabel(financialYearStart) {
    const startYear = Number(financialYearStart);
    if (!Number.isFinite(startYear)) return "selected financial year";
    return `FY ${startYear}-${String(startYear + 1).slice(-2)}`;
}
function getMrFinancialMonthLabelsWithYear(financialYearStart) {
    const startYear = Number(financialYearStart);
    if (!Number.isFinite(startYear)) return MR_FINANCIAL_MONTH_LABELS;
    return MR_FINANCIAL_MONTH_ORDER.map((monthIndex) => {
        const year = monthIndex + 1 >= MR_FINANCIAL_YEAR_START_MONTH ? startYear : startYear + 1;
        return `${MR_MONTH_LABELS[monthIndex]} ${year}`;
    });
}
function getMrFinancialMonthPosition(monthNumber) {
    const monthIndex = Number(monthNumber) - 1;
    const found = MR_FINANCIAL_MONTH_ORDER.indexOf(monthIndex);
    return found >= 0 ? found : 0;
}
function orderMrCountsByFinancialCalendar(monthlyCounts = []) {
    return MR_FINANCIAL_MONTH_ORDER.map((monthIndex) => monthlyCounts[monthIndex] || 0);
}
const MACHINE_EXPLORER_ALL_GROUP = "All Groups";
const MACHINE_EXPLORER_GROUPS = [
    "All Groups",
    "Production Equipment",
    "Utilities",
    "Refrigeration",
    "Non-Critical / Facility",
];
const MACHINE_EXPLORER_LEGACY_GROUP_MAP = {
    "Utilities / Support": "Utilities",
    "Facility / Building": "Non-Critical / Facility",
    Critical: MACHINE_EXPLORER_ALL_GROUP,
    "Unknown / Review": MACHINE_EXPLORER_ALL_GROUP,
};

function normalizeMachineExplorerGroupLabel(group) {
    const value = String(group || "").trim();
    if (!value) return MACHINE_EXPLORER_ALL_GROUP;
    return MACHINE_EXPLORER_LEGACY_GROUP_MAP[value] || value;
}

// Condenser-to-evaporator network for the Refrigeration System drill-down.
// Machine Group: Refrigeration | Subgroup: Condenser-Evaporator Network.
// Each condenser is the parent asset; each evaporator in the list is a child.
const REFRIGERATION_NETWORK = [
    { condenserName: "Condenser Unit No.1", condenserId: "ENUT-240083", evaporators: [
        { name: "Evaporator CDU1-UC1.1", id: "ENUT-240084" }, { name: "Evaporator CDU1-UC1.2", id: "ENUT-240085" },
        { name: "Evaporator CDU1-UC1.3", id: "ENUT-240086" }, { name: "Evaporator CDU1-UC1.4", id: "ENUT-240087" },
        { name: "Evaporator CDU1-UC1.5", id: "ENUT-240088" }, { name: "Evaporator CDU1-UC1.6", id: "ENUT-240089" },
    ]},
    { condenserName: "Condenser Unit No.2", condenserId: "ENUT-240090", evaporators: [
        { name: "Evaporator CDU2-UC2.1", id: "ENUT-240091" }, { name: "Evaporator CDU2-UC2.2", id: "ENUT-240092" },
        { name: "Evaporator CDU2-UC2.3", id: "ENUT-240093" }, { name: "Evaporator CDU2-UC2.4", id: "ENUT-240094" },
        { name: "Evaporator CDU2-UC2.5", id: "ENUT-240095" }, { name: "Evaporator CDU2-UC2.6", id: "ENUT-240096" },
        { name: "Evaporator CDU2-UC2.7", id: "ENUT-240097" },
    ]},
    { condenserName: "Condenser Unit No.3", condenserId: "ENUT-240098", evaporators: [
        { name: "Evaporator CDU3-UC3.1", id: "ENUT-240099" }, { name: "Evaporator CDU3-UC3.2", id: "ENUT-240100" },
        { name: "Evaporator CDU3-UC3.3", id: "ENUT-240101" }, { name: "Evaporator CDU3-UC3.4", id: "ENUT-240102" },
    ]},
    { condenserName: "Condenser Unit No.4", condenserId: "ENUT-240103", evaporators: [
        { name: "Evaporator CDU4-UC4.1", id: "ENUT-240104" }, { name: "Evaporator CDU4-UC4.2", id: "ENUT-240105" },
        { name: "Evaporator CDU4-UC4.3", id: "ENUT-240106" }, { name: "Evaporator CDU4-UC4.4", id: "ENUT-240107" },
        { name: "Evaporator CDU4-UC4.5", id: "ENUT-240108" }, { name: "Evaporator CDU4-UC4.6", id: "ENUT-240109" },
        { name: "Evaporator CDU4-UC4.7", id: "ENUT-240110" }, { name: "Evaporator CDU4-UC4.8", id: "ENUT-240111" },
        { name: "Evaporator CDU4-UC4.9", id: "ENUT-240112" }, { name: "Evaporator CDU4-UC4.10", id: "ENUT-240113" },
    ]},
    { condenserName: "Condenser Unit No.5", condenserId: "ENUT-240114", evaporators: [
        { name: "Evaporator CDU5-UC5.1", id: "ENUT-240115" }, { name: "Evaporator CDU5-UC5.2", id: "ENUT-240116" },
        { name: "Evaporator CDU5-UC5.3", id: "ENUT-240117" },
    ]},
    { condenserName: "Condenser Unit No.6", condenserId: "ENUT-240118", evaporators: [
        { name: "Evaporator CDU6-UC6.1", id: "ENUT-240119" }, { name: "Evaporator CDU6-UC6.2", id: "ENUT-240120" },
        { name: "Evaporator CDU6-UC6.3", id: "ENUT-240121" },
    ]},
    { condenserName: "Condenser Unit No.7", condenserId: "ENUT-240122", evaporators: [] },
    { condenserName: "Condenser Unit No.8", condenserId: "ENUT-240123", evaporators: [
        { name: "Evaporator CDU8-UC8.1", id: "ENUT-240124" }, { name: "Evaporator CDU8-UC8.2", id: "ENUT-240125" },
        { name: "Evaporator CDU8-UC8.3", id: "ENUT-240126" },
    ]},
    { condenserName: "Condenser Unit No.9 - COLD5", condenserId: "ENUT-240127", evaporators: [
        { name: "Evaporator CDU9-UC9.1", id: "ENUT-240128" }, { name: "Evaporator CDU9-UC9.2", id: "ENUT-240129" },
    ]},
    { condenserName: "Condenser Unit No.11", condenserId: "ENUT-240130", evaporators: [
        { name: "Evaporator CDU11-UC11.1", id: "ENUT-240131" }, { name: "Evaporator CDU11-UC11.2", id: "ENUT-240132" },
        { name: "Evaporator CDU11-UC11.3", id: "ENUT-240133" },
    ]},
    { condenserName: "Condenser Unit No.12", condenserId: "ENUT-240134", evaporators: [
        { name: "Evaporator CDU12-UC12.1", id: "ENUT-240135" }, { name: "Evaporator CDU12-UC12.2", id: "ENUT-240136" },
        { name: "Evaporator CDU12-UC12.3", id: "ENUT-240137" }, { name: "Evaporator CDU12-UC12.4", id: "ENUT-240138" },
        { name: "Evaporator CDU12-UC12.5", id: "ENUT-240139" }, { name: "Evaporator CDU12-UC12.6", id: "ENUT-240140" },
        { name: "Evaporator CDU12-UC12.7", id: "ENUT-240141" }, { name: "Evaporator CDU12-UC12.8", id: "ENUT-240142" },
        { name: "Evaporator CDU12-UC12.9", id: "ENUT-240143" },
    ]},
    { condenserName: "Condenser Unit No.13", condenserId: "ENUT-240144", evaporators: [
        { name: "Evaporator CDU13-UC13.1", id: "ENUT-240145" },
    ]},
    { condenserName: "Condenser Unit No.14", condenserId: "ENUT-240146", evaporators: [
        { name: "Evaporator CDU14-UC14.1", id: "ENUT-240147" }, { name: "Evaporator CDU14-UC14.2", id: "ENUT-240148" },
        { name: "Evaporator CDU14-UC14.3", id: "ENUT-240149" }, { name: "Evaporator CDU14-UC14.4", id: "ENUT-240150" },
        { name: "Evaporator CDU14-UC14.5", id: "ENUT-240151" },
    ]},
    { condenserName: "Condenser Unit No.15", condenserId: "ENUT-240152", evaporators: [
        { name: "Evaporator CDU15-UC15.1", id: "ENUT-240153" }, { name: "Evaporator CDU15-UC15.2", id: "ENUT-240154" },
        { name: "Evaporator CDU15-UC15.3", id: "ENUT-240155" }, { name: "Evaporator CDU15-UC15.4", id: "ENUT-240156" },
    ]},
    { condenserName: "Condenser Unit No.16", condenserId: "ENUT-240157", evaporators: [
        { name: "Evaporator CDU16-UC16.1", id: "ENUT-240158" }, { name: "Evaporator CDU16-UC16.2", id: "ENUT-240159" },
        { name: "Evaporator CDU16-UC16.3", id: "ENUT-240160" }, { name: "Evaporator CDU16-UC16.4", id: "ENUT-240161" },
        { name: "Evaporator CDU16-UC16.5", id: "ENUT-240162" },
    ]},
    { condenserName: "Condenser Unit No.17", condenserId: "ENUT-240163", evaporators: [
        { name: "Evaporator CDU17-UC17.1", id: "ENUT-240164" }, { name: "Evaporator CDU17-UC17.2", id: "ENUT-240165" },
        { name: "Evaporator CDU17-UC17.3", id: "ENUT-240166" },
    ]},
    { condenserName: "Condenser Unit No.18", condenserId: "ENUT-240167", evaporators: [
        { name: "Evaporator CDU18-UC18.1", id: "ENUT-240168" }, { name: "Evaporator CDU18-UC18.2", id: "ENUT-240169" },
    ]},
    { condenserName: "Condenser Unit No.19 - COLD1", condenserId: "ENUT-240170", evaporators: [
        { name: "Evaporator CDU19-UC19.1", id: "ENUT-240171" }, { name: "Evaporator CDU19-UC19.2", id: "ENUT-240172" },
    ]},
    { condenserName: "Condenser Unit No.20 - COLD2", condenserId: "ENUT-240173", evaporators: [
        { name: "Evaporator CDU20-UC20.1", id: "ENUT-240174" }, { name: "Evaporator CDU20-UC20.2", id: "ENUT-240175" },
    ]},
    { condenserName: "Condenser Unit No.21 - COLD3", condenserId: "ENUT-240176", evaporators: [
        { name: "Evaporator CDU21-UC21.1", id: "ENUT-240177" },
    ]},
    { condenserName: "Condenser Unit No.22 - COLD7", condenserId: "ENUT-240178", evaporators: [
        { name: "Evaporator CDU22-UC22.1", id: "ENUT-240179" },
    ]},
    { condenserName: "Condenser Unit No.23 - Air Blast Freezer1", condenserId: "ENUT-240180", evaporators: [
        { name: "Evaporator CDU23-UC23.1", id: "ENUT-240181" },
    ]},
    { condenserName: "Condenser Unit No.24 - COLD6", condenserId: "ENUT-240182", evaporators: [
        { name: "Evaporator CDU24-UC24.1", id: "ENUT-240183" },
    ]},
    { condenserName: "Condenser Unit No.27", condenserId: "ENUT-240184", evaporators: [
        { name: "Evaporator CDU27-UC27.1", id: "ENUT-240185" },
    ]},
    { condenserName: "Condenser Unit No.28", condenserId: "ENUT-240186", evaporators: [
        { name: "Evaporator CDU28-UC28.1", id: "ENUT-240187" },
    ]},
    { condenserName: "Condenser Unit No.29 - Air Blast Freezer2", condenserId: "ENUT-240188", evaporators: [
        { name: "Evaporator CDU29-UC29.1", id: "ENUT-240189" },
    ]},
    { condenserName: "Condenser Unit No.30", condenserId: "ENUT-240190", evaporators: [
        { name: "Evaporator CDU30-UC30.1", id: "ENUT-240191" },
    ]},
    { condenserName: "Condenser Unit No.33 - Ice Maker", condenserId: "ENUT-240192", evaporators: [
        { name: "Evaporator CDU33-UC33.1", id: "ENUT-240193" },
    ]},
    { condenserName: "Condenser Unit No.34", condenserId: "ENUT-240194", evaporators: [
        { name: "Evaporator CDU34-UC34.1", id: "ENUT-240195" },
    ]},
    { condenserName: "Condenser Unit No.35", condenserId: "ENUT-240196", evaporators: [
        { name: "Evaporator CDU35-UC35.1", id: "ENUT-240197" },
    ]},
    { condenserName: "Condenser Unit No.36", condenserId: "ENUT-240198", evaporators: [
        { name: "Evaporator CDU36-UC36.1", id: "ENUT-240199" },
    ]},
    { condenserName: "Condenser Unit No.37", condenserId: "ENUT-240200", evaporators: [
        { name: "Evaporator CDU37-UC37.1", id: "ENUT-240201" },
    ]},
    { condenserName: "Condenser Unit No.38", condenserId: "ENUT-240202", evaporators: [
        { name: "Evaporator CDU38-UC38.1", id: "ENUT-240203" },
    ]},
    { condenserName: "Condenser Unit No.39", condenserId: "ENUT-240204", evaporators: [
        { name: "Evaporator CDU39-UC39.1", id: "ENUT-240205" },
    ]},
    { condenserName: "Condenser Unit No.40", condenserId: "ENUT-240206", evaporators: [
        { name: "Evaporator CDU40-UC40.1", id: "ENUT-240207" },
    ]},
    { condenserName: "Condenser Unit No.41", condenserId: "ENUT-240208", evaporators: [
        { name: "Evaporator CDU41-UC41.1", id: "ENUT-240209" },
    ]},
    { condenserName: "Condenser Unit No.42 - Air Blast Chill1", condenserId: "ENUT-240210", evaporators: [
        { name: "Evaporator CDU42-UC42.1", id: "ENUT-240211" },
    ]},
    { condenserName: "Condenser Unit No.43 - Air Blast Chill2", condenserId: "ENUT-240212", evaporators: [
        { name: "Evaporator CDU43-UC43.1", id: "ENUT-240213" },
    ]},
    { condenserName: "Condenser Unit No.44 - Holding Chill", condenserId: "ENUT-240214", evaporators: [
        { name: "Evaporator CDU44-UC44.1", id: "ENUT-240215" },
    ]},
    { condenserName: "Condenser Unit No.45 - Cold4", condenserId: "ENUT-240216", evaporators: [
        { name: "Evaporator CDU45-UC45.1", id: "ENUT-240217" },
    ]},
];

// Sub machine groups for the Refrigeration System drill-down.
const REFRIG_SUBGROUPS = [
    { key: "all",                  label: "All Refrigeration Assets" },
    { key: "condenser-evaporator", label: "Condenser / Evaporator" },
    { key: "air-blast",            label: "Air Blast Freezer / Chiller" },
    { key: "cold-room",            label: "Cold Room / Freezer" },
    { key: "ice-maker",            label: "Ice Maker" },
    { key: "other",                label: "Other Refrigeration" },
];

// Classify each condenser entry into a subgroup based on its name.
const REFRIG_CDU_SUBGROUP = new Map(
    REFRIGERATION_NETWORK.map((entry) => {
        const name = entry.condenserName.toLowerCase();
        let sg = "condenser-evaporator";
        if (/air blast|chill/.test(name)) sg = "air-blast";
        else if (/cold\s*\d+|cold\d/.test(name)) sg = "cold-room";
        else if (/ice maker/.test(name)) sg = "ice-maker";
        return [entry.condenserId, sg];
    })
);

// All Asset IDs that are part of the CDU condenser-evaporator network.
const REFRIG_CDU_ALL_IDS = new Set(
    REFRIGERATION_NETWORK.flatMap((e) => [e.condenserId, ...e.evaporators.map((ev) => ev.id)])
);

const MR_RAISED_DATE_ALIASES = [
    "MR Raised Date",
    "mr_raised_date",
    "Created date and time",
    "created_date_and_time",
    "request_created_time",
    "Request Created Date",
    "request_created_date",
    "Created Date",
    "created_date",
    "Reported Date",
    "reported_date",
    "Request Date",
    "request_date",
    "Actual Start",
    "actual_start",
    "Actual Start Date",
    "actual_start_date",
    "actual_start_time",
    "Start Date",
    "start_date",
    "start_time",
    "maintenance_start_time",
];
const MR_FINISHED_DATE_ALIASES = [
    "MR Finished Date",
    "mr_finished_date",
    "Resolution Date",
    "resolution_date",
    "Actual end",
    "Actual End",
    "actual_end",
    "Actual End Date",
    "actual_end_date",
    "actual_end_time",
    "Completed Date",
    "completed_date",
    "Finish Date",
    "finish_date",
    "End Date",
    "end_date",
    "end_time",
    "maintenance_end_time",
];
const MR_ID_ALIASES = [
    "MR ID",
    "mr_id",
    "Maintenance request",
    "Maintenance Request",
    "maintenance_request",
    "Request ID",
    "request_id",
    "Maintenance Request ID",
    "maintenance_request_id",
    "Maintenance Order ID",
    "maintenance_order_id",
    "Work Order ID",
    "work_order_id",
    "WO ID",
    "wo_id",
];
const MR_EQUIPMENT_ALIASES = [
    "Equipment Name",
    "equipment_name",
    "Machine Name",
    "machine_name",
    "machine_name_display",
    "Asset Name",
    "asset_name",
    "asset_display_name",
    "AssetID",
    "asset_id",
    "Asset ID",
    "machine_group",
];
const MR_ASSET_ID_ALIASES = ["AssetID", "asset_id", "Asset ID", "machine_code", "equipment_id"];
const MR_ACK_STATUS_ALIASES = [
    "Acknowledged",
    "acknowledged",
    "Acknowledgement Status",
    "acknowledgement_status",
    "Acknowledgment Status",
    "acknowledgment_status",
    "Ack Status",
    "ack_status",
    "Is Acknowledged",
    "is_acknowledged",
];
const MR_ACK_DATE_ALIASES = [
    "Acknowledged Date",
    "acknowledged_date",
    "Acknowledgement Date",
    "acknowledgement_date",
    "Acknowledgment Date",
    "acknowledgment_date",
    "Ack Date",
    "ack_date",
    "First Response Date",
    "first_response_date",
];
const MR_STATUS_ALIASES = [
    "Status",
    "status",
    "Request State",
    "request_state",
    "Current lifecycle state",
    "current_lifecycle_state",
    "Lifecycle State",
    "LifecycleState",
    "lifecycle_state",
];
const MR_SEVERITY_ALIASES = [
    "Service level",
    "service_level",
    "Severity",
    "severity",
    "Priority",
    "priority",
    "Criticality",
    "criticality",
    "Impact Level",
    "impact_level",
];
const MR_ASSIGNED_ALIASES = [
    "Assigned To",
    "assigned_to",
    "Assignee",
    "assignee",
    "Technician",
    "technician",
    "Assigned Technician",
    "assigned_technician",
    "Person In Charge",
    "person_in_charge",
    "Owner",
    "owner",
];
const MR_REMARKS_ALIASES = [
    "Remarks",
    "remarks",
    "Notes",
    "notes",
    "Description",
    "description",
    "Problem",
    "problem",
    "Details",
    "details",
    "Comments",
    "comments",
];

const SLA_CREATED_DATE_ALIASES = [
    "request_created_time",
    "Request Created Date",
    "request_created_date",
    "Created Date",
    "created_date",
    "Created date and time",
    "created_date_and_time",
    "Request Date",
    "request_date",
    "Reported Date",
    "reported_date",
];
const SLA_START_DATE_ALIASES = [
    "actual_start_time",
    "Actual Start Date",
    "actual_start_date",
    "Actual Start",
    "actual_start",
    "Maintenance Start Date",
    "maintenance_start_time",
    "Start Date",
    "start_date",
];
const SLA_END_DATE_ALIASES = [
    "actual_end_time",
    "Actual End Date",
    "actual_end_date",
    "Actual End",
    "actual_end",
    "Maintenance End Date",
    "maintenance_end_time",
    "End Date",
    "end_date",
    "Closed Date",
    "closed_date",
    "Completed Date",
    "completed_date",
];
const DEFAULT_WORK_ORDER_SLA_TARGETS = [
    { key: "S1", label: "S1 Critical", shortLabel: "S1", fallbackSeverity: "Critical", responseTargetHours: 1, completionTargetHours: null, rank: 1 },
    { key: "S2", label: "S2 High", shortLabel: "S2", fallbackSeverity: "High", responseTargetHours: 4, completionTargetHours: 72, rank: 2 },
    { key: "S3", label: "S3 Medium", shortLabel: "S3", fallbackSeverity: "Medium", responseTargetHours: 48, completionTargetHours: 24 * 21, rank: 3 },
    { key: "S4", label: "S4 Low", shortLabel: "S4", fallbackSeverity: "Low", responseTargetHours: null, completionTargetHours: 24 * 45, rank: 4 },
];
let WORK_ORDER_SLA_TARGETS = DEFAULT_WORK_ORDER_SLA_TARGETS.map((target) => ({ ...target }));
const WORK_ORDER_SLA_UNCLASSIFIED = {
    key: "UNCLASSIFIED",
    label: "Unclassified",
    shortLabel: "N/A",
    fallbackSeverity: "Unclassified",
    responseTargetHours: null,
    completionTargetHours: null,
    rank: 99,
};
const WORK_ORDER_SLA_STATUS_ORDER = {
    "Open Overdue": 0,
    Late: 1,
    "Missing Start Date": 2,
    "Missing End Date": 3,
    "Missing Data": 4,
    "No SLA Target": 5,
    "Met Target": 6,
};

function toNullableSlaHours(value) {
    if (value === null || value === undefined || value === "") return null;
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
}

function pickConfiguredSlaValue(row = {}, keys = [], fallbackValue = null) {
    for (const key of keys) {
        if (Object.prototype.hasOwnProperty.call(row, key)) return row[key];
    }
    return fallbackValue;
}

function normalizeSlaTargetRow(row = {}, fallback = {}) {
    const key = String(row.key || row.severity_key || fallback.key || "").trim().toUpperCase();
    if (!key) return null;
    return {
        key,
        label: String(row.label || row.severity_label || fallback.label || key).trim(),
        shortLabel: String(row.shortLabel || row.short_label || row.short || fallback.shortLabel || key).trim(),
        fallbackSeverity: String(row.fallbackSeverity || row.fallback_severity || fallback.fallbackSeverity || key).trim(),
        responseTargetHours: toNullableSlaHours(pickConfiguredSlaValue(row, ["responseTargetHours", "response_target_hours"], fallback.responseTargetHours)),
        completionTargetHours: toNullableSlaHours(pickConfiguredSlaValue(row, ["completionTargetHours", "completion_target_hours"], fallback.completionTargetHours)),
        rank: Number.isFinite(Number(row.rank ?? fallback.rank)) ? Number(row.rank ?? fallback.rank) : 99,
    };
}

function applySlaTargetConfig(payload = {}) {
    const defaultByKey = new Map(DEFAULT_WORK_ORDER_SLA_TARGETS.map((target) => [target.key, target]));
    const configuredTargets = payload?.config?.sla_targets?.targets;
    if (!Array.isArray(configuredTargets) || !configuredTargets.length) {
        WORK_ORDER_SLA_TARGETS = DEFAULT_WORK_ORDER_SLA_TARGETS.map((target) => ({ ...target }));
        return;
    }
    const nextByKey = new Map();
    configuredTargets.forEach((row) => {
        const rawKey = String(row?.key || row?.severity_key || "").trim().toUpperCase();
        const normalized = normalizeSlaTargetRow(row, defaultByKey.get(rawKey) || {});
        if (normalized) nextByKey.set(normalized.key, normalized);
    });
    DEFAULT_WORK_ORDER_SLA_TARGETS.forEach((target) => {
        if (!nextByKey.has(target.key)) nextByKey.set(target.key, { ...target });
    });
    WORK_ORDER_SLA_TARGETS = [...nextByKey.values()].sort((a, b) => (a.rank || 99) - (b.rank || 99));
}

function getSlaTargetSourceText() {
    const config = downtimePayload?.config?.sla_targets;
    if (config?.available) {
        return `${config.message || "SLA targets loaded from Asset_Master.xlsx."} Edit Asset_Master.xlsx > SLA_Targets to change response/completion targets.`;
    }
    return `${config?.message || "Using built-in default SLA targets."} Edit Asset_Master.xlsx > SLA_Targets to make them editable.`;
}

// Placeholder only: estimated downtime requires a real operating-hours config before any value is calculated.
const OPERATING_HOURS_PLACEHOLDER = {
    configured: false,
    source: "placeholder",
    windows: [],
};

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}

function setHtml(id, value) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = value;
}

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (match) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "\"": "&quot;",
        "'": "&#39;",
    }[match]));
}

function fmtHours(hours) {
    if (hours === null || hours === undefined || Number.isNaN(Number(hours))) return "--";
    const numeric = Number(hours);
    if (numeric <= 0) return "0 min";
    if (numeric < 1) return `${Math.round(numeric * 60)} min`;
    const wholeHours = Math.floor(numeric);
    const minutes = Math.round((numeric - wholeHours) * 60);
    if (minutes === 60) return `${wholeHours + 1} hr`;
    if (minutes > 0) return `${wholeHours} hr ${minutes} min`;
    return `${wholeHours} hr`;
}

function fmtAxisHours(hours) {
    if (hours === null || hours === undefined || Number.isNaN(Number(hours))) return "--";
    return `${Number(hours).toLocaleString(undefined, { maximumFractionDigits: 1 })} hrs`;
}

function fmtDaysHours(hours) {
    if (hours === null || hours === undefined || Number.isNaN(Number(hours))) return "--";
    const numeric = Number(hours);
    if (numeric <= 0) return "0 hr";
    if (numeric < 24) return fmtHours(numeric);
    const days = numeric / 24;
    if (days >= 10) return `${days.toLocaleString(undefined, { maximumFractionDigits: 0 })} days`;
    return `${days.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} days`;
}

function fmtMtbfDays(hours) {
    if (hours === null || hours === undefined || Number.isNaN(Number(hours))) return "";
    return fmtDaysHours(hours);
}

function fmtNumber(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
    return Number(value).toLocaleString();
}

function fmtPercent(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
    const numeric = Number(value);
    const rounded = Math.abs(numeric - Math.round(numeric)) < 0.05
        ? { maximumFractionDigits: 0 }
        : { minimumFractionDigits: 1, maximumFractionDigits: 1 };
    return `${numeric.toLocaleString(undefined, rounded)}%`;
}

function fmtHoursIfAvailable(hours, hasRecords, unavailableText = "No valid records") {
    if (!hasRecords) return unavailableText;
    return fmtHours(hours);
}

function parseDateValue(value) {
    if (!value) return null;
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatDateKey(date) {
    if (!date) return "";
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
}

function formatMonthKey(date) {
    if (!date) return "";
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    return `${year}-${month}`;
}

function formatMtbfBucketLabel(date, bucketMode) {
    if (!date) return "";
    if (bucketMode === "month") {
        return date.toLocaleDateString("en-GB", { month: "short", year: "numeric" });
    }
    return date.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

function median(values) {
    if (!values.length) return null;
    const sorted = [...values].sort((a, b) => a - b);
    const middle = Math.floor(sorted.length / 2);
    if (sorted.length % 2) return sorted[middle];
    return (sorted[middle - 1] + sorted[middle]) / 2;
}

function sumHours(records) {
    return records.reduce((sum, record) => sum + Number(record.hours || 0), 0);
}

function normalizeClassification(value) {
    return String(value || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, " ").replace(/\s+/g, " ").trim();
}

function isOpenLifecycleState(value) {
    return ["new", "in progress", "inprogress"].includes(normalizeClassification(value));
}

function getTtrHours(row) {
    if (row?.valid_mttr_ttr === false || row?.valid_ttr === false) return null;
    if (row?.data_quality_flag && row.data_quality_flag !== "Valid") return null;
    const status = getMrStatus(row);
    if (status && status !== "--" && !isMrFinishedStatus(status)) return null;
    const raw = row?.ttr_hours ?? row?.duration_hours ?? row?.original_ttr_hours;
    if (raw !== null && raw !== undefined && !(typeof raw === "string" && raw.trim() === "")) {
        const hours = Number(raw);
        if (Number.isFinite(hours) && hours >= 0) return hours;
    }

    const start = parseDateValue(row?.actual_start_time || row?.maintenance_start_time || row?.start_time || row?.actual_start);
    const end = parseDateValue(row?.actual_end_time || row?.maintenance_end_time || row?.end_time || row?.actual_end);
    if (!start || !end || end < start) return null;
    // Raw imported TTR is maintenance resolution time. If it is missing, derive it from
    // the valid work-order date span instead of excluding the row from MTTR/TTR views.
    return Math.round(((end.getTime() - start.getTime()) / 3600000) * 1000) / 1000;
}

function getWorkOrderRows(management) {
    // Apply the page-level equipment-category filter at the source so every section
    // (KPIs, MTTR, MTBF, trend, rankings, tables) inherits it.
    return applyCategoryFilter(Array.isArray(management?.work_orders) ? management.work_orders : []);
}

function getPeriodLabel(meta = {}) {
    if (meta.period === "this_month" && meta.month_label && meta.month_label !== "All Months") return meta.month_label;
    if (meta.period === "all_years") return "All-year";
    if (meta.period === "custom" && meta.period_start && meta.period_end) return "Custom range";
    if (meta.period === "ytd") return "YTD";
    if (meta.period_label) return meta.period_label;
    const selected = document.getElementById("period-select")?.selectedOptions?.[0]?.textContent?.trim();
    return selected || "YTD";
}

function getSelectedDowntimeStage() {
    return document.getElementById("downtime-stage-filter")?.value || downtimeStageFilter || DOWNTIME_STAGE_ALL;
}

function buildDowntimeApiUrl(params = {}) {
    const query = new URLSearchParams({ ...params, _: String(Date.now()) });
    const stage = getSelectedDowntimeStage();
    if (stage && stage !== DOWNTIME_STAGE_ALL) query.set("stage", stage);
    return `/api/downtime?${query.toString()}`;
}

function buildDowntimeMtbfHistoryUrl() {
    const query = new URLSearchParams({ _: String(Date.now()) });
    const stage = getSelectedDowntimeStage();
    if (stage && stage !== DOWNTIME_STAGE_ALL) query.set("stage", stage);
    return `/api/downtime/mtbf-history?${query.toString()}`;
}

// Incremented by resetStageScopedCaches() so any in-flight all-year or MTBF
// fetch started for the previous stage discards its result instead of writing
// stale data into the shared cache.
let _stageFetchGeneration = 0;

function resetStageScopedCaches() {
    _stageFetchGeneration++;
    allWorkOrderRowsCache = null;
    allWorkOrderRowsPromise = null;
    mtbfHistoryPayload = null;
    mtbfHistoryPromise = null;
}

function getCurrentDowntimePeriodSelection() {
    const period = document.getElementById("period-select")?.value || "ytd";
    return {
        period,
        start: period === "custom" ? (document.getElementById("custom-start")?.value || "") : "",
        end: period === "custom" ? (document.getElementById("custom-end")?.value || "") : "",
    };
}

async function reloadCurrentDowntimeData() {
    const { period, start, end } = getCurrentDowntimePeriodSelection();
    if (period === "custom" && (!start || !end)) return;
    await loadDowntimeData(period, "", start, end);
}

function getWorkOrderRecordCount(summary = {}, downtimeSummary = {}, rows = []) {
    const count = downtimeSummary.work_order_record_count ?? summary.total_work_orders ?? rows.length;
    const numeric = Number(count);
    return Number.isFinite(numeric) && numeric >= 0 ? numeric : rows.length;
}

function hasAssetListClassification(row) {
    if (row?.has_assetlist_classification === false) return false;
    if (row?.classification_source === "fallback" || row?.mapping_source === "fallback") return false;
    return Boolean(String(row?.raw_criticality ?? row?.criticality ?? "").trim());
}

function hasCriticality(row) {
    if (!hasAssetListClassification(row)) return false;
    const normalized = normalizeClassification(row?.criticality);
    if (["", "unclassified", "unmapped"].includes(normalized)) return false;
    return Boolean(String(row?.criticality || "").trim() || row?.is_critical === true || row?.is_production_critical === true);
}

function isProductionCritical(row) {
    if (!hasCriticality(row)) return false;
    const criticality = normalizeClassification(row?.criticality);
    return PRODUCTION_CRITICAL_LABELS.has(criticality) || row?.is_production_critical === true || row?.is_critical === true;
}

function isNonProductionClassification(row) {
    const criticality = normalizeClassification(row?.criticality);
    return NON_PRODUCTION_LABELS.has(criticality);
}

function getWorkOrderStartTime(row) {
    return row?.start_time || row?.actual_start_time || row?.maintenance_start_time;
}

function getWorkOrderEndTime(row) {
    return row?.end_time || row?.actual_end_time || row?.maintenance_end_time || row?.latest_event_time;
}

function normalizeFieldKey(value) {
    return String(value || "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

function getRowFieldByAliases(row, aliases) {
    if (!row) return { value: null, field: "" };
    const fieldMap = new Map();
    Object.keys(row).forEach((key) => fieldMap.set(normalizeFieldKey(key), key));
    for (const alias of aliases) {
        const actualKey = fieldMap.get(normalizeFieldKey(alias));
        if (!actualKey) continue;
        const value = row[actualKey];
        if (value === null || value === undefined || String(value).trim() === "") {
            continue;
        }
        return { value, field: actualKey };
    }
    return { value: null, field: "" };
}

function getRowFieldByAliasesRaw(row, aliases) {
    if (!row) return { value: null, field: "", hasField: false };
    const fieldMap = new Map();
    Object.keys(row).forEach((key) => fieldMap.set(normalizeFieldKey(key), key));
    for (const alias of aliases) {
        const actualKey = fieldMap.get(normalizeFieldKey(alias));
        if (!actualKey) continue;
        return { value: row[actualKey], field: actualKey, hasField: true };
    }
    return { value: null, field: "", hasField: false };
}

function parseMrDateField(row, aliases, { ignoreForOpenStatus = false } = {}) {
    if (ignoreForOpenStatus && isOpenLifecycleState(row?.request_state || row?.status || row?.lifecycle_state)) {
        return { date: null, field: "", invalid: false, missing: true };
    }
    const { value, field } = getRowFieldByAliases(row, aliases);
    if (value === null || value === undefined || String(value).trim() === "") {
        return { date: null, field: "", invalid: false, missing: true };
    }
    const date = parseDateValue(value);
    return { date, field, invalid: !date, missing: false, raw: value };
}

function getMrWorkOrderId(row, index = "") {
    return String(
        row?.maintenance_order_id
        || row?.mr_id
        || row?.request_id
        || row?.work_order_id
        || row?.wo_id
        || (index !== "" ? `row-${index}` : "")
    ).trim();
}

function getMrMachineName(row) {
    return String(
        row?.machine_name_display
        || row?.asset_display_name
        || row?.machine_name
        || row?.asset_name
        || row?.machine_group
        || "--"
    ).trim();
}

function getMrStatus(row) {
    return String(row?.request_state || row?.status || row?.lifecycle_state || "--").trim();
}

function getMrRequestId(row, index = "") {
    const value = String(row?.maintenance_order_id || row?.request_id || row?.mr_id || "").trim();
    return value && value !== "--" ? value : (index !== "" ? `row-${index}` : "");
}

function getMrWorkOrderOnlyId(row) {
    const value = String(row?.work_order_id || row?.wo_id || "").trim();
    return value && value !== "--" ? value : "";
}

function getMrServiceLevel(row) {
    return String(row?.service_level ?? row?.priority ?? row?.severity ?? "").trim() || "Unassigned";
}

function getMrStartedBy(row) {
    return String(row?.started_by || row?.startedBy || "").trim() || "--";
}

function getMrCreatedBy(row) {
    return String(row?.created_by || row?.createdBy || "").trim() || "--";
}

function isMrNewStatus(status) {
    return normalizeClassification(status) === "new";
}

function isMrInProgressStatus(status) {
    return normalizeClassification(status).replace(/\s+/g, "") === "inprogress";
}

function isMrFinishedStatus(status) {
    return normalizeClassification(status) === "finished";
}

function isMrReviewStatus(status) {
    return ["confirm", "rework", "re work", "rejected", "reject"].includes(normalizeClassification(status));
}

function isMrRejectedStatus(status) {
    return ["rejected", "reject", "cancelled", "canceled", "cancel"].includes(normalizeClassification(status));
}

function isNormalOpenMrStatus(status) {
    return isMrNewStatus(status) || isMrInProgressStatus(status);
}

function getAcknowledgementStatus(row) {
    const explicit = String(row?.acknowledgement_status || row?.acknowledgment_status || "").trim();
    if (explicit) return explicit;
    const status = getMrStatus(row);
    const workOrderId = getMrWorkOrderOnlyId(row);
    if (isMrNewStatus(status) && !workOrderId) return "Not Acknowledged";
    if (isMrInProgressStatus(status) && workOrderId) return "Acknowledged / In Progress";
    if (isMrFinishedStatus(status)) return "Closed";
    return "Review";
}

function getDataQualityFlags(row) {
    // A confirmed correction (Data Review) makes the row count as valid for KPIs.
    if (dataReviewCorrectionResolvedFor(row)) return ["Valid"];
    const rawFlags = row?.data_quality_flags;
    if (Array.isArray(rawFlags) && rawFlags.length) return rawFlags.map((flag) => String(flag || "").trim()).filter(Boolean);
    const single = String(row?.data_quality_flag || "").trim();
    if (!single) return ["Valid"];
    return single.split(";").map((flag) => flag.trim()).filter(Boolean);
}

function getDataQualityFlag(row) {
    const flags = getDataQualityFlags(row);
    return flags.length === 1 ? flags[0] : flags.join("; ");
}

function isDataQualityValid(row) {
    const flags = getDataQualityFlags(row);
    return flags.length === 1 && flags[0] === "Valid";
}

function getMrDefaultYear(meta = {}, rows = []) {
    const period = meta.period || document.getElementById("period-select")?.value || "ytd";
    const preferredDate = (
        period === "previous_year" || period === "this_month" || period === "last_month" || period === "custom"
            ? (meta.period_start || meta.reference_end || meta.period_end)
            : (meta.period_end || meta.reference_end || meta.period_start)
    );
    const parsed = parseDateValue(preferredDate);
    if (period !== "all_years" && parsed) return String(getMrFinancialYearStart(parsed));

    const years = getMrAvailableYears(rows);
    if (years.length) return String(Math.max(...years));
    const fallback = parseDateValue(meta.reference_end || meta.period_end || meta.period_start);
    return fallback ? String(getMrFinancialYearStart(fallback)) : String(getMrFinancialYearStart(new Date()));
}

function getMrAvailableYears(rows) {
    const years = new Set();
    (rows || []).forEach((row) => {
        const raised = parseMrDateField(row, MR_RAISED_DATE_ALIASES);
        const finished = parseMrDateField(row, MR_FINISHED_DATE_ALIASES, { ignoreForOpenStatus: true });
        if (raised.date) years.add(getMrFinancialYearStart(raised.date));
        if (finished.date) years.add(getMrFinancialYearStart(finished.date));
    });
    return [...years].sort((a, b) => b - a);
}

function getFallbackAllWorkOrderRows() {
    const rowMap = new Map();
    getWorkOrderRows(getManagement()).forEach((row, index) => rowMap.set(getExportWorkOrderKey(row, index), row));
    if (getSelectedDowntimeStage() !== DOWNTIME_STAGE_ALL) {
        return [...rowMap.values()];
    }
    const allCachedPeriods = downtimeCachePayload?.payloads || {};
    Object.keys(allCachedPeriods).forEach((key) => {
        (allCachedPeriods[key]?.management?.work_orders || []).forEach((row, index) => {
            const rowKey = getExportWorkOrderKey(row, `${key}:${index}`);
            if (!rowMap.has(rowKey)) rowMap.set(rowKey, row);
        });
    });
    return [...rowMap.values()];
}

async function loadAllWorkOrderRowsForMovement() {
    if (allWorkOrderRowsCache) return allWorkOrderRowsCache;
    if (allWorkOrderRowsPromise) return allWorkOrderRowsPromise;
    // Capture generation so a stage change mid-flight does not pollute cache.
    const gen = _stageFetchGeneration;
    allWorkOrderRowsPromise = fetch(buildDowntimeApiUrl({ period: "all_years", work_orders_only: "1" }), { cache: "no-store" })
        .then(async (response) => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const payload = await response.json();
            if (_stageFetchGeneration !== gen) throw new Error("stage-changed");
            const rows = getWorkOrderRows(payload.management);
            if (!rows.length) throw new Error("No all-year work orders found");
            allWorkOrderRowsCache = rows;
            return rows;
        })
        .catch((error) => {
            if (String(error.message).includes("stage-changed")) {
                // Stage changed while this fetch was in flight — discard silently.
                // The new stage's fetch will be started separately.
                allWorkOrderRowsCache = null;
                return [];
            }
            console.warn("All-year MR movement source unavailable, falling back to loaded rows:", error);
            const rows = getFallbackAllWorkOrderRows();
            if (_stageFetchGeneration === gen) allWorkOrderRowsCache = rows;
            return rows;
        })
        .finally(() => {
            allWorkOrderRowsPromise = null;
        });
    return allWorkOrderRowsPromise;
}

function syncMrMovementYearToPeriod(rows = []) {
    if (mrMovementUserSelectedYear && mrMovementSelectedYear) return;
    mrMovementSelectedYear = getMrDefaultYear(downtimePayload?.meta || {}, rows);
}

function populateMrMovementYearOptions(rows = []) {
    const select = document.getElementById("mr-movement-year");
    if (!select) return;
    const years = getMrAvailableYears(rows);
    const current = mrMovementSelectedYear || getMrDefaultYear(downtimePayload?.meta || {}, rows);
    const safeYears = years.length ? years : [Number(current) || getMrFinancialYearStart(new Date())];
    select.innerHTML = safeYears.map((year) => `<option value="${escapeHtml(year)}">${escapeHtml(getMrFinancialYearLabel(year))}</option>`).join("");
    if (safeYears.map(String).includes(String(current))) {
        select.value = String(current);
        mrMovementSelectedYear = String(current);
    } else {
        select.value = String(safeYears[0]);
        mrMovementSelectedYear = String(safeYears[0]);
    }
}

function classifyMrMovementRow(row, selectedYear, index) {
    const raised = parseMrDateField(row, MR_RAISED_DATE_ALIASES);
    const status = getMrStatus(row);
    const rawFinished = parseMrDateField(row, MR_FINISHED_DATE_ALIASES, { ignoreForOpenStatus: true });
    const finished = isMrFinishedStatus(status)
        ? rawFinished
        : { date: null, field: rawFinished.field || "", invalid: false, missing: true, raw: rawFinished.raw };
    const raisedYear = raised.date ? getMrFinancialYearStart(raised.date) : null;
    const finishedYear = finished.date ? getMrFinancialYearStart(finished.date) : null;
    const { start: selectedStart, end: selectedEnd } = getMrFinancialYearRange(selectedYear);
    const raisedInSelectedYear = raised.date ? isDateInMrFinancialYear(raised.date, selectedYear) : false;
    const finishedInSelectedYear = finished.date ? isDateInMrFinancialYear(finished.date, selectedYear) : false;
    const raisedBeforeSelectedYear = raised.date ? raised.date < selectedStart : false;
    const finishedAfterSelectedYear = finished.date ? finished.date >= selectedEnd : false;
    const reviewOnlyStatus = isMrReviewStatus(status) || (!isNormalOpenMrStatus(status) && !isMrFinishedStatus(status));
    if (reviewOnlyStatus) {
        return {
            row,
            index,
            raised,
            finished,
            raisedYear,
            finishedYear,
            invalidDate: false,
            relevant: false,
            type: "review_status",
            label: "Review Status",
        };
    }
    const hasInvalidDate = raised.invalid || (isMrFinishedStatus(status) && (finished.invalid || !finished.date));
    if (!raised.date || hasInvalidDate) {
        return {
            row,
            index,
            raised,
            finished,
            raisedYear,
            finishedYear,
            invalidDate: true,
            relevant: false,
            type: "invalid_date",
            label: "Invalid Date",
        };
    }

    let type = "";
    let label = "";
    if (raisedInSelectedYear && finishedInSelectedYear) {
        type = "opened_closed_same_year";
        label = "Opened and Closed Same Financial Year";
    } else if (raisedBeforeSelectedYear && finishedInSelectedYear) {
        type = "carryover_closed";
        label = "Carry-over Closed in Selected Financial Year";
    } else if (raisedInSelectedYear && isNormalOpenMrStatus(status)) {
        type = "raised_this_year_open";
        label = "Raised This Financial Year Still Open";
    } else if (raisedBeforeSelectedYear && isNormalOpenMrStatus(status)) {
        type = "previous_year_open";
        label = "Previous Financial-Year Still Open";
    } else if (raisedInSelectedYear && finishedAfterSelectedYear) {
        type = "raised_this_year_finished_later";
        label = "Raised This Financial Year Finished Later";
    } else {
        type = "not_relevant";
        label = "Outside Selected Financial Year";
    }

    return {
        row,
        index,
        raised,
        finished,
        raisedYear,
        finishedYear,
        invalidDate: false,
        relevant: type !== "not_relevant",
        type,
        label,
    };
}

function buildMrMovementModel(rows = [], selectedYearValue) {
    const selectedYear = Number(selectedYearValue || mrMovementSelectedYear || getMrDefaultYear(downtimePayload?.meta || {}, rows));
    const { start: selectedStart } = getMrFinancialYearRange(selectedYear);
    const monthlyRaised = new Array(12).fill(0);
    const monthlyFinished = new Array(12).fill(0);
    const classified = rows.map((row, index) => classifyMrMovementRow(row, selectedYear, index));
    const validRows = classified.filter((item) => !item.invalidDate && item.raised.date);
    const invalidDateCount = classified.filter((item) => item.invalidDate).length;
    let raisedCount = 0;
    let finishedCount = 0;
    let carryoverClosed = 0;
    let raisedOpen = 0;
    let previousOpen = 0;
    let selectedYearRaisedClosed = 0;
    let raisedRejectedCount = 0;
    let openingBacklog = 0;

    validRows.forEach((item) => {
        // MR demand and MR completion are intentionally counted from separate date perspectives.
        const itemStatus = getMrStatus(item.row);
        const itemIsOpen = isNormalOpenMrStatus(itemStatus);
        const itemIsFinished = isMrFinishedStatus(itemStatus);
        const itemIsRejected = isMrRejectedStatus(itemStatus);
        if (isDateInMrFinancialYear(item.raised.date, selectedYear)) {
            raisedCount += 1;
            monthlyRaised[item.raised.date.getMonth()] += 1;
            if (itemIsFinished && item.finished.date) selectedYearRaisedClosed += 1;
            if (itemIsRejected) raisedRejectedCount += 1;
        }
        if (itemIsFinished && isDateInMrFinancialYear(item.finished.date, selectedYear)) {
            finishedCount += 1;
            monthlyFinished[item.finished.date.getMonth()] += 1;
        }
        if (item.raised.date < selectedStart && itemIsFinished && isDateInMrFinancialYear(item.finished.date, selectedYear)) carryoverClosed += 1;
        if (isDateInMrFinancialYear(item.raised.date, selectedYear) && itemIsOpen) raisedOpen += 1;
        if (item.raised.date < selectedStart && itemIsOpen) previousOpen += 1;
        if (item.raised.date < selectedStart && (itemIsOpen || (itemIsFinished && item.finished.date && item.finished.date >= selectedStart))) {
            openingBacklog += 1;
        }
    });

    const closingBacklog = previousOpen + raisedOpen;
    const relevantRows = classified.filter((item) => item.relevant);
    // Closure rate excludes rejected/cancelled from denominator — they were never
    // worked on and skew the metric down when all open MR have genuinely been resolved.
    const validDenominator = raisedCount - raisedRejectedCount;
    return {
        selectedYear,
        financialYearLabel: getMrFinancialYearLabel(selectedYear),
        rows,
        classified,
        relevantRows,
        invalidDateCount,
        raisedCount,
        finishedCount,
        carryoverClosed,
        raisedOpen,
        previousOpen,
        selectedYearRaisedClosed,
        raisedRejectedCount,
        closureRate: validDenominator > 0 ? (selectedYearRaisedClosed / validDenominator) * 100 : null,
        resolutionRate: raisedCount > 0 ? ((selectedYearRaisedClosed + raisedRejectedCount) / raisedCount) * 100 : null,
        rejectionRate: raisedCount > 0 ? (raisedRejectedCount / raisedCount) * 100 : null,
        monthlyRaised,
        monthlyFinished,
        openingBacklog,
        closingBacklog,
    };
}

function cleanMrValue(value) {
    const text = String(value ?? "").trim();
    return text && text !== "--" ? text : "";
}

function parseMrTrackingDateField(row, aliases) {
    const raw = getRowFieldByAliasesRaw(row, aliases);
    if (!raw.hasField || raw.value === null || raw.value === undefined || String(raw.value).trim() === "") {
        return { date: null, field: raw.field || "", invalid: false, missing: true, raw: raw.value ?? "" };
    }
    const date = parseDateValue(raw.value);
    return { date, field: raw.field, invalid: !date, missing: false, raw: raw.value };
}

function getMrTrackingText(row, aliases, fallback = "") {
    const raw = getRowFieldByAliasesRaw(row, aliases);
    const text = cleanMrValue(raw.value);
    return text || fallback;
}

function normalizeMrSeverity(value, row = {}) {
    const text = cleanMrValue(value);
    const normalized = normalizeClassification(text);
    if (!normalized) return "Unclassified";
    const numeric = Number(text);
    if (Number.isFinite(numeric)) return priorityToSeverity(numeric).replace("Unknown", "Unclassified");
    const priorityMatch = normalized.match(/\b(?:p|priority|prio)\s*([1-4])\b/) || normalized.match(/\b([1-4])\b/);
    if (priorityMatch) return priorityToSeverity(Number(priorityMatch[1])).replace("Unknown", "Unclassified");
    if (normalized.includes("urgent") || normalized.includes("emergency") || normalized.includes("breakdown")) return "Critical";
    if (normalized.includes("high")) return "High";
    if (normalized.includes("medium") || normalized.includes("normal") || normalized.includes("moderate")) return "Medium";
    if (normalized.includes("low") || normalized.includes("minor")) return "Low";
    if ((normalized.includes("non") && normalized.includes("critical")) || normalized.includes("facility")) return "Low";
    if (normalized.includes("support")) return "Medium";
    if (normalized.includes("critical") || normalized.includes("semi")) return "Critical";
    if (row?.is_critical === true || row?.is_production_critical === true) return "Critical";
    return "Unclassified";
}

function formatMrSeverityCode(value, row = {}) {
    const rawText = cleanMrValue(value).toUpperCase();
    const directCodeMatch = rawText.match(/\bS([1-4])\b/);
    if (directCodeMatch) return `S${directCodeMatch[1]}`;
    const normalized = normalizeMrSeverity(value, row);
    return ({
        Critical: "S1",
        High: "S2",
        Medium: "S3",
        Low: "S4",
    })[normalized] || normalized;
}

function getSlaTargetByKey(key) {
    return WORK_ORDER_SLA_TARGETS.find((target) => target.key === key) || null;
}

function matchWorkOrderSlaSeverity(value) {
    const text = cleanMrValue(value);
    if (!text) return null;
    const numeric = Number(text);
    if (Number.isFinite(numeric)) {
        if (numeric <= 1) return getSlaTargetByKey("S1");
        if (numeric === 2) return getSlaTargetByKey("S2");
        if (numeric === 3) return getSlaTargetByKey("S3");
        return getSlaTargetByKey("S4");
    }
    const normalized = normalizeClassification(text);
    const codedMatch = normalized.match(/\b(?:s|sl|severity|sev|priority|prio|p)\s*([1-4])\b/) || normalized.match(/\b([1-4])\b/);
    if (codedMatch) return getSlaTargetByKey(`S${codedMatch[1]}`);
    if (normalized.includes("urgent") || normalized.includes("emergency") || normalized.includes("breakdown")) return getSlaTargetByKey("S1");
    if (normalized.includes("critical")) return getSlaTargetByKey("S1");
    if (normalized.includes("high")) return getSlaTargetByKey("S2");
    if (normalized.includes("medium") || normalized.includes("normal") || normalized.includes("moderate")) return getSlaTargetByKey("S3");
    if (normalized.includes("low") || normalized.includes("minor")) return getSlaTargetByKey("S4");
    return null;
}

function getWorkOrderSlaSeverity(row = {}) {
    const candidates = [
        row?.priority,
        row?.service_level,
        row?.severity,
        row?.priority_label,
        row?.Severity,
        normalizeMrSeverity(row?.service_level ?? row?.priority ?? row?.severity, row),
    ];
    for (const candidate of candidates) {
        const matched = matchWorkOrderSlaSeverity(candidate);
        if (matched) return matched;
    }
    if (row?.is_critical === true || row?.is_production_critical === true) return getSlaTargetByKey("S1");
    return WORK_ORDER_SLA_UNCLASSIFIED;
}

function getWorkOrderSlaCreatedDate(row) {
    return parseMrDateField(row, SLA_CREATED_DATE_ALIASES);
}

function getWorkOrderSlaStartDate(row) {
    return parseMrDateField(row, SLA_START_DATE_ALIASES);
}

function getWorkOrderSlaEndDate(row) {
    return parseMrDateField(row, SLA_END_DATE_ALIASES);
}

function isWorkOrderSlaFinished(statusValue, row = null) {
    const normalized = normalizeClassification(statusValue);
    if (["finished", "completed", "complete", "closed", "resolved", "done"].includes(normalized)) return true;
    if (!normalized && parseDateValue(row?.actual_end_time || row?.maintenance_end_time || row?.end_time)) return true;
    return false;
}

function getWorkOrderSlaReferenceDate() {
    const meta = downtimePayload?.meta || {};
    return parseDateValue(meta.period_end || meta.reference_end || meta.last_synced) || new Date();
}

function getDurationHours(start, end) {
    if (!(start instanceof Date) || Number.isNaN(start.getTime())) return null;
    if (!(end instanceof Date) || Number.isNaN(end.getTime())) return null;
    return (end.getTime() - start.getTime()) / 3600000;
}

function formatSlaDuration(hours, { signed = false } = {}) {
    const numeric = Number(hours);
    if (!Number.isFinite(numeric)) return "--";
    const sign = signed && numeric > 0 ? "+" : (signed && numeric < 0 ? "-" : "");
    const absolute = Math.abs(numeric);
    if (absolute < 1) {
        const minutes = Math.max(1, Math.round(absolute * 60));
        return `${sign}${minutes} min`;
    }
    if (absolute < 48) {
        const roundedHours = absolute >= 10 ? Math.round(absolute) : Math.round(absolute * 10) / 10;
        return `${sign}${roundedHours} hr`;
    }
    const days = absolute / 24;
    const roundedDays = days >= 10 ? Math.round(days) : Math.round(days * 10) / 10;
    return `${sign}${roundedDays} day${roundedDays === 1 ? "" : "s"}`;
}

function getSlaMetricTone(actualHours, targetHours) {
    const actual = Number(actualHours);
    const target = Number(targetHours);
    if (!Number.isFinite(actual) || !Number.isFinite(target) || target <= 0) return "missing";
    if (actual > target) return "bad";
    if (actual >= target * 0.85) return "warn";
    return "good";
}

function getSlaStatusTone(status) {
    if (status === "Met Target") return "met";
    if (status === "Late" || status === "Open Overdue") return "late";
    if (status === "Missing Start Date" || status === "Missing End Date" || status === "Missing Data" || status === "No SLA Target") return "missing";
    return "warning";
}

function isWorkOrderSlaMissingStatus(status) {
    return status === "Missing Start Date" || status === "Missing End Date" || status === "Missing Data" || status === "No SLA Target";
}

function formatSlaTargetLabel(target) {
    if (!target || target.key === WORK_ORDER_SLA_UNCLASSIFIED.key) return "No SLA target";
    const parts = [];
    if (target.responseTargetHours !== null) parts.push(`Response <= ${formatSlaDuration(target.responseTargetHours)}`);
    if (target.completionTargetHours !== null) parts.push(`Completion <= ${formatSlaDuration(target.completionTargetHours)}`);
    return parts.length ? parts.join("; ") : "No SLA target";
}

function logWorkOrderSlaWarning(key, message) {
    if (workOrderSlaWarningKeys.has(key)) return;
    workOrderSlaWarningKeys.add(key);
    console.warn(message);
}

function warnWorkOrderSlaFieldAvailability(rows = []) {
    if (!rows.length) return;
    if (!rows.some((row) => getWorkOrderSlaCreatedDate(row).date)) {
        logWorkOrderSlaWarning(
            "sla-created-date-missing",
            "Work Order SLA: no usable Created Date field was found in the loaded work-order rows. Affected records will be reported under Missing Data."
        );
    }
    if (!rows.some((row) => getRowFieldByAliasesRaw(row, MR_SEVERITY_ALIASES).hasField || row?.priority !== undefined)) {
        logWorkOrderSlaWarning(
            "sla-severity-missing",
            "Work Order SLA: no Priority/Severity field was found in the loaded work-order rows. Affected records will be reported as Unclassified or Missing Data."
        );
    }
    if (!rows.some((row) => getWorkOrderSlaStartDate(row).date || getWorkOrderSlaEndDate(row).date)) {
        logWorkOrderSlaWarning(
            "sla-actual-dates-missing",
            "Work Order SLA: no usable Actual Start or Actual End dates were found in the loaded work-order rows. Response and completion calculations will fall back to Missing Data where required."
        );
    }
}

function getMrTrackingUniqueKey(row, index) {
    const id = getMrTrackingText(row, MR_ID_ALIASES, "");
    if (id) return { key: `id:${normalizeFieldKey(id)}`, displayId: id, hasMrId: true };
    const equipment = getMrTrackingText(row, MR_EQUIPMENT_ALIASES, "");
    const raised = getRowFieldByAliasesRaw(row, MR_RAISED_DATE_ALIASES).value;
    const remarks = getMrTrackingText(row, MR_REMARKS_ALIASES, "");
    const fallback = [equipment, raised, remarks].map((part) => normalizeFieldKey(part)).join("|");
    return {
        key: fallback.replace(/\|/g, "") ? `fallback:${fallback}` : `row:${index}`,
        displayId: `MR-${index + 1}`,
        hasMrId: false,
    };
}

function isMrClosedStatus(status) {
    return isMrFinishedStatus(status);
}

function isMrOpenTracking(item) {
    return isNormalOpenMrStatus(item.status);
}

function isPositiveAckStatus(status) {
    const normalized = normalizeClassification(status);
    return ["yes", "true", "acknowledged", "acknowledge", "ack", "accepted", "received", "responded", "assigned"].includes(normalized)
        || normalized.includes("acknowledged")
        || normalized.includes("accepted")
        || normalized.includes("responded");
}

function isMrAcknowledged(item) {
    const normalized = normalizeClassification(item.ackStatus);
    if (normalized === "not acknowledged") return false;
    if (["acknowledged in progress", "acknowledged", "closed"].includes(normalized)) return true;
    return isPositiveAckStatus(item.ackStatus);
}

function isMrOutstandingAcknowledgement(item) {
    return isMrNewStatus(item.status) && !getMrWorkOrderOnlyId(item.row) && !isMrAcknowledged(item);
}

function normalizeMrTrackingRows(rows = []) {
    const rowMap = new Map();
    let duplicateCount = 0;
    const hasAckStatusField = rows.some((row) => getRowFieldByAliasesRaw(row, MR_ACK_STATUS_ALIASES).hasField);
    const hasAckDateField = rows.some((row) => getRowFieldByAliasesRaw(row, MR_ACK_DATE_ALIASES).hasField);
    rows.forEach((row, index) => {
        const keyInfo = getMrTrackingUniqueKey(row, index);
        if (rowMap.has(keyInfo.key)) {
            duplicateCount += 1;
            return;
        }
        const equipmentRaw = getMrTrackingText(row, MR_EQUIPMENT_ALIASES, "");
        const assetId = getMrTrackingText(row, MR_ASSET_ID_ALIASES, "");
        const raised = parseMrTrackingDateField(row, MR_RAISED_DATE_ALIASES);
        const finished = parseMrTrackingDateField(row, MR_FINISHED_DATE_ALIASES);
        const ackDate = parseMrTrackingDateField(row, MR_ACK_DATE_ALIASES);
        const severityRawField = getRowFieldByAliasesRaw(row, MR_SEVERITY_ALIASES);
        const ackStatusField = getRowFieldByAliasesRaw(row, MR_ACK_STATUS_ALIASES);
        const status = getMrTrackingText(row, MR_STATUS_ALIASES, "Data not available");
        const severity = normalizeMrSeverity(severityRawField.value, row);
        rowMap.set(keyInfo.key, {
            row,
            index,
            key: keyInfo.key,
            id: keyInfo.displayId,
            hasMrId: keyInfo.hasMrId,
            equipment: equipmentRaw || "Data not available",
            equipmentKey: equipmentRaw,
            assetId: assetId || "Data not available",
            raised,
            finished,
            ackDate,
            ackStatus: getAcknowledgementStatus(row),
            hasAckStatusField: ackStatusField.hasField,
            severity,
            severityRaw: cleanMrValue(severityRawField.value),
            hasSeverityField: severityRawField.hasField,
            status,
            criticality: row?.criticality || row?.normalized_criticality || "Unmapped",
            startedBy: getMrStartedBy(row),
            createdBy: getMrCreatedBy(row),
            assignedTo: getMrStartedBy(row) !== "--" ? getMrStartedBy(row) : getMrTrackingText(row, MR_ASSIGNED_ALIASES, "Data not available"),
            remarks: getMrTrackingText(row, MR_REMARKS_ALIASES, "Data not available"),
            missingEquipment: !equipmentRaw,
            missingSeverity: !cleanMrValue(severityRawField.value),
        });
        const item = rowMap.get(keyInfo.key);
    });
    const items = [...rowMap.values()];
    return {
        rawCount: rows.length,
        items,
        duplicateCount,
        hasAckTracking: hasAckStatusField || hasAckDateField,
        hasAckStatusField,
        hasAckDateField,
        quality: {
            missingRaisedDate: items.filter((item) => item.raised.missing).length,
            missingEquipment: items.filter((item) => item.missingEquipment).length,
            missingSeverity: items.filter((item) => item.missingSeverity).length,
            missingAckStatus: items.filter((item) => !cleanMrValue(item.ackStatus)).length,
            invalidDate: items.filter((item) => item.raised.invalid || item.finished.invalid || item.ackDate.invalid).length,
        },
    };
}

function getMrTrackingSelectedYear(rows = []) {
    const fallbackYear = getMrDefaultYear(downtimePayload?.meta || {}, rows);
    const availableYears = getMrAvailableYears(rows);
    if (!mrTrackingSelectedYear) mrTrackingSelectedYear = String(fallbackYear);
    if (mrTrackingSelectedYear === "all") return "all";
    if (availableYears.length && !availableYears.map(String).includes(String(mrTrackingSelectedYear))) {
        mrTrackingSelectedYear = String(availableYears[0]);
    }
    return Number(mrTrackingSelectedYear || fallbackYear);
}

function getMrTrackingDefaultMonth(items, selectedYear) {
    const meta = downtimePayload?.meta || {};
    const period = meta.period || document.getElementById("period-select")?.value || "";
    const periodDate = parseDateValue(meta.period_start || meta.reference_end || meta.period_end);
    if (selectedYear !== "all" && ["this_month", "last_month", "custom"].includes(period) && periodDate && isDateInMrFinancialYear(periodDate, selectedYear)) {
        return String(periodDate.getMonth() + 1);
    }
    return "all";
}

function getMrTrackingYearLabel(selectedYear) {
    return selectedYear === "all" ? "All years" : getMrFinancialYearLabel(selectedYear);
}

function getMrTrackingMonthLabel(selectedMonth) {
    if (selectedMonth === "all") return "All months";
    const monthNumber = Number(selectedMonth);
    return MR_MONTH_LABELS[monthNumber - 1] || "Selected month";
}

function getMrTrackingScopeLabel(selectedYear, selectedMonth) {
    const yearLabel = getMrTrackingYearLabel(selectedYear);
    const monthLabel = getMrTrackingMonthLabel(selectedMonth);
    if (selectedYear === "all" && selectedMonth === "all") return "All months across all years";
    if (selectedYear === "all") return `${monthLabel} across all years`;
    if (selectedMonth === "all") return `All months in ${yearLabel}`;
    return `${monthLabel} in ${yearLabel}`;
}

function populateMrTrackingControls(items, selectedYear, rows = []) {
    const equipmentSelect = document.getElementById("mr-tracking-equipment");
    if (equipmentSelect) {
        const equipmentNames = [...new Set(items.map((item) => item.equipmentKey).filter(Boolean))].sort((a, b) => a.localeCompare(b));
        const current = mrTrackingEquipmentFilter;
        equipmentSelect.innerHTML = `<option value="all">All Equipment</option>` + equipmentNames.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
        mrTrackingEquipmentFilter = current === "all" || equipmentNames.includes(current) ? current : "all";
        equipmentSelect.value = mrTrackingEquipmentFilter;
    }
    const yearSelect = document.getElementById("mr-tracking-year");
    if (yearSelect) {
        const years = getMrAvailableYears(rows);
        const fallbackYear = getMrDefaultYear(downtimePayload?.meta || {}, rows);
        if (!mrTrackingSelectedYear) mrTrackingSelectedYear = String(fallbackYear);
        if (mrTrackingSelectedYear !== "all" && years.length && !years.map(String).includes(String(mrTrackingSelectedYear))) {
            mrTrackingSelectedYear = String(years[0]);
        }
        yearSelect.innerHTML = `<option value="all">All Years</option>` +
            years.map((year) => `<option value="${escapeHtml(year)}">${escapeHtml(getMrFinancialYearLabel(year))}</option>`).join("");
        if (!years.length) mrTrackingSelectedYear = "all";
        yearSelect.value = mrTrackingSelectedYear === "all" ? "all" : String(mrTrackingSelectedYear || fallbackYear);
    }
    const monthSelect = document.getElementById("mr-tracking-month");
    if (monthSelect) {
        if (!mrTrackingSelectedMonth) mrTrackingSelectedMonth = getMrTrackingDefaultMonth(items, selectedYear);
        monthSelect.innerHTML = `<option value="all">All Months</option>` +
            MR_FINANCIAL_MONTH_ORDER.map((monthIndex) => `<option value="${monthIndex + 1}">${escapeHtml(MR_MONTH_LABELS[monthIndex])}</option>`).join("");
        if (!Array.from(monthSelect.options).some((option) => option.value === String(mrTrackingSelectedMonth))) {
            mrTrackingSelectedMonth = getMrTrackingDefaultMonth(items, selectedYear);
        }
        monthSelect.value = String(mrTrackingSelectedMonth);
    }
}

function filterMrTrackingItems(items, { selectedYear = "all", selectedMonth = "all", applyYear = true, applyMonth = true } = {}) {
    return items.filter((item) => {
        if (mrTrackingEquipmentFilter !== "all" && item.equipmentKey !== mrTrackingEquipmentFilter) return false;
        const needsYearMatch = applyYear && selectedYear !== "all";
        const needsMonthMatch = applyMonth && selectedMonth !== "all";
        if ((needsYearMatch || needsMonthMatch) && !item.raised.date) return false;
        if (needsYearMatch && !isDateInMrFinancialYear(item.raised.date, selectedYear)) return false;
        if (needsMonthMatch && item.raised.date.getMonth() + 1 !== Number(selectedMonth)) return false;
        return true;
    });
}

function countMrBySeverity(items) {
    return ["Critical", "High", "Medium", "Low", "Unclassified"].reduce((acc, severity) => {
        acc[severity] = items.filter((item) => item.severity === severity).length;
        return acc;
    }, {});
}

function getMonthlyMrCounts(items, selectedYear = "all") {
    const counts = new Array(12).fill(0);
    items.forEach((item) => {
        if (!item.raised.date) return;
        if (selectedYear !== "all" && !isDateInMrFinancialYear(item.raised.date, selectedYear)) return;
        counts[item.raised.date.getMonth()] += 1;
    });
    return counts;
}

function getCumulativeCounts(monthlyCounts) {
    let running = 0;
    return monthlyCounts.map((count) => {
        running += count;
        return running;
    });
}

function buildMrTrackingModel(rows = []) {
    const normalized = normalizeMrTrackingRows(rows);
    const selectedYear = getMrTrackingSelectedYear(rows);
    populateMrTrackingControls(normalized.items, selectedYear, rows);
    const selectedMonth = String(mrTrackingSelectedMonth || getMrTrackingDefaultMonth(normalized.items, selectedYear));
    const baseFilteredItems = filterMrTrackingItems(normalized.items, { applyYear: false, applyMonth: false });
    const filteredYear = filterMrTrackingItems(normalized.items, { selectedYear, selectedMonth: "all", applyYear: true, applyMonth: false });
    const filteredAll = filterMrTrackingItems(normalized.items, { selectedYear, selectedMonth, applyYear: true, applyMonth: selectedMonth !== "all" });
    const openItems = filteredAll.filter(isMrOpenTracking);
    const outstandingItems = filteredAll.filter(isMrOutstandingAcknowledgement);
    const raisedThisMonth = selectedMonth === "all" ? filteredYear : filteredAll;
    const criticalRaisedYear = (selectedMonth === "all" ? filteredYear : filteredAll).filter((item) => isProductionCritical(item.row));
    const openBySeverity = countMrBySeverity(openItems);
    const trendItems = selectedMonth === "all" ? filteredYear : filteredAll;
    const monthlyCounts = orderMrCountsByFinancialCalendar(getMonthlyMrCounts(trendItems, selectedYear));
    const cumulativeCounts = getCumulativeCounts(monthlyCounts);
    const machineCounts = [...filteredAll.reduce((map, item) => {
        const label = item.equipmentKey || "Data not available";
        map.set(label, (map.get(label) || 0) + 1);
        return map;
    }, new Map()).entries()]
        .map(([label, count]) => ({ label, count }))
        .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
    const SEVERITY_LEVELS = ["Critical", "High", "Medium", "Low", "Unclassified"];
    const monthlyBySeverity = Object.fromEntries(
        SEVERITY_LEVELS.map((sev) => [sev, orderMrCountsByFinancialCalendar(getMonthlyMrCounts(trendItems.filter((item) => item.severity === sev), selectedYear))])
    );
    return {
        ...normalized,
        selectedYear,
        selectedMonth,
        baseFilteredItems,
        filteredAll,
        filteredYear,
        trendItems,
        openItems,
        outstandingItems,
        raisedThisMonth,
        criticalRaisedYear,
        openBySeverity,
        monthlyCounts,
        cumulativeCounts,
        machineCounts,
        monthlyBySeverity,
    };
}

function renderMrTrackingLoading(message = "Loading MR tracking.") {
    ["mr-ack-outstanding", "mr-open-critical", "mr-open-total", "mr-raised-this-year"].forEach((id) => setText(id, "--"));
    setText("mr-tracking-summary", message);
    const body = document.getElementById("mr-outstanding-body");
    if (body) body.innerHTML = `<tr><td colspan="10" class="empty-cell">${escapeHtml(message)}</td></tr>`;
    ["mrParetoChart", "mrMonthlySeverityChart"].forEach((id) => renderEmptyChart(id, message));
}

function renderMrTrackingSection(rows = allWorkOrderRowsCache) {
    if (!document.getElementById("mr-tracking-equipment")) return;
    if (!rows) {
        renderMrTrackingLoading("Loading all-year MR data.");
        return;
    }
    const model = buildMrTrackingModel(rows);
    const selectedYearLabel = getMrTrackingYearLabel(model.selectedYear);
    setText("mr-raised-year-label", model.selectedYear === "all" ? "Total MR Raised (All Years)" : "Total MR Raised FYTD");
    setText("mr-raised-this-year", fmtNumber(model.filteredYear.length));
    setText("mr-raised-this-year-sub", model.selectedYear === "all" ? "Raised date across all years" : `${selectedYearLabel} raised date`);
    setText("mr-open-total", fmtNumber(model.openItems.length));
    setText("mr-open-critical", fmtNumber(model.openBySeverity.Critical || 0));
    setText("mr-ack-outstanding", fmtNumber(model.outstandingItems.length));
    renderMrTrackingSummary(model);
    renderMrTrackingQuality(model);
    renderMrParetoChart(model);
    renderMrMonthlySeverityChart(model);
    renderMrOutstandingTable(model);
}

function renderMrTrackingSummary(model) {
    const topMachine = model.machineCounts[0];
    const scopeLabel = getMrTrackingScopeLabel(model.selectedYear, model.selectedMonth);
    setText(
        "mr-tracking-summary",
        `${fmtNumber(model.filteredYear.length)} MR raised in ${scopeLabel.toLowerCase()}. ${fmtNumber(model.openItems.length)} open — ${fmtNumber(model.openBySeverity.Critical || 0)} critical. ${fmtNumber(model.outstandingItems.length)} awaiting acknowledgement. Highest volume: ${topMachine ? topMachine.label : "Data not available"} (${fmtNumber(topMachine?.count || 0)}).`
    );
}

function renderMrTrackingQuality(model) {
    const node = document.getElementById("mr-tracking-quality");
    if (!node) return;
    const parts = [];
    if (!model.items.length) parts.push("No imported MR/work-order rows are available.");
    if (model.quality.missingRaisedDate) parts.push(`${fmtNumber(model.quality.missingRaisedDate)} missing raised date.`);
    if (model.quality.invalidDate) parts.push(`${fmtNumber(model.quality.invalidDate)} invalid date format.`);
    if (model.quality.missingEquipment) parts.push(`${fmtNumber(model.quality.missingEquipment)} missing equipment name.`);
    if (model.quality.missingSeverity) parts.push(`${fmtNumber(model.quality.missingSeverity)} missing severity.`);
    if (model.quality.missingAckStatus) parts.push(`${fmtNumber(model.quality.missingAckStatus)} missing acknowledgement status.`);
    if (model.duplicateCount) parts.push(`${fmtNumber(model.duplicateCount)} duplicate MR ID/key skipped.`);
    if (!model.hasAckTracking) parts.push("Dedicated acknowledgement fields are not available; open MR without acknowledgement dates are treated as awaiting acknowledgement.");
    node.textContent = parts.length ? `Data quality: ${parts.join(" ")}` : "";
    node.classList.toggle("hidden", !parts.length);
}


function renderMrParetoChart(model) {
    const canvas = ensureCanvas("mrParetoChart");
    if (!canvas) return;
    destroyChart("mrParetoChart");
    const scopeLabel = getMrTrackingScopeLabel(model.selectedYear, model.selectedMonth);
    if (mrTrackingEquipmentFilter !== "all") {
        const monthlyCounts = orderMrCountsByFinancialCalendar(getMonthlyMrCounts(model.filteredAll, model.selectedYear));
        setText("mr-pareto-chart-note", `MR trend for ${mrTrackingEquipmentFilter} in ${scopeLabel.toLowerCase()}.`);
        if (!monthlyCounts.some(Boolean)) {
            renderEmptyChart("mrParetoChart", `No MR raised for this equipment in ${scopeLabel.toLowerCase()}.`);
            return;
        }
        chartRefs.mrParetoChart = new Chart(canvas.getContext("2d"), {
            type: "line",
            data: {
                labels: MR_FINANCIAL_MONTH_LABELS,
                datasets: [{ label: "MR Raised", data: monthlyCounts, borderColor: "#2563eb", backgroundColor: "rgba(37,99,235,0.12)", fill: true, tension: 0.25, pointRadius: 3 }],
            },
            options: mrTrackingAxisOptions("MR Count"),
        });
        return;
    }
    const topRows = model.machineCounts.slice(0, 10);
    const noteText = model.selectedMonth === "all" && model.selectedYear !== "all"
        ? "Top 10 by MR count with cumulative %."
        : `Top 10 by MR count — ${scopeLabel.toLowerCase()}.`;
    setText("mr-pareto-chart-note", noteText);
    if (!topRows.length) {
        renderEmptyChart("mrParetoChart", "No MR raised by machine for the selected filters.");
        return;
    }
    const total = topRows.reduce((sum, row) => sum + row.count, 0);
    let running = 0;
    const cumulative = topRows.map((row) => {
        running += row.count;
        return total > 0 ? Math.round((running / total) * 100) : 0;
    });
    chartRefs.mrParetoChart = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
            labels: topRows.map((row) => row.label),
            datasets: [
                { type: "bar", label: "MR Raised", data: topRows.map((row) => row.count), backgroundColor: "#3b82f6", borderRadius: 6, maxBarThickness: 28, yAxisID: "y", order: 2 },
                { type: "line", label: "Cumulative %", data: cumulative, borderColor: "#f59e0b", backgroundColor: "#f59e0b", tension: 0.25, yAxisID: "y1", order: 1, pointRadius: 3 },
            ],
        },
        options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { position: "bottom", labels: { usePointStyle: true, boxWidth: 10 } },
                tooltip: {
                    callbacks: {
                        label: (context) => context.datasetIndex === 0
                            ? `MR Raised: ${fmtNumber(context.parsed.x ?? context.raw)}`
                            : `Cumulative: ${context.parsed.x ?? context.raw}%`,
                    },
                },
            },
            scales: {
                x: { beginAtZero: true, ticks: { precision: 0, color: "#475569" }, grid: { color: "rgba(148,163,184,0.18)" } },
                y: { ticks: { color: "#475569", autoSkip: false }, grid: { display: false } },
                y1: { beginAtZero: true, max: 100, position: "right", title: { display: true, text: "Cumulative %", color: "#f59e0b" }, ticks: { precision: 0, color: "#f59e0b", callback: (v) => `${v}%` }, grid: { drawOnChartArea: false } },
            },
        },
    });
}

function renderMrMonthlySeverityChart(model) {
    const canvas = ensureCanvas("mrMonthlySeverityChart");
    if (!canvas) return;
    destroyChart("mrMonthlySeverityChart");
    const scopeLabel = getMrTrackingScopeLabel(model.selectedYear, model.selectedMonth);
    if (!model.monthlyCounts.some(Boolean)) {
        renderEmptyChart("mrMonthlySeverityChart", `No MR raised in ${scopeLabel.toLowerCase()}.`);
        return;
    }
    const SEVERITY_COLORS = { Critical: "#ef4444", High: "#f59e0b", Medium: "#3b82f6", Low: "#10b981", Unclassified: "#64748b" };
    const datasets = Object.entries(model.monthlyBySeverity)
        .filter(([, counts]) => counts.some(Boolean))
        .map(([sev, counts]) => ({
            label: sev,
            data: counts,
            backgroundColor: SEVERITY_COLORS[sev] || "#94a3b8",
            borderRadius: 4,
            maxBarThickness: 34,
            stack: "severity",
        }));
    if (!datasets.length) {
        renderEmptyChart("mrMonthlySeverityChart", "No severity data for the selected filters.");
        return;
    }
    chartRefs.mrMonthlySeverityChart = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: { labels: MR_FINANCIAL_MONTH_LABELS, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { position: "bottom", labels: { usePointStyle: true, boxWidth: 10 } },
                tooltip: { callbacks: { label: (context) => `${context.dataset.label}: ${fmtNumber(context.parsed.y ?? context.raw)}` } },
            },
            scales: {
                x: { stacked: true, grid: { display: false }, ticks: { color: "#475569" } },
                y: { stacked: true, beginAtZero: true, title: { display: true, text: "MR Count" }, ticks: { precision: 0, color: "#475569" }, grid: { color: "rgba(148,163,184,0.18)" } },
            },
        },
    });
}

function mrTrackingAxisOptions(yTitle) {
    return {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: { position: "bottom", labels: { usePointStyle: true, boxWidth: 10 } },
            tooltip: { callbacks: { label: (context) => `${context.dataset.label}: ${fmtNumber(context.parsed.y ?? context.parsed.x ?? context.raw)}` } },
        },
        scales: {
            x: { grid: { display: false }, ticks: { color: "#475569" } },
            y: { beginAtZero: true, title: { display: Boolean(yTitle), text: yTitle }, ticks: { precision: 0, color: "#475569" }, grid: { color: "rgba(148, 163, 184, 0.18)" } },
        },
    };
}

function getMrAcknowledgementDisplay(item, hasAckTracking) {
    if (item.ackStatus) return item.ackStatus;
    if (isMrAcknowledged(item)) return "Acknowledged";
    if (item.ackDate.date) return "Date present / status missing";
    if (!hasAckTracking) return "Pending";
    return "Pending";
}

function getMrAckDaysText(item) {
    if (!item.raised.date) return "Invalid Date";
    const end = item.ackDate.date || new Date();
    const days = Math.max(0, Math.floor((end.getTime() - item.raised.date.getTime()) / 86400000));
    return item.ackDate.date ? `Ack in ${fmtNumber(days)} day${days === 1 ? "" : "s"}` : `${fmtNumber(days)} day${days === 1 ? "" : "s"}`;
}

function getMrWaitingDays(item) {
    if (!item.raised.date) return null;
    const end = item.ackDate.date || new Date();
    return Math.max(0, Math.floor((end.getTime() - item.raised.date.getTime()) / 86400000));
}

function renderMrOutstandingBadges(item) {
    const badges = [];
    const waitingDays = getMrWaitingDays(item);
    if (item.severity === "Critical") badges.push(buildStatusPill("critical", "Critical"));
    badges.push(buildStatusPill("requires_attention", "Awaiting Acknowledgement"));
    if (waitingDays !== null && waitingDays > 30) badges.push(buildStatusPill("critical", "Open > 30 Days"));
    else if (waitingDays !== null && waitingDays > 7) badges.push(buildStatusPill("warning", "Open > 7 Days"));
    if (item.missingSeverity || item.severity === "Unclassified") badges.push(buildStatusPill("warning", "Missing Severity"));
    if (item.missingEquipment) badges.push(buildStatusPill("warning", "Missing Equipment Name"));
    return `<div class="mr-row-badges">${badges.join(" ")}</div>`;
}

function renderMrOutstandingTable(model) {
    const body = document.getElementById("mr-outstanding-body");
    if (!body) return;
    const rows = [...model.outstandingItems].sort((a, b) => {
        // Latest raised date first; severity (Critical first) breaks ties on the same date.
        const ta = a.raised && a.raised.date ? new Date(a.raised.date).getTime() : -Infinity;
        const tb = b.raised && b.raised.date ? new Date(b.raised.date).getTime() : -Infinity;
        if (tb !== ta) return tb - ta;
        const severityOrder = { Critical: 0, High: 1, Medium: 2, Low: 3, Unclassified: 4 };
        return (severityOrder[a.severity] ?? 9) - (severityOrder[b.severity] ?? 9);
    });
    if (!rows.length) {
        body.innerHTML = `<tr><td colspan="10" class="empty-cell">No open MR awaiting acknowledgement for the selected filters.</td></tr>`;
        return;
    }
    body.innerHTML = rows.map((item) => `
        <tr>
            <td>${escapeHtml(item.id)}${renderMrOutstandingBadges(item)}</td>
            <td>${escapeHtml(item.equipment)}</td>
            <td>${escapeHtml(item.assetId)}</td>
            <td>${escapeHtml(item.raised.date ? fmtDateOnly(item.raised.date) : "Invalid Date")}</td>
            <td>${escapeHtml(formatMrSeverityCode(item.severity, item.row))}</td>
            <td>${escapeHtml(getMrAcknowledgementDisplay(item, model.hasAckTracking))}</td>
            <td>${escapeHtml(getMrAckDaysText(item))}</td>
            <td>${escapeHtml(item.status || "Data not available")}</td>
            <td>${escapeHtml(item.startedBy)}</td>
            <td>${escapeHtml(item.createdBy)}</td>
        </tr>
    `).join("");
}

function renderMrMovementLoading(message = "Loading yearly MR movement.") {
    ["mr-raised-selected-year", "mr-finished-selected-year", "mr-carryover-closed", "mr-raised-open", "mr-previous-open", "mr-rejected-count", "mr-closure-rate", "mr-resolution-rate"].forEach((id) => {
        setText(id, "--");
    });
    setText("mr-movement-summary", message);
    const backlogBody = document.getElementById("mr-backlog-body");
    if (backlogBody) backlogBody.innerHTML = `<tr><td colspan="2" class="empty-cell">${escapeHtml(message)}</td></tr>`;
    const carryoverBody = document.getElementById("mr-carryover-body");
    if (carryoverBody) carryoverBody.innerHTML = `<tr><td colspan="9" class="empty-cell">${escapeHtml(message)}</td></tr>`;
    renderEmptyChart("mrMovementChart", message);
    renderMrTrackingLoading(message);
}

function setMrMovementWarning(message) {
    const warning = document.getElementById("mr-movement-warning");
    if (!warning) return;
    if (!message) {
        warning.classList.add("hidden");
        warning.textContent = "";
        return;
    }
    warning.textContent = message;
    warning.classList.remove("hidden");
}

function renderMrMovementSection(rows = allWorkOrderRowsCache) {
    if (!document.getElementById("mr-movement-year")) return;
    if (!rows) {
        syncMrMovementYearToPeriod([]);
        populateMrMovementYearOptions([]);
        renderMrMovementLoading("Loading all-year MR data.");
        return;
    }
    syncMrMovementYearToPeriod(rows);
    populateMrMovementYearOptions(rows);
    const model = buildMrMovementModel(rows, mrMovementSelectedYear);
    setText("mr-raised-selected-year", fmtNumber(model.raisedCount));
    setText("mr-finished-selected-year", fmtNumber(model.finishedCount));
    setText("mr-carryover-closed", fmtNumber(model.carryoverClosed));
    setText("mr-raised-open", fmtNumber(model.raisedOpen));
    setText("mr-previous-open", fmtNumber(model.previousOpen));
    setText("mr-rejected-count", fmtNumber(model.raisedRejectedCount));
    setText("mr-closure-rate", model.closureRate === null ? "N/A" : fmtPercent(model.closureRate));
    setText("mr-resolution-rate", model.resolutionRate === null ? "N/A" : fmtPercent(model.resolutionRate));
    const direction = model.closingBacklog > model.openingBacklog
        ? "increasing"
        : (model.closingBacklog < model.openingBacklog ? "reducing" : "stable");
    const rejectedNote = model.raisedRejectedCount > 0
        ? ` ${fmtNumber(model.raisedRejectedCount)} MR were rejected or cancelled and are excluded from the closure rate.`
        : "";
    setText(
        "mr-movement-summary",
        `In ${model.financialYearLabel}, ${fmtNumber(model.raisedCount)} MR were raised and ${fmtNumber(model.finishedCount)} MR were finished.${rejectedNote} ${fmtNumber(model.carryoverClosed)} completed MR were carry-over from previous financial years, while ${fmtNumber(model.previousOpen)} previous financial-year MR remain open. This indicates whether maintenance backlog is ${direction} during the selected financial year.`
    );
    const warningParts = [];
    if (!rows.length) warningParts.push("No imported work orders are available for yearly MR movement.");
    const hasRaisedDateField = rows.some((row) => getRowFieldByAliases(row, MR_RAISED_DATE_ALIASES).field);
    const hasFinishedDateField = rows.some((row) => getRowFieldByAliases(row, MR_FINISHED_DATE_ALIASES).field);
    if (rows.length && !hasRaisedDateField) warningParts.push("MR raised/start date field is missing, so raised-year movement cannot be calculated.");
    if (rows.length && !hasFinishedDateField) warningParts.push("MR finished/end date field is missing, so finished-year movement cannot be calculated.");
    if (model.invalidDateCount) warningParts.push(`${fmtNumber(model.invalidDateCount)} record(s) have invalid or missing MR movement dates and are excluded from year-based calculations.`);
    setMrMovementWarning(warningParts.join(" "));
    renderMrMovementChart(model);
    renderMrBacklogTable(model);
    renderMrCarryoverTable(model);
}

function renderMrMovementTable(monthLabels, raisedByMonth, finishedByMonth) {
    const head = document.getElementById("mr-movement-table-head");
    const body = document.getElementById("mr-movement-table-body");
    if (!head && !body) return;
    if (!monthLabels.length) {
        if (head) head.innerHTML = "<th>Metric</th><th>No data</th>";
        if (body) body.innerHTML = `<tr><td colspan="2" class="empty-cell">No MR movement data available.</td></tr>`;
        return;
    }
    if (head) {
        head.innerHTML = `<th>Metric</th>${monthLabels.map((label) => `<th>${escapeHtml(label)}</th>`).join("")}`;
    }
    if (body) {
        const rowFor = (label, values) => `
            <tr>
                <td>${escapeHtml(label)}</td>
                ${values.map((value) => `<td>${escapeHtml(fmtNumber(value || 0))}</td>`).join("")}
            </tr>`;
        body.innerHTML = rowFor("MR Raised", raisedByMonth) + rowFor("MR Finished", finishedByMonth);
    }
}

function renderMrMovementChart(model) {
    const canvas = ensureCanvas("mrMovementChart");
    if (!canvas) return;
    if (!model.rows.length) {
        renderEmptyChart("mrMovementChart", "No MR movement data available.");
        renderMrMovementTable([], [], []);
        return;
    }
    const raisedByFinancialMonth = MR_FINANCIAL_MONTH_ORDER.map((monthIndex) => model.monthlyRaised[monthIndex] || 0);
    const finishedByFinancialMonth = MR_FINANCIAL_MONTH_ORDER.map((monthIndex) => model.monthlyFinished[monthIndex] || 0);
    renderMrMovementTable(getMrFinancialMonthLabelsWithYear(model.selectedYear), raisedByFinancialMonth, finishedByFinancialMonth);
    destroyChart("mrMovementChart");
    chartRefs.mrMovementChart = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
            labels: getMrFinancialMonthLabelsWithYear(model.selectedYear),
            datasets: [
                {
                    label: "MR Raised",
                    data: raisedByFinancialMonth,
                    backgroundColor: "#3b82f6",
                    borderRadius: 8,
                    maxBarThickness: 34,
                },
                {
                    label: "MR Finished",
                    data: finishedByFinancialMonth,
                    backgroundColor: "#10b981",
                    borderRadius: 8,
                    maxBarThickness: 34,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { position: "bottom", labels: { usePointStyle: true, boxWidth: 10 } },
                tooltip: { callbacks: { label: (context) => `${context.dataset.label}: ${fmtNumber(context.parsed.y)}` } },
            },
            scales: {
                x: { grid: { display: false }, ticks: { color: "#475569", maxRotation: 45, minRotation: 45 } },
                y: { beginAtZero: true, ticks: { precision: 0, color: "#475569" }, grid: { color: "rgba(148, 163, 184, 0.18)" } },
            },
        },
    });
}

function renderMrBacklogTable(model) {
    const body = document.getElementById("mr-backlog-body");
    if (!body) return;
    const rows = [
        ["Opening Backlog", model.openingBacklog],
        ["MR Raised", model.raisedCount],
        ["MR Finished", -model.finishedCount],
        ["Closing Backlog", model.closingBacklog],
    ];
    body.innerHTML = rows.map(([label, value]) => `
        <tr>
            <td>${escapeHtml(label)}</td>
            <td class="${label === "MR Finished" ? "mr-negative" : ""}">${escapeHtml(label === "MR Finished" ? `-${fmtNumber(Math.abs(value))}` : fmtNumber(value))}</td>
        </tr>
    `).join("");
}

function getMrTypeBadge(type, label) {
    const level = type === "carryover_closed" || type === "previous_year_open" ? "warning"
        : (type === "raised_this_year_open" ? "requires_attention" : "stable");
    return buildStatusPill(level, label);
}

function getMrAgeDays(item, selectedYear) {
    if (!item.raised.date) return null;
    const financialYearEnd = new Date(getMrFinancialYearRange(selectedYear).end.getTime() - 1);
    const end = item.finished.date || new Date(Math.min(Date.now(), financialYearEnd.getTime()));
    return Math.max(0, Math.round((end.getTime() - item.raised.date.getTime()) / 86400000));
}

function formatMrAge(item, selectedYear) {
    const days = getMrAgeDays(item, selectedYear);
    if (days === null || days === undefined) return "--";
    if (days < 1) return "0 days";
    return `${fmtNumber(days)} day${days === 1 ? "" : "s"}`;
}

function sortMrCarryoverRows(rows, selectedYear, sortKey = "duration_desc") {
    const direction = sortKey === "duration_asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
        const ageDelta = (Number(getMrAgeDays(a, selectedYear)) || 0) - (Number(getMrAgeDays(b, selectedYear)) || 0);
        if (ageDelta) return ageDelta * direction;
        const raisedDelta = (a.raised.date?.getTime() || 0) - (b.raised.date?.getTime() || 0);
        if (raisedDelta) return raisedDelta;
        return String(getMrWorkOrderId(a.row, a.index) || "").localeCompare(String(getMrWorkOrderId(b.row, b.index) || ""));
    });
}

function renderMrCarryoverTable(model) {
    const body = document.getElementById("mr-carryover-body");
    if (!body) return;
    const filter = mrCarryoverFilter || "all";
    const filteredRows = model.relevantRows.filter((item) => filter === "all" || item.type === filter);
    const rows = sortMrCarryoverRows(filteredRows, model.selectedYear, mrCarryoverSort || "duration_desc");
    if (!rows.length) {
        body.innerHTML = `<tr><td colspan="9" class="empty-cell">No MR records match this carry-over view for ${escapeHtml(model.financialYearLabel)}.</td></tr>`;
        return;
    }
    body.innerHTML = rows.map((item) => {
        const row = item.row;
        return `
            <tr>
                <td>${escapeHtml(getMrWorkOrderId(row, item.index) || "--")}</td>
                <td>${escapeHtml(row?.asset_id || row?.machine_code || "--")}</td>
                <td>${escapeHtml(getMrMachineName(row))}</td>
                <td>${escapeHtml(row?.criticality || row?.normalized_criticality || "--")}</td>
                <td>${escapeHtml(fmtDateOnly(item.raised.date))}</td>
                <td>${escapeHtml(item.finished.date ? fmtDateOnly(item.finished.date) : "--")}</td>
                <td>${escapeHtml(getMrStatus(row))}</td>
                <td>${getMrTypeBadge(item.type, item.label)}</td>
                <td>${escapeHtml(formatMrAge(item, model.selectedYear))}</td>
            </tr>
        `;
    }).join("");
}

function getMrRaisedDate(row) {
    return parseMrDateField(row, MR_RAISED_DATE_ALIASES);
}

function getMrFinishedDate(row) {
    return isMrFinishedStatus(getMrStatus(row))
        ? parseMrDateField(row, MR_FINISHED_DATE_ALIASES)
        : { date: null, field: "", invalid: false, missing: true };
}

function getAgeDaysFrom(date, end = new Date()) {
    if (!date) return null;
    return Math.max(0, Math.floor((end.getTime() - date.getTime()) / 86400000));
}

function formatDays(days) {
    if (days === null || days === undefined || Number.isNaN(Number(days))) return "--";
    const numeric = Number(days);
    return `${fmtNumber(numeric)} day${numeric === 1 ? "" : "s"}`;
}

function getRowAgeOrDuration(row) {
    const raised = getMrRaisedDate(row).date;
    const start = parseDateValue(row?.actual_start_time || row?.maintenance_start_time);
    const finished = getMrFinishedDate(row).date;
    const ttrHours = getTtrHours(row);
    if (isMrFinishedStatus(getMrStatus(row)) && ttrHours !== null) return fmtHours(ttrHours);
    if (finished && start && finished >= start) return fmtHours((finished.getTime() - start.getTime()) / 3600000);
    if (raised) return formatDays(getAgeDaysFrom(raised));
    return "--";
}

const SL_BADGE_COLORS = {
    "1": { bg: "#fee2e2", text: "#b91c1c" },
    "2": { bg: "#ffedd5", text: "#c2410c" },
    "3": { bg: "#fef9c3", text: "#854d0e" },
    "4": { bg: "#dbeafe", text: "#1d4ed8" },
};

const SEVERITY_LEVEL_ORDER = ["1", "2", "3", "4"];

// Fixed, ordered S1–S4 layout. Every level is shown, even when its count is 0,
// so the card reads consistently each render.
function buildSeverityBreakdownHtml(map) {
    return SEVERITY_LEVEL_ORDER.map((label) => {
        const count = map.get(label) || 0;
        const { bg, text } = SL_BADGE_COLORS[label] || { bg: "#e2e8f0", text: "#475569" };
        return `<div class="sl-item"><span class="sl-badge" style="background:${bg};color:${text}">S${escapeHtml(label)}</span><span class="sl-count">${fmtNumber(count)}</span></div>`;
    }).join("");
}

function buildSeverityBreakdownCriticalHtml(map, limit = 2) {
    const rows = [...map.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, limit);
    if (!rows.length) return `<span class="sl-trend-label">No critical open MR</span>`;
    const items = rows.map(([label, count]) => {
        const { bg, text } = SL_BADGE_COLORS[label] || { bg: "#e2e8f0", text: "#475569" };
        return `<span class="sl-badge-sm" style="background:${bg};color:${text}">S${escapeHtml(label)}</span><span class="sl-crit-count">${fmtNumber(count)}</span>`;
    }).join(" ");
    return `<span class="sl-trend-label">Critical:</span>${items}`;
}

// Cache of the most recently rendered overview source rows so scoped topic
// panels (Data Reliability, Preventive vs Corrective) can re-render without
// refetching. The top KPI strip derives its own YTD subset from this source.
let downtimeOverviewRowsCache = [];

// Scoped filters live on the dedicated topic panels — the top KPI strip stays YTD.
let topicReliabilityYearFilter = "";
let topicReliabilityMonthFilter = "";
let topicPreventiveYearFilter = "";
let topicPreventiveMonthFilter = "";

function getOverviewRowYearMonth(row) {
    const raised = getMrRaisedDate(row).date;
    if (raised instanceof Date && !Number.isNaN(raised.getTime())) {
        return { year: String(raised.getFullYear()), month: String(raised.getMonth() + 1).padStart(2, "0") };
    }
    const raw = row?.request_created_time || row?.created_date || row?.start_time
        || row?.actual_start_time || row?.actual_start || row?.maintenance_start_time;
    if (!raw) return { year: "", month: "" };
    const s = String(raw);
    return { year: s.slice(0, 4) || "", month: s.length >= 7 ? s.slice(5, 7) : "" };
}

function filterOverviewRowsByDate(rows, year, month) {
    if (!year && !month) return rows;
    return rows.filter((row) => {
        const { year: y, month: m } = getOverviewRowYearMonth(row);
        if (year && y !== year) return false;
        if (month && m !== month) return false;
        return true;
    });
}

function getOverviewYtdYear(rows = []) {
    const currentYear = String(new Date().getFullYear());
    let latestYear = "";
    let hasCurrentYear = false;
    rows.forEach((row) => {
        const { year } = getOverviewRowYearMonth(row);
        if (!/^\d{4}$/.test(year)) return;
        if (!latestYear || Number(year) > Number(latestYear)) latestYear = year;
        if (year === currentYear) hasCurrentYear = true;
    });
    return hasCurrentYear ? currentYear : latestYear;
}

function getOverviewKpiRows(rows = []) {
    const ytdYear = getOverviewYtdYear(rows);
    return ytdYear ? filterOverviewRowsByDate(rows, ytdYear, "") : rows;
}

function getOverviewSourceRows(management = getManagement()) {
    if (Array.isArray(allWorkOrderRowsCache) && allWorkOrderRowsCache.length) return allWorkOrderRowsCache;
    const fallbackRows = getFallbackAllWorkOrderRows();
    if (fallbackRows.length) return fallbackRows;
    return getWorkOrderRows(management);
}

function populateTopicYearOptions(rows) {
    const years = new Set();
    rows.forEach((row) => {
        const { year } = getOverviewRowYearMonth(row);
        if (year && /^\d{4}$/.test(year)) years.add(year);
    });
    const sorted = [...years].sort((a, b) => Number(b) - Number(a));
    ["topic-reliability-year-filter", "topic-preventive-year-filter"].forEach((id) => {
        const sel = document.getElementById(id);
        if (!sel) return;
        const current = sel.value;
        sel.innerHTML = `<option value="">All Years</option>` +
            sorted.map((y) => `<option value="${y}">${y}</option>`).join("");
        if (current && sorted.includes(current)) sel.value = current;
    });
}

function renderDowntimeOverviewFromRows(rows = []) {
    downtimeOverviewRowsCache = rows;
    populateTopicYearOptions(rows);
    if (!rows.length) {
        setText("kpi-maintenance-resolution-time", "--");
        setText("kpi-maintenance-resolution-sub", "No imported work orders loaded.");
        setText("kpi-maintenance-resolution-count", "-- in-progress MR");
        setText("kpi-work-order-count", "--");
        setText("kpi-work-order-count-sub", "--");
        setHtml("kpi-open-severity-breakdown", `<span class="sl-empty">--</span>`);
        setHtml("kpi-open-severity-critical-breakdown", `<span class="sl-trend-label">Critical open MR by Service level.</span>`);
        setText("kpi-data-review-count", "--");
        renderTopicDataReliabilityPanel();
        syncTopicMirrors();
        return;
    }

    const kpiRows = getOverviewKpiRows(rows);
    const openRows = kpiRows.filter((row) => isNormalOpenMrStatus(getMrStatus(row)));
    const newRows = openRows.filter((row) => isMrNewStatus(getMrStatus(row)));
    const inProgressRows = openRows.filter((row) => isMrInProgressStatus(getMrStatus(row)));
    const machinesAffected = new Set(openRows.map((row) => String(row.asset_id || row.machine_code || "").trim()).filter(Boolean));
    const severityMap = new Map();
    const criticalSeverityMap = new Map();
    openRows.forEach((row) => {
        const severity = getMrServiceLevel(row);
        severityMap.set(severity, (severityMap.get(severity) || 0) + 1);
        if (isProductionCritical(row)) criticalSeverityMap.set(severity, (criticalSeverityMap.get(severity) || 0) + 1);
    });
    const qualityValid = kpiRows.filter(isDataQualityValid).length;
    const invalidCount = kpiRows.length - qualityValid;
    const reviewCount = kpiRows.filter((row) => getDataQualityFlags(row).some((flag) => flag === "Review status") || getAcknowledgementStatus(row) === "Review").length;

    setText("kpi-maintenance-resolution-time", fmtNumber(openRows.length));
    setText("kpi-maintenance-resolution-sub", `${fmtNumber(newRows.length)} New MR + ${fmtNumber(inProgressRows.length)} In progress MR`);
    setText("kpi-maintenance-resolution-count", `${fmtNumber(inProgressRows.length)} in-progress MR`);
    setText("kpi-work-order-count", fmtNumber(newRows.length));
    setText("kpi-work-order-count-sub", fmtNumber(machinesAffected.size));
    setHtml("kpi-open-severity-breakdown", buildSeverityBreakdownHtml(severityMap));
    setHtml("kpi-open-severity-critical-breakdown", buildSeverityBreakdownCriticalHtml(criticalSeverityMap));
    setText("kpi-data-reliability", fmtPercent((qualityValid / kpiRows.length) * 100));
    setText("kpi-invalid-work-orders", fmtNumber(invalidCount));
    setText("kpi-data-review-count", fmtNumber(reviewCount));
    setText("kpi-data-reliability-sub", `${fmtNumber(qualityValid)} of ${fmtNumber(kpiRows.length)} work order records are valid.`);

    renderTopicDataReliabilityPanel();
    syncTopicMirrors();
}

// Renders the dedicated Data Reliability topic panel with its own year/month scope.
// The mini KPI cards on this panel write to *-topic targets so they don't fight
// with the YTD values mirrored from the top strip.
function renderTopicDataReliabilityPanel() {
    const rows = downtimeOverviewRowsCache || [];
    const scoped = filterOverviewRowsByDate(rows, topicReliabilityYearFilter, topicReliabilityMonthFilter);
    if (!scoped.length) {
        setText("topic-data-reliability-pct", "--");
        setText("topic-data-reliability-sub", "No work order records in the selected scope.");
        setText("topic-invalid-work-orders", "--");
        setText("topic-data-review-count", "--");
        renderDataReliabilityActionList([]);
        renderDataReliabilityHistoryTable([]);
        renderDataReviewHistoryPanel();
        return;
    }
    const qualityValid = scoped.filter(isDataQualityValid).length;
    const invalidCount = scoped.length - qualityValid;
    const reviewCount = scoped.filter((row) => getDataQualityFlags(row).some((flag) => flag === "Review status") || getAcknowledgementStatus(row) === "Review").length;
    setText("topic-data-reliability-pct", fmtPercent((qualityValid / scoped.length) * 100));
    setText("topic-invalid-work-orders", fmtNumber(invalidCount));
    setText("topic-data-review-count", fmtNumber(reviewCount));
    setText("topic-data-reliability-sub", `${fmtNumber(qualityValid)} of ${fmtNumber(scoped.length)} work order records are valid.`);
    renderDataReliabilityActionList(buildWorkOrderSlaModel(scoped, getWorkOrderSlaReferenceDate()).entries);
    renderDataReliabilityHistoryTable(scoped);
    renderDataReviewHistoryPanel();
}

function getCriticalMrItems(rows = []) {
    return normalizeMrTrackingRows(rows).items.filter((item) => isProductionCritical(item.row));
}

// ─── MR Comparison & Trend Analysis ─────────────────────────────────────────

// Returns all normalised items filtered by the currently selected scope.
// Reuses getPerformanceMachineGroup() — same logic as Machine Explorer group cards.
function getCmcScopeItems(rows = []) {
    const allItems = normalizeMrTrackingRows(rows).items;
    const selectedScope = normalizeMachineExplorerGroupLabel(cmcScope);
    if (cmcScope === "Critical") return allItems.filter((item) => isProductionCritical(item.row));
    if (cmcScope === "all" || selectedScope === MACHINE_EXPLORER_ALL_GROUP) return allItems;
    // All remaining scopes map to getPerformanceMachineGroup values
    return allItems.filter((item) => normalizeMachineExplorerGroupLabel(getPerformanceMachineGroup(item.row)) === selectedScope);
}

// Returns scope-specific subtitle text for display.
function getCmcScopeSubtitle() {
    const map = {
        all: "All assets, using the same Machine Explorer grouping logic. Select two periods or all years to compare performance.",
        Critical: "Critical assets only, using the Asset Master mapping.",
        "Non-Critical / Facility": "Non-critical and facility assets.",
        "Production Equipment": "Production equipment assets only, including bratt pans, ovens, conveyors, fryers, and similar production line assets.",
        Refrigeration: "Refrigeration assets only, including condensers, evaporators, freezers, chillers, cold rooms, air blast units, ice makers, and refrigerant-related assets.",
        Utilities: "Utilities assets, including water systems, boilers, pumps, tanks, MDB, and compressors.",
        "Utilities / Support": "Utilities and support assets, including water systems, boilers, pumps, tanks, MDB, and compressors.",
        "Facility / Building": "Facility and building assets, including doors, rooms, lighting, CCTV, air conditioning, and electrical systems.",
        "Unknown / Review": "Assets not yet classified into a specific group. Review these records for correct machine group assignment.",
    };
    return map[cmcScope] || map[normalizeMachineExplorerGroupLabel(cmcScope)] || map.all;
}

// Returns the short display label for the selected scope (used in chart titles and KPI labels).
function getCmcScopeLabel() {
    return cmcScope === "all" ? "" : `${cmcScope} `;
}

// Compute all-years stats for Show All Years mode.
// YTD logic: if the latest year is incomplete, compare using a matching cutoff in the prior year.
function computeCmcAllYears(items) {
    if (!items.length) return [];
    // Find latest data date
    let latestDate = new Date(0);
    items.forEach((it) => {
        const d = it.raised.date || it.finished.date;
        if (d && d > latestDate) latestDate = d;
    });
    const latestYear = latestDate.getFullYear();
    const currentYear = new Date().getFullYear();
    const isCurrentYearIncomplete = latestYear >= currentYear;
    // YTD cutoff: if the latest year equals the current calendar year, use the actual latest date as cutoff
    const ytdCutoffDate = isCurrentYearIncomplete ? latestDate : null;

    // Collect all years from raised dates
    const yearSet = new Set();
    items.forEach((it) => { if (it.raised.date) yearSet.add(it.raised.date.getFullYear()); });
    const years = [...yearSet].sort();

    return years.map((y) => {
        const start = new Date(y, 0, 1);
        let end, isYtd = false;
        if (y === latestYear && ytdCutoffDate) {
            // Use actual latest date as end — do not claim full-year results for an incomplete year
            end = new Date(ytdCutoffDate); end.setHours(23, 59, 59, 999);
            isYtd = true;
        } else {
            end = new Date(y, 11, 31, 23, 59, 59);
        }
        const stats = computeCmcStats(items, start, end);
        return { year: y, label: isYtd ? `${y} YTD` : String(y), isYtd, stats, start, end };
    });
}

function renderCmcAllYearsSection(items) {
    const section = document.getElementById("cmc-all-years-section");
    if (section) section.classList.remove("hidden");

    const yearStats = computeCmcAllYears(items);
    const grid = document.getElementById("cmc-all-years-kpi-grid");
    const scopeLabel = getCmcScopeLabel();

    // Totals across all full years + current YTD
    const totalRaised = yearStats.reduce((s, y) => s + y.stats.raised, 0);
    const totalFinished = yearStats.reduce((s, y) => s + y.stats.finished, 0);
    const latestYear = yearStats[yearStats.length - 1];
    const prevYear = yearStats[yearStats.length - 2] || null;

    // YTD note
    const ytdYear = yearStats.find((y) => y.isYtd);
    const ytdNote = document.getElementById("cmc-ytd-note");
    if (ytdNote) {
        if (ytdYear && prevYear) {
            const cutoffFmt = ytdYear.end.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
            const prevCutoff = new Date(prevYear.year, ytdYear.end.getMonth(), ytdYear.end.getDate());
            const prevCutoffFmt = prevCutoff.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
            ytdNote.textContent = `YTD comparison uses the same date cut-off for both years: ${ytdYear.year} Jan 1 – ${cutoffFmt} vs ${prevYear.year} Jan 1 – ${prevCutoffFmt}.`;
            ytdNote.style.display = "";
        } else {
            ytdNote.style.display = "none";
        }
    }

    if (!grid) return;
    if (!yearStats.length) { grid.innerHTML = `<p class="cmc-empty">No data available for the selected scope.</p>`; return; }

    // Trend direction card
    const trendDir = document.getElementById("cmc-trend-direction-row");
    if (trendDir && latestYear && prevYear) {
        // Compare latest-year vs previous-year MR raised (YTD-adjusted if needed)
        let prevRaisedComparable = prevYear.stats.raised;
        if (latestYear.isYtd) {
            // Recalculate prevYear with same cutoff as YTD
            const prevStart = new Date(prevYear.year, 0, 1);
            const prevEnd = new Date(prevYear.year, latestYear.end.getMonth(), latestYear.end.getDate(), 23, 59, 59);
            prevRaisedComparable = computeCmcStats(items, prevStart, prevEnd).raised;
        }
        const diff = latestYear.stats.raised - prevRaisedComparable;
        const dirLabel = diff < 0 ? "Downward trend ↓ (fewer MR raised)" : diff > 0 ? "Upward trend ↑ (more MR raised)" : "Flat / Stable →";
        const dirCls = diff < 0 ? "cmc-chg-good" : diff > 0 ? "cmc-chg-bad" : "cmc-chg-neutral";
        const ytdTag = latestYear.isYtd ? ` <span class="cmc-ytd-tag">YTD comparison</span>` : "";
        trendDir.innerHTML = `<span class="cmc-trend-label ${dirCls}">${escapeHtml(dirLabel)}</span>${ytdTag}`;
    } else if (trendDir) {
        trendDir.innerHTML = "";
    }

    // Summary KPI cards — one per year
    grid.innerHTML = yearStats.map((ys) => {
        const s = ys.stats;
        const closureStr = s.closureRate !== null ? fmtPercent(s.closureRate) : "--";
        return `<div class="cmc-all-years-card">
            <div class="cmc-all-years-yr">${escapeHtml(ys.label)}${ys.isYtd ? ` <span class="cmc-ytd-tag">YTD</span>` : ""}</div>
            <div class="cmc-all-years-metrics">
                <div class="cmc-ay-row"><span>${escapeHtml(`${scopeLabel}MR Raised`)}</span><strong>${fmtNumber(s.raised)}</strong></div>
                <div class="cmc-ay-row"><span>Finished</span><strong>${fmtNumber(s.finished)}</strong></div>
                <div class="cmc-ay-row"><span>Open Backlog</span><strong>${fmtNumber(s.openBacklog)}</strong></div>
                <div class="cmc-ay-row"><span>Not Acknowledged</span><strong>${fmtNumber(s.notAck)}</strong></div>
                <div class="cmc-ay-row"><span>Closure Rate</span><strong>${closureStr}</strong></div>
                <div class="cmc-ay-row"><span>Invalid</span><strong>${fmtNumber(s.invalid)}</strong></div>
            </div>
        </div>`;
    }).join("");

    // All Years bar chart
    const scopeStr = cmcScope === "all" ? "All Assets" : cmcScope;
    setText("cmc-bar-title", `All Years Trend: ${scopeStr}`);
    const canvas = ensureCanvas("cmcBarChart");
    if (canvas) {
        destroyChart("cmcBarChart");
        const labels = yearStats.map((y) => y.label);
        chartRefs.cmcBarChart = new Chart(canvas.getContext("2d"), {
            type: "bar",
            data: {
                labels: ["MR Raised", "Finished", "Open Backlog", "Not Ack", "Invalid"],
                datasets: yearStats.map((ys, i) => ({
                    label: ys.label,
                    data: [ys.stats.raised, ys.stats.finished, ys.stats.openBacklog, ys.stats.notAck, ys.stats.invalid],
                    backgroundColor: CMC_ALL_YEARS_COLORS[i % CMC_ALL_YEARS_COLORS.length],
                    borderRadius: 6,
                })),
            },
            options: mrTrackingAxisOptions(scopeStr),
        });
    }

    // All Years cumulative trend (monthly, all years overlaid)
    setText("cmc-trend-title", `Cumulative All Years Trend: ${scopeStr}`);
    const trendCanvas = ensureCanvas("cmcTrendChart");
    if (trendCanvas && yearStats.length) {
        destroyChart("cmcTrendChart");
        // Build per-year monthly cumulative raised series
        const allMonths = new Set();
        items.forEach((it) => {
            if (it.raised.date) {
                const m = `${String(it.raised.date.getMonth() + 1).padStart(2, "0")}`;
                allMonths.add(m);
            }
        });
        const monthLabels = [...allMonths].sort().map((m) => MR_MONTH_LABELS[Number(m) - 1]);
        const datasets = yearStats.map((ys, i) => {
            const monthly = new Array(12).fill(0);
            items.forEach((it) => {
                if (it.raised.date && it.raised.date.getFullYear() === ys.year && it.raised.date <= ys.end) {
                    monthly[it.raised.date.getMonth()]++;
                }
            });
            // Build cumulative
            const cumul = []; let sum = 0;
            monthly.forEach((v) => { sum += v; cumul.push(sum); });
            return {
                label: `${ys.label} Raised`,
                data: cumul,
                borderColor: CMC_ALL_YEARS_COLORS[i % CMC_ALL_YEARS_COLORS.length],
                backgroundColor: "transparent",
                fill: false,
                tension: 0.2,
                pointRadius: 3,
                borderDash: ys.isYtd ? [5, 3] : [],
            };
        });
        chartRefs.cmcTrendChart = new Chart(trendCanvas.getContext("2d"), {
            type: "line",
            data: { labels: MR_MONTH_LABELS, datasets },
            options: mrTrackingAxisOptions(scopeStr),
        });
    }

    // Hide compare-mode KPI cards and summary table when in all-years mode
    const kpiGrid = document.getElementById("cmc-kpi-grid");
    if (kpiGrid) kpiGrid.classList.add("hidden");
    const summaryCard = document.querySelector(".cmc-summary-card");
    if (summaryCard) summaryCard.classList.add("hidden");
}

const CMC_ALL_YEARS_COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#8b5cf6", "#ef4444", "#14b8a6", "#f97316"];

// ─── Spare Part Usage Trend (linked to MR Comparison scope) ─────────────────

// Refrigeration-related categories and keywords used to filter spare part data
// when the scope is set to Refrigeration.
const CMC_REFRIG_SPARE_CATEGORIES = new Set(["Refrigerant / Chemical"]);
const CMC_REFRIG_SPARE_KEYWORDS = /refriger|r507|r22|r404|chiller|freezer|condenser|evaporator|cold room|ice maker|air blast/i;

async function loadCmcSpareTrendData() {
    if (cmcSpareTrendData) return cmcSpareTrendData;
    try {
        const data = await fetch("/api/maintenance/project_transactions").then((r) => r.ok ? r.json() : null);
        cmcSpareTrendData = data;
    } catch (e) {
        cmcSpareTrendData = null;
    }
    return cmcSpareTrendData;
}

// Filter transactions by current scope.
function cmcFilterSpareByScope(transactions) {
    if (!transactions?.length) return [];
    const selectedScope = normalizeMachineExplorerGroupLabel(cmcScope);
    if (cmcScope === "all" || selectedScope === MACHINE_EXPLORER_ALL_GROUP) return transactions;
    if (selectedScope === "Refrigeration") {
        return transactions.filter((t) =>
            CMC_REFRIG_SPARE_CATEGORIES.has(t.item_category) ||
            CMC_REFRIG_SPARE_KEYWORDS.test(t.translated_description || t.original_description || "") ||
            CMC_REFRIG_SPARE_KEYWORDS.test(t.asset_id || "")
        );
    }
    // For other scopes try to match via equipment_type or asset link
    return transactions.filter((t) => {
        if (!t.equipment_type && !t.equipment_criticality) return false;
        if (cmcScope === "Critical") return (t.equipment_criticality || "").toLowerCase().includes("critical");
        if (selectedScope === "Production Equipment") return /production|bratt|oven|conveyor|fryer/i.test(t.equipment_type || "");
        if (selectedScope === "Utilities") return /utility|water|boiler|pump|compressor/i.test(t.equipment_type || "");
        if (selectedScope === "Non-Critical / Facility") return /facility|building|electrical/i.test(t.equipment_type || "");
        return false;
    });
}

async function renderCmcSpareTrend() {
    const data = await loadCmcSpareTrendData();
    const card = document.getElementById("cmc-spare-trend-card");
    const titleEl = document.getElementById("cmc-spare-trend-title");
    const subtitleEl = document.getElementById("cmc-spare-trend-subtitle");
    const kpiRow = document.getElementById("cmc-spare-kpi-row");
    const topPartEl = document.getElementById("cmc-spare-top-part");
    const topAssetEl = document.getElementById("cmc-spare-top-asset");
    const linkNoteEl = document.getElementById("cmc-spare-link-note");
    if (!card) return;

    const selectedScope = normalizeMachineExplorerGroupLabel(cmcScope);
    const scopeLabel = cmcScope === "all" || selectedScope === MACHINE_EXPLORER_ALL_GROUP ? "All Assets" : selectedScope;
    if (titleEl) titleEl.textContent = `${scopeLabel} — Spare Part Usage Trend`;

    if (!data || data.status === "missing") {
        if (subtitleEl) subtitleEl.textContent = "Project Actual Transactions file not uploaded. Upload to enable spare part trend.";
        if (kpiRow) kpiRow.innerHTML = "";
        renderCmcSpareEmpty("cmcSpareMonthlyChart");
        renderCmcSpareEmpty("cmcSpareTopChart");
        return;
    }

    const all = data.transactions || [];
    const filtered = cmcFilterSpareByScope(all);
    const totalVal = filtered.reduce((s, t) => s + (t.total_consumption || 0), 0);
    const linked = filtered.filter((t) => t.link_status === "Linked").length;
    const linkPct = filtered.length ? Math.round(linked / filtered.length * 100) : 0;

    // Scope-specific subtitle
    const subtitleMap = {
        Refrigeration: "Refrigerant/Chemical parts + refrigeration asset keywords. Includes R507, chillers, condensers, evaporators.",
        Utilities: "Spare parts linked to utilities assets.",
        "Non-Critical / Facility": "Spare parts linked to non-critical and facility assets.",
        Critical: "Spare parts linked to Critical assets via work order data.",
        all: "All spare parts from Project Actual Transactions 2026.",
    };
    if (subtitleEl) subtitleEl.textContent = subtitleMap[cmcScope] || subtitleMap[selectedScope] || `Spare parts linked to ${scopeLabel} assets.`;

    // KPI pills
    const fmtCurr = (v) => `THB ${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
    if (kpiRow) {
        kpiRow.innerHTML = `
            <span class="cmc-spare-kpi">${fmtCurr(totalVal)} <em>Total</em></span>
            <span class="cmc-spare-kpi">${filtered.length} <em>Lines</em></span>
            <span class="cmc-spare-kpi">${linkPct}% <em>Linked</em></span>`;
    }

    // Monthly trend chart
    const monthly = {};
    filtered.forEach((t) => {
        const mk = (t.project_date || "").slice(0, 7);
        if (mk) monthly[mk] = (monthly[mk] || 0) + (t.total_consumption || 0);
    });
    const monthlyEntries = Object.entries(monthly).sort(([a], [b]) => a.localeCompare(b));

    if (monthlyEntries.length) {
        const canvas = ensureCanvas("cmcSpareMonthlyChart");
        if (canvas) {
            destroyChart("cmcSpareMonthlyChart");
            chartRefs.cmcSpareMonthlyChart = new Chart(canvas.getContext("2d"), {
                type: "bar",
                data: {
                    labels: monthlyEntries.map(([m]) => m),
                    datasets: [{ label: "THB", data: monthlyEntries.map(([, v]) => Math.round(v)), backgroundColor: "#0f766e", borderRadius: 5 }],
                },
                options: cmcSpareChartOptions("THB"),
            });
        }
    } else {
        renderCmcSpareEmpty("cmcSpareMonthlyChart");
    }

    // Top parts by value chart
    const byPart = {};
    filtered.forEach((t) => {
        const k = (t.translated_description || t.clean_description || t.original_description || "Unknown").slice(0, 35);
        byPart[k] = (byPart[k] || 0) + (t.total_consumption || 0);
    });
    const topParts = Object.entries(byPart).sort(([, a], [, b]) => b - a).slice(0, 8);

    if (topParts.length) {
        const canvas2 = ensureCanvas("cmcSpareTopChart");
        if (canvas2) {
            destroyChart("cmcSpareTopChart");
            chartRefs.cmcSpareTopChart = new Chart(canvas2.getContext("2d"), {
                type: "bar",
                data: {
                    labels: topParts.map(([l]) => l),
                    datasets: [{ label: "THB", data: topParts.map(([, v]) => Math.round(v)), backgroundColor: "#8b5cf6", borderRadius: 5 }],
                },
                options: { ...cmcSpareChartOptions("THB"), indexAxis: "y" },
            });
        }
    } else {
        renderCmcSpareEmpty("cmcSpareTopChart");
    }

    // Bottom info row
    const topPart = topParts[0];
    const byAsset = {};
    filtered.forEach((t) => { if (t.asset_id) byAsset[t.asset_id] = (byAsset[t.asset_id] || 0) + (t.total_consumption || 0); });
    const topAsset = Object.entries(byAsset).sort(([, a], [, b]) => b - a)[0];

    if (topPartEl) topPartEl.innerHTML = topPart
        ? `<span class="cmc-spare-info-lbl">Top Part</span><span class="cmc-spare-info-val">${escapeHtml(topPart[0])} — ${fmtCurr(topPart[1])}</span>`
        : `<span class="cmc-spare-info-lbl">Top Part</span><span class="cmc-spare-info-val">No data</span>`;

    if (topAssetEl) topAssetEl.innerHTML = topAsset
        ? `<span class="cmc-spare-info-lbl">Top Asset</span><span class="cmc-spare-info-val">${escapeHtml(topAsset[0])} — ${fmtCurr(topAsset[1])}</span>`
        : `<span class="cmc-spare-info-lbl">Top Asset</span><span class="cmc-spare-info-val">No data</span>`;

    if (linkNoteEl) {
        linkNoteEl.innerHTML = filtered.length === 0
            ? `<span class="cmc-spare-info-lbl">Linkage</span><span class="cmc-spare-info-val cmc-spare-warn">No ${scopeLabel} spare parts found. Parts may not be linked to this group yet.</span>`
            : `<span class="cmc-spare-info-lbl">Linkage</span><span class="cmc-spare-info-val">${linked} of ${filtered.length} lines linked to WO data (${linkPct}%)</span>`;
    }
}

function renderCmcSpareEmpty(canvasId) {
    destroyChart(canvasId);
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    canvas.style.display = "none";
    const existing = canvas.parentElement?.querySelector(".cmc-spare-empty-msg");
    if (!existing) {
        const d = document.createElement("div");
        d.className = "cmc-spare-empty-msg";
        d.textContent = "No data for selected scope.";
        canvas.parentElement?.appendChild(d);
    }
}

function cmcSpareChartOptions(axisLabel) {
    return {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
            x: { grid: { display: false }, ticks: { color: "#64748b", font: { size: 10 }, maxRotation: 45 } },
            y: { beginAtZero: true, grid: { color: "rgba(148,163,184,0.15)" }, ticks: { color: "#64748b", font: { size: 10 } }, title: axisLabel ? { display: false } : undefined },
        },
    };
}

function getCmcPeriods() {
    if (cmcMode === "month") {
        const [ay, am] = (cmcMonthA || "").split("-").map(Number);
        const [by, bm] = (cmcMonthB || "").split("-").map(Number);
        if (!ay || !am || !by || !bm) return null;
        return {
            a: { start: new Date(ay, am - 1, 1), end: new Date(ay, am, 0, 23, 59, 59) },
            b: { start: new Date(by, bm - 1, 1), end: new Date(by, bm, 0, 23, 59, 59) },
            labelA: `${MR_MONTH_LABELS[am - 1]} ${ay}`,
            labelB: `${MR_MONTH_LABELS[bm - 1]} ${by}`,
        };
    }
    if (cmcMode === "year") {
        const ay = Number(cmcYearA), by = Number(cmcYearB);
        if (!ay || !by) return null;
        return {
            a: { start: new Date(ay, 0, 1), end: new Date(ay, 11, 31, 23, 59, 59) },
            b: { start: new Date(by, 0, 1), end: new Date(by, 11, 31, 23, 59, 59) },
            labelA: String(ay),
            labelB: String(by),
        };
    }
    if (cmcMode === "custom") {
        const aS = parseDateValue(cmcCustomAStart);
        const aE = parseDateValue(cmcCustomAEnd);
        const bS = parseDateValue(cmcCustomBStart);
        const bE = parseDateValue(cmcCustomBEnd);
        if (!aS || !aE || !bS || !bE) return null;
        aE.setHours(23, 59, 59, 999);
        bE.setHours(23, 59, 59, 999);
        const fmtD = (d) => d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
        return {
            a: { start: aS, end: aE },
            b: { start: bS, end: bE },
            labelA: `${fmtD(aS)} – ${fmtD(aE)}`,
            labelB: `${fmtD(bS)} – ${fmtD(bE)}`,
        };
    }
    return null;
}

// Compute all comparison KPIs for one period window.
function computeCmcStats(items, start, end) {
    // Raised: created date falls within [start, end]
    const raised = items.filter((it) => { const d = it.raised.date; return d && d >= start && d <= end; });

    // Finished: lifecycle = Finished AND actual end within [start, end]
    const finished = items.filter((it) => { const d = it.finished.date; return isMrFinishedStatus(it.status) && d && d >= start && d <= end; });

    // Open backlog at period end: raised on/before end, not finished by period end
    const openBacklog = items.filter((it) => {
        const r = it.raised.date;
        if (!r || r > end) return false;
        const f = isMrFinishedStatus(it.status) ? it.finished.date : null;
        return !f || f > end;
    });

    // Not acknowledged: status=New with no Work Order, raised within period
    const notAck = raised.filter((it) => isMrOutstandingAcknowledgement(it));

    // Opening backlog: raised before start, still open at period start
    const openingBacklog = items.filter((it) => {
        const r = it.raised.date;
        if (!r || r >= start) return false;
        const f = isMrFinishedStatus(it.status) ? it.finished.date : null;
        return !f || f >= start;
    });

    // Closure rate = finished / raised (handle divide-by-zero)
    const closureRate = raised.length > 0 ? (finished.length / raised.length) * 100 : null;

    // Backlog change = closing backlog − opening backlog (negative = improved)
    const backlogChange = openBacklog.length - openingBacklog.length;

    // Average TTR: finished within period with valid positive TTR hours
    const ttrValues = finished.map((it) => getTtrHours(it.row)).filter((t) => t !== null && t > 0);
    const avgTtr = ttrValues.length > 0 ? ttrValues.reduce((s, t) => s + t, 0) / ttrValues.length : null;

    // Invalid: records raised in period flagged for data quality review
    const invalid = raised.filter((it) => !isDataQualityValid(it.row)).length;

    return { raised: raised.length, finished: finished.length, openBacklog: openBacklog.length, notAck: notAck.length, closureRate, openingBacklog: openingBacklog.length, backlogChange, avgTtr, invalid };
}

function populateCmcControls(rows) {
    // Populate dropdown options using scope-filtered items (same logic as Machine Explorer groups)
    const criticalItems = getCmcScopeItems(rows);
    const monthSet = new Set();
    const yearSet = new Set();
    criticalItems.forEach((it) => {
        const d = it.raised.date;
        if (!d) return;
        monthSet.add(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`);
        yearSet.add(d.getFullYear());
    });
    const months = [...monthSet].sort().reverse();
    const years = [...yearSet].sort((a, b) => b - a);

    const monthHtml = months.map((m) => { const [y, mo] = m.split("-").map(Number); return `<option value="${m}">${MR_MONTH_LABELS[mo - 1]} ${y}</option>`; }).join("") || `<option value="">No data</option>`;
    const yearHtml = years.map((y) => `<option value="${y}">${y}</option>`).join("") || `<option value="">No data</option>`;
    ["cmc-month-a", "cmc-month-b"].forEach((id) => { const el = document.getElementById(id); if (el) el.innerHTML = monthHtml; });
    ["cmc-year-a", "cmc-year-b"].forEach((id) => { const el = document.getElementById(id); if (el) el.innerHTML = yearHtml; });

    // Set defaults: month A = previous available, month B = latest
    if (!cmcMonthA || !months.includes(cmcMonthA)) cmcMonthA = months[1] || months[0] || "";
    if (!cmcMonthB || !months.includes(cmcMonthB)) cmcMonthB = months[0] || "";
    if (!cmcYearA || !years.map(String).includes(String(cmcYearA))) cmcYearA = String(years[1] || years[0] || "");
    if (!cmcYearB || !years.map(String).includes(String(cmcYearB))) cmcYearB = String(years[0] || "");

    const sync = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
    sync("cmc-mode", cmcMode);
    sync("cmc-month-a", cmcMonthA);
    sync("cmc-month-b", cmcMonthB);
    sync("cmc-year-a", cmcYearA);
    sync("cmc-year-b", cmcYearB);
    if (cmcCustomAStart) sync("cmc-custom-a-start", cmcCustomAStart);
    if (cmcCustomAEnd) sync("cmc-custom-a-end", cmcCustomAEnd);
    if (cmcCustomBStart) sync("cmc-custom-b-start", cmcCustomBStart);
    if (cmcCustomBEnd) sync("cmc-custom-b-end", cmcCustomBEnd);
}

function updateCmcControlVisibility() {
    const isMonth = cmcMode === "month";
    const isYear = cmcMode === "year";
    const isCustom = cmcMode === "custom";
    const isAllYears = isYear && cmcYearView === "all";
    const isCompareTwoYears = isYear && cmcYearView === "compare";
    document.querySelectorAll(".cmc-month-controls").forEach((el) => el.classList.toggle("hidden", !isMonth));
    // Year-mode controls: Year View dropdown + Year A/B dropdowns (only in Compare Two Years)
    document.querySelectorAll(".cmc-year-controls").forEach((el) => el.classList.toggle("hidden", !isYear));
    document.querySelectorAll(".cmc-year-ab-controls").forEach((el) => el.classList.toggle("hidden", !isCompareTwoYears));
    document.querySelectorAll(".cmc-custom-controls").forEach((el) => el.classList.toggle("hidden", !isCustom));
    // All-years section visibility
    const allYearsSection = document.getElementById("cmc-all-years-section");
    if (allYearsSection) allYearsSection.classList.toggle("hidden", !isAllYears);
    // Two-year mode KPI grid visibility
    const kpiGrid = document.getElementById("cmc-kpi-grid");
    if (kpiGrid) kpiGrid.classList.toggle("hidden", isAllYears);
    const summaryCard = document.querySelector(".cmc-summary-card");
    if (summaryCard) summaryCard.classList.toggle("hidden", isAllYears);
}

// KPI definitions — labels are computed dynamically from scope via getCmcKpis()
const CMC_KPI_DEFS = [
    { key: "raised",        labelKey: "raised",      fmt: "count", lower: null  },
    { key: "finished",      labelKey: "finished",    fmt: "count", lower: false },
    { key: "openBacklog",   labelKey: "backlog",     fmt: "count", lower: true  },
    { key: "notAck",        labelKey: "notAck",      fmt: "count", lower: true  },
    { key: "closureRate",   labelKey: "closure",     fmt: "pct",   lower: false },
    { key: "backlogChange", labelKey: "backlogChg",  fmt: "diff",  lower: true  },
    { key: "avgTtr",        labelKey: "ttr",         fmt: "hours", lower: true  },
    { key: "invalid",       labelKey: "invalid",     fmt: "count", lower: true  },
];

// Returns the KPI array with labels adjusted for the selected scope.
function getCmcKpis() {
    const s = cmcScope === "all" ? "" : `${cmcScope} `;
    const labelMap = {
        raised:     `${s}MR Raised`,
        finished:   `${s}MR Finished`,
        backlog:    `${s}Open Backlog`,
        notAck:     `${s}Not Acknowledged`,
        closure:    "Closure Rate",
        backlogChg: "Backlog Change",
        ttr:        "Avg TTR / MTTR",
        invalid:    "Invalid / Missing Dates",
    };
    return CMC_KPI_DEFS.map((d) => ({ ...d, label: labelMap[d.labelKey] || d.labelKey }));
}

// Keep CMC_KPIS as a live getter alias so existing render helpers still work.
function getCmcKpisLegacy() { return getCmcKpis(); }

function fmtCmcVal(v, fmt) {
    if (v === null || v === undefined) return "--";
    if (fmt === "count") return fmtNumber(v);
    if (fmt === "pct")   return fmtPercent(v);
    if (fmt === "hours") return fmtHours(v);
    if (fmt === "diff")  return v === 0 ? "0" : (v > 0 ? `+${fmtNumber(v)}` : String(fmtNumber(v)));
    return String(v);
}

function cmcSentiment(kpi, a, b) {
    if (a === null || a === undefined || b === null || b === undefined) return "neutral";
    if (kpi.lower === null) return "neutral";
    const diff = b - a;
    if (diff === 0) return "neutral";
    return kpi.lower ? (diff < 0 ? "good" : "bad") : (diff > 0 ? "good" : "bad");
}

function cmcDiffText(kpi, a, b) {
    if (a === null || a === undefined || b === null || b === undefined) return "—";
    const diff = b - a;
    const sign = diff > 0 ? "+" : diff < 0 ? "" : "";
    const arrow = diff > 0 ? "↑" : diff < 0 ? "↓" : "—";
    if (kpi.fmt === "count" || kpi.fmt === "diff") {
        const pct = a !== 0 ? ` (${sign}${Math.round((diff / Math.abs(a)) * 100)}%)` : "";
        return `${arrow} ${sign}${fmtNumber(diff)}${pct}`;
    }
    if (kpi.fmt === "pct") return `${arrow} ${sign}${Math.round(Math.abs(diff) * 10) / 10} pp`;
    if (kpi.fmt === "hours") return `${arrow} ${fmtHours(Math.abs(diff))} ${diff >= 0 ? "longer" : "shorter"}`;
    return "—";
}

function cmcInterpretation(kpi, a, b) {
    if (a === null || a === undefined || b === null || b === undefined) return "Insufficient data";
    const diff = b - a;
    if (diff === 0) return "No change";
    const interp = {
        raised:       diff > 0 ? "More MR raised" : "Fewer MR raised",
        finished:     diff > 0 ? "Closure performance improved" : "Fewer MR closed",
        openBacklog:  diff < 0 ? "Backlog reduced" : "Backlog increased",
        notAck:       diff < 0 ? "Acknowledgement improved" : "More unacknowledged MR",
        closureRate:  diff > 0 ? "Closure rate improved" : "Closure rate declined",
        backlogChange:diff < 0 ? "Backlog pressure eased" : "Backlog pressure increased",
        avgTtr:       diff < 0 ? "Faster resolution" : "Slower resolution",
        invalid:      diff < 0 ? "Data quality improved" : "Data quality worsened",
    };
    return interp[kpi.key] || "Changed";
}

function renderCmcKpiCards(sA, sB, lA, lB) {
    const grid = document.getElementById("cmc-kpi-grid");
    if (!grid) return;
    grid.innerHTML = getCmcKpis().map((kpi) => {
        const a = sA[kpi.key], b = sB[kpi.key];
        const sent = cmcSentiment(kpi, a, b);
        const sentCls = sent === "good" ? "cmc-chg-good" : sent === "bad" ? "cmc-chg-bad" : "cmc-chg-neutral";
        return `<div class="cmc-kpi-card">
            <div class="cmc-kpi-lbl">${escapeHtml(kpi.label)}</div>
            <div class="cmc-kpi-row">
                <div class="cmc-kpi-col"><span class="cmc-plbl">${escapeHtml(lA)}</span><span class="cmc-pval">${escapeHtml(fmtCmcVal(a, kpi.fmt))}</span></div>
                <div class="cmc-kpi-col"><span class="cmc-plbl">${escapeHtml(lB)}</span><span class="cmc-pval">${escapeHtml(fmtCmcVal(b, kpi.fmt))}</span></div>
                <div class="cmc-kpi-col ${sentCls}"><span class="cmc-plbl">Change</span><span class="cmc-pval">${escapeHtml(cmcDiffText(kpi, a, b))}</span></div>
            </div>
        </div>`;
    }).join("");
}

function renderCmcBarChart(sA, sB, lA, lB) {
    const canvas = ensureCanvas("cmcBarChart");
    if (!canvas) return;
    destroyChart("cmcBarChart");
    const dataA = [sA.raised, sA.finished, sA.openBacklog, sA.notAck, sA.invalid];
    const dataB = [sB.raised, sB.finished, sB.openBacklog, sB.notAck, sB.invalid];
    if (!dataA.some(Boolean) && !dataB.some(Boolean)) { renderEmptyChart("cmcBarChart", "No data for selected periods."); return; }
    const scopeStr = cmcScope === "all" ? "All Assets" : cmcScope;
    setText("cmc-bar-title", `Comparison: ${scopeStr} ${lA} vs ${lB}`);
    chartRefs.cmcBarChart = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
            labels: ["MR Raised", "Finished", "Open Backlog", "Not Ack", "Invalid"],
            datasets: [
                { label: lA, data: dataA, backgroundColor: "#3b82f6", borderRadius: 6 },
                { label: lB, data: dataB, backgroundColor: "#10b981", borderRadius: 6 },
            ],
        },
        options: mrTrackingAxisOptions(scopeStr),
    });
}

// Determine trend granularity based on period length in days.
function cmcTrendGranularity(start, end, compareStart = null, compareEnd = null) {
    const primaryDays = (end - start) / 86400000;
    const compareDays = compareStart instanceof Date && compareEnd instanceof Date
        ? (compareEnd - compareStart) / 86400000
        : primaryDays;
    const days = Math.max(primaryDays, compareDays);
    if (days <= 31) return "day";
    if (days <= 180) return "week";
    return "month";
}

// Build positional cumulative series for one period at the given granularity.
function buildCmcSeries(items, start, end, gran, relativeLabels = false) {
    const buckets = [];
    if (gran === "day") {
        const d = new Date(start); d.setHours(0, 0, 0, 0);
        const endD = new Date(end); endD.setHours(0, 0, 0, 0);
        let dayIndex = 1;
        while (d <= endD) {
            buckets.push({
                lbl: relativeLabels ? `Day ${dayIndex++}` : d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" }),
                date: new Date(d),
                r: 0,
                f: 0,
            });
            d.setDate(d.getDate() + 1);
        }
    } else if (gran === "week") {
        const d = new Date(start); d.setHours(0, 0, 0, 0);
        let wk = 1;
        while (d <= end) { buckets.push({ lbl: `Wk ${wk++}`, date: new Date(d), r: 0, f: 0 }); d.setDate(d.getDate() + 7); }
    } else {
        let y = start.getFullYear(), m = start.getMonth();
        const ey = end.getFullYear(), em = end.getMonth();
        let monthIndex = 1;
        while (y < ey || (y === ey && m <= em)) {
            buckets.push({
                lbl: relativeLabels ? `Month ${monthIndex++}` : `${MR_MONTH_LABELS[m]} ${y}`,
                date: new Date(y, m, 1),
                r: 0,
                f: 0,
            });
            m++; if (m > 11) { m = 0; y++; }
        }
    }
    const findIdx = (date) => {
        if (gran === "day") return buckets.findIndex((b) => b.date.toDateString() === date.toDateString());
        if (gran === "month") return buckets.findIndex((b) => b.date.getFullYear() === date.getFullYear() && b.date.getMonth() === date.getMonth());
        let idx = -1;
        for (let i = 0; i < buckets.length; i++) { if (buckets[i].date <= date) idx = i; else break; }
        return idx;
    };
    items.forEach((it) => {
        if (it.raised.date && it.raised.date >= start && it.raised.date <= end) { const i = findIdx(it.raised.date); if (i >= 0) buckets[i].r++; }
        if (isMrFinishedStatus(it.status) && it.finished.date && it.finished.date >= start && it.finished.date <= end) { const i = findIdx(it.finished.date); if (i >= 0) buckets[i].f++; }
    });
    let cr = 0, cf = 0;
    return buckets.map((b) => { cr += b.r; cf += b.f; return { lbl: b.lbl, raised: cr, finished: cf, backlog: Math.max(0, cr - cf) }; });
}

function renderCmcTrendChart(items, periods) {
    const canvas = ensureCanvas("cmcTrendChart");
    if (!canvas) return;
    destroyChart("cmcTrendChart");
    const isCustomMode = cmcMode === "custom";
    const gran = cmcMode === "month"
        ? "day"
        : cmcMode === "year"
            ? "month"
            : cmcTrendGranularity(periods.a.start, periods.a.end, periods.b.start, periods.b.end);
    const sA = buildCmcSeries(items, periods.a.start, periods.a.end, gran, isCustomMode);
    const sB = buildCmcSeries(items, periods.b.start, periods.b.end, gran, isCustomMode);
    if (!sA.length && !sB.length) { renderEmptyChart("cmcTrendChart", "No trend data for selected periods."); return; }
    const labels = (sA.length >= sB.length ? sA : sB).map((s) => s.lbl);
    const pad = (arr) => { const a = [...arr]; while (a.length < labels.length) a.push(null); return a; };
    const scopeStr2 = cmcScope === "all" ? "All Assets" : cmcScope;
    setText(
        "cmc-trend-title",
        isCustomMode
            ? `Custom Range Trend: ${scopeStr2} ${periods.labelA} vs ${periods.labelB}`
            : `Cumulative Trend: ${scopeStr2} ${periods.labelA} vs ${periods.labelB}`
    );
    const datasets = isCustomMode
        ? [
            {
                label: periods.labelA,
                data: pad(sA.map((s) => s.raised)),
                borderColor: "#3b82f6",
                fill: false,
                tension: 0.2,
                pointRadius: 2,
            },
            {
                label: periods.labelB,
                data: pad(sB.map((s) => s.raised)),
                borderColor: "#10b981",
                fill: false,
                tension: 0.2,
                pointRadius: 2,
                borderDash: [5, 3],
            },
        ]
        : [
            { label: `${periods.labelA} Raised`, data: pad(sA.map((s) => s.raised)), borderColor: "#3b82f6", fill: false, tension: 0.2, pointRadius: 2 },
            { label: `${periods.labelB} Raised`, data: pad(sB.map((s) => s.raised)), borderColor: "#10b981", fill: false, tension: 0.2, pointRadius: 2, borderDash: [5, 3] },
            { label: `${periods.labelA} Open`, data: pad(sA.map((s) => s.backlog)), borderColor: "#f59e0b", fill: false, tension: 0.2, pointRadius: 2 },
            { label: `${periods.labelB} Open`, data: pad(sB.map((s) => s.backlog)), borderColor: "#f97316", fill: false, tension: 0.2, pointRadius: 2, borderDash: [5, 3] },
        ];
    chartRefs.cmcTrendChart = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: {
            labels,
            datasets,
        },
        options: mrTrackingAxisOptions(scopeStr2),
    });
}

function renderCmcSummaryTable(sA, sB, lA, lB) {
    const body = document.getElementById("cmc-summary-body");
    if (!body) return;
    const thead = body.closest("table")?.querySelector("thead");
    if (thead) {
        const ths = thead.querySelectorAll("th");
        if (ths[1]) ths[1].textContent = `Period A (${lA})`;
        if (ths[2]) ths[2].textContent = `Period B (${lB})`;
    }
    body.innerHTML = getCmcKpis().map((kpi) => {
        const a = sA[kpi.key], b = sB[kpi.key];
        const sent = cmcSentiment(kpi, a, b);
        const chgCls = sent === "good" ? "cmc-chg-good" : sent === "bad" ? "cmc-chg-bad" : "cmc-chg-neutral";
        return `<tr>
            <td>${escapeHtml(kpi.label)}</td>
            <td>${escapeHtml(fmtCmcVal(a, kpi.fmt))}</td>
            <td>${escapeHtml(fmtCmcVal(b, kpi.fmt))}</td>
            <td class="${chgCls}">${escapeHtml(cmcDiffText(kpi, a, b))}</td>
            <td class="cmc-interp">${escapeHtml(cmcInterpretation(kpi, a, b))}</td>
        </tr>`;
    }).join("");
}

function renderCriticalMrComparison(rows = []) {
    populateCmcControls(rows);
    updateCmcControlVisibility();

    // Update dynamic subtitle and sync scope selector
    const subtitleEl = document.getElementById("cmc-subtitle");
    if (subtitleEl) subtitleEl.textContent = getCmcScopeSubtitle();
    const scopeEl = document.getElementById("cmc-scope");
    if (scopeEl && scopeEl.value !== cmcScope) scopeEl.value = cmcScope;
    const yearViewEl = document.getElementById("cmc-year-view");
    if (yearViewEl && yearViewEl.value !== cmcYearView) yearViewEl.value = cmcYearView;

    // Get scope-filtered items — same grouping logic as Machine Explorer group cards
    const scopeItems = getCmcScopeItems(rows);
    const scopeStr = cmcScope === "all" ? "All Assets" : cmcScope;

    // Show All Years mode
    if (cmcMode === "year" && cmcYearView === "all") {
        renderCmcAllYearsSection(scopeItems);
        renderCmcSpareTrend();
        return;
    }

    // Ensure all-years section is hidden and two-year mode elements are visible
    const allYearsSection = document.getElementById("cmc-all-years-section");
    if (allYearsSection) allYearsSection.classList.add("hidden");
    const kpiGrid = document.getElementById("cmc-kpi-grid");
    if (kpiGrid) kpiGrid.classList.remove("hidden");
    const summaryCard = document.querySelector(".cmc-summary-card");
    if (summaryCard) summaryCard.classList.remove("hidden");

    const periods = getCmcPeriods();
    if (!periods) {
        if (kpiGrid) kpiGrid.innerHTML = `<p class="cmc-empty">Select periods above to compare ${scopeStr} MR performance.</p>`;
        renderEmptyChart("cmcBarChart", "Select periods above to compare.");
        renderEmptyChart("cmcTrendChart", "Select periods above to compare.");
        const body = document.getElementById("cmc-summary-body");
        if (body) body.innerHTML = `<tr><td colspan="5" class="empty-cell">Select periods above to compare.</td></tr>`;
        return;
    }

    const sA = computeCmcStats(scopeItems, periods.a.start, periods.a.end);
    const sB = computeCmcStats(scopeItems, periods.b.start, periods.b.end);
    renderCmcKpiCards(sA, sB, periods.labelA, periods.labelB);
    renderCmcBarChart(sA, sB, periods.labelA, periods.labelB);
    renderCmcTrendChart(scopeItems, periods);
    renderCmcSummaryTable(sA, sB, periods.labelA, periods.labelB);
    renderCmcSpareTrend();
}

function buildMachineMrRows(rows = []) {
    const grouped = new Map();
    normalizeMrTrackingRows(rows).items.forEach((item) => {
        const assetId = item.assetId === "Data not available" ? "" : item.assetId;
        const key = assetId || item.equipmentKey || item.key;
        const bucket = grouped.get(key) || {
            assetId: assetId || "--",
            machineName: item.equipment || getMrMachineName(item.row),
            criticality: item.criticality || "--",
            rows: [],
            raised: 0,
            open: 0,
            finished: 0,
            ttrHours: [],
            oldestOpenAge: null,
        };
        bucket.rows.push(item);
        if (item.raised.date) bucket.raised += 1;
        if (isNormalOpenMrStatus(item.status)) {
            bucket.open += 1;
            const age = getAgeDaysFrom(item.raised.date);
            if (age !== null) bucket.oldestOpenAge = Math.max(bucket.oldestOpenAge ?? 0, age);
        }
        if (isMrFinishedStatus(item.status) && item.finished.date) bucket.finished += 1;
        const ttr = getTtrHours(item.row);
        if (ttr !== null) bucket.ttrHours.push(ttr);
        grouped.set(key, bucket);
    });
    return [...grouped.values()]
        .map((row) => ({
            ...row,
            closureRate: row.raised ? (row.finished / row.raised) * 100 : null,
            averageTtr: row.ttrHours.length ? row.ttrHours.reduce((sum, value) => sum + value, 0) / row.ttrHours.length : null,
        }))
        .sort((a, b) => b.raised - a.raised || a.machineName.localeCompare(b.machineName));
}

function populateMachineMrFilter(machineRows) {
    const select = document.getElementById("mr-machine-filter");
    if (!select) return;
    const current = mrMachineFilter;
    select.innerHTML = `<option value="all">All Machines</option>` + machineRows.map((row) => {
        const value = row.assetId !== "--" ? row.assetId : row.machineName;
        return `<option value="${escapeHtml(value)}">${escapeHtml(row.machineName)}${row.assetId !== "--" ? ` | ${escapeHtml(row.assetId)}` : ""}</option>`;
    }).join("");
    const values = new Set(["all", ...machineRows.map((row) => row.assetId !== "--" ? row.assetId : row.machineName)]);
    mrMachineFilter = values.has(current) ? current : "all";
    select.value = mrMachineFilter;
}

function renderMachineMrSection(rows = []) {
    const machineRows = buildMachineMrRows(rows);
    populateMachineMrFilter(machineRows);
    const filteredRows = mrMachineFilter === "all"
        ? machineRows
        : machineRows.filter((row) => row.assetId === mrMachineFilter || row.machineName === mrMachineFilter);
    renderMachineMrChart(filteredRows);
    renderMachineMrTable(filteredRows);
}

function renderMachineMrChart(machineRows) {
    const canvas = ensureCanvas("machineMrRaisedChart");
    if (!canvas) return;
    const rows = machineRows.slice(0, 10);
    destroyChart("machineMrRaisedChart");
    if (!rows.length) {
        renderEmptyChart("machineMrRaisedChart", "No machine MR data available.");
        return;
    }
    chartRefs.machineMrRaisedChart = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
            labels: rows.map((row) => row.machineName),
            datasets: [{ label: "MR Raised", data: rows.map((row) => row.raised), backgroundColor: "#3b82f6", borderRadius: 8 }],
        },
        options: { ...mrTrackingAxisOptions("MR Raised"), indexAxis: "y" },
    });
}

function renderMachineMrTable(machineRows) {
    const body = document.getElementById("machine-mr-table-body");
    if (!body) return;
    if (!machineRows.length) {
        body.innerHTML = `<tr><td colspan="9" class="empty-cell">No machine MR records match the selected filter.</td></tr>`;
        return;
    }
    body.innerHTML = machineRows.slice(0, 100).map((row) => `
        <tr>
            <td>${escapeHtml(row.machineName)}</td>
            <td>${escapeHtml(row.assetId)}</td>
            <td>${escapeHtml(row.criticality)}</td>
            <td>${escapeHtml(fmtNumber(row.raised))}</td>
            <td>${escapeHtml(fmtNumber(row.open))}</td>
            <td>${escapeHtml(fmtNumber(row.finished))}</td>
            <td>${escapeHtml(row.closureRate === null ? "--" : fmtPercent(row.closureRate))}</td>
            <td>${escapeHtml(row.averageTtr === null ? "--" : fmtHours(row.averageTtr))}</td>
            <td>${escapeHtml(formatDays(row.oldestOpenAge))}</td>
        </tr>
    `).join("");
}

function buildOpenSeverityRows(rows = []) {
    const grouped = new Map();
    normalizeMrTrackingRows(rows).items.filter(isMrOpenTracking).forEach((item) => {
        const serviceLevel = getMrServiceLevel(item.row);
        const bucket = grouped.get(serviceLevel) || {
            serviceLevel,
            open: 0,
            newCount: 0,
            inProgress: 0,
            ages: [],
            oldestAge: null,
            critical: 0,
            nonCritical: 0,
        };
        const age = getAgeDaysFrom(item.raised.date);
        bucket.open += 1;
        if (isMrNewStatus(item.status)) bucket.newCount += 1;
        if (isMrInProgressStatus(item.status)) bucket.inProgress += 1;
        if (age !== null) {
            bucket.ages.push(age);
            bucket.oldestAge = Math.max(bucket.oldestAge ?? 0, age);
        }
        if (isProductionCritical(item.row)) bucket.critical += 1;
        else bucket.nonCritical += 1;
        grouped.set(serviceLevel, bucket);
    });
    return [...grouped.values()]
        .map((row) => ({
            ...row,
            averageAge: row.ages.length ? row.ages.reduce((sum, value) => sum + value, 0) / row.ages.length : null,
        }))
        .sort((a, b) => b.open - a.open || a.serviceLevel.localeCompare(b.serviceLevel));
}

function renderOpenSeveritySection(rows = []) {
    const severityRows = buildOpenSeverityRows(rows);
    renderSeverityOpenChart(severityRows);
    const body = document.getElementById("severity-open-table-body");
    if (!body) return;
    if (!severityRows.length) {
        body.innerHTML = `<tr><td colspan="8" class="empty-cell">No open MR by service level.</td></tr>`;
        return;
    }
    body.innerHTML = severityRows.map((row) => `
        <tr>
            <td>${escapeHtml(row.serviceLevel)}</td>
            <td>${escapeHtml(fmtNumber(row.open))}</td>
            <td>${escapeHtml(fmtNumber(row.newCount))}</td>
            <td>${escapeHtml(fmtNumber(row.inProgress))}</td>
            <td>${escapeHtml(formatDays(row.averageAge === null ? null : Math.round(row.averageAge)))}</td>
            <td>${escapeHtml(formatDays(row.oldestAge))}</td>
            <td>${escapeHtml(fmtNumber(row.critical))}</td>
            <td>${escapeHtml(fmtNumber(row.nonCritical))}</td>
        </tr>
    `).join("");
}

function renderSeverityOpenChart(severityRows) {
    const canvas = ensureCanvas("severityOpenMrChart");
    if (!canvas) return;
    destroyChart("severityOpenMrChart");
    if (!severityRows.length) {
        renderEmptyChart("severityOpenMrChart", "No open MR by service level.");
        return;
    }
    chartRefs.severityOpenMrChart = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
            labels: severityRows.map((row) => row.serviceLevel),
            datasets: [
                { label: "New MR", data: severityRows.map((row) => row.newCount), backgroundColor: "#ef4444", borderRadius: 8 },
                { label: "In Progress MR", data: severityRows.map((row) => row.inProgress), backgroundColor: "#f59e0b", borderRadius: 8 },
            ],
        },
        options: mrTrackingAxisOptions("Open MR"),
    });
}

const PM_CM_PREVENTIVE_PATTERNS = [
    { pattern: /\bprevent(?:ive|ative)\b/i, label: "preventive" },
    { pattern: /\bplanned maintenance\b/i, label: "planned maintenance" },
    { pattern: /\bscheduled\b|\bschedule\b/i, label: "scheduled" },
    { pattern: /\broutine\b|\bperiodic\b/i, label: "routine / periodic" },
    { pattern: /\bpm\b|\bp\.m\.\b/i, label: "PM" },
    { pattern: /\binspect(?:ion)?\b|\bcheck(?:ing|list)?\b/i, label: "inspection / check" },
    { pattern: /\blubricat(?:e|ion)?\b|\bgreas(?:e|ing)?\b/i, label: "lubrication" },
    { pattern: /\bcalibrat(?:e|ion)?\b/i, label: "calibration" },
    { pattern: /\bclean(?:ing)?\b/i, label: "cleaning" },
    { pattern: /\bweekly\b|\bmonthly\b|\bquarterly\b|\bannual(?:ly)?\b/i, label: "frequency wording" },
];
const PM_CM_CORRECTIVE_PATTERNS = [
    { pattern: /\bcorrective\b|\bcm\b/i, label: "corrective" },
    { pattern: /\bbreak\s*down\b|\bbreakdown\b/i, label: "breakdown" },
    { pattern: /\brepair\b|\bfix\b|\btroubleshoot/i, label: "repair / troubleshoot" },
    { pattern: /\bfail(?:ure|ed)?\b|\bfault\b|\berror\b/i, label: "failure / fault" },
    { pattern: /\bleak(?:age)?\b|\bdamage(?:d)?\b|\bbroken\b/i, label: "leak / damage" },
    { pattern: /\balarm\b|\btrip(?:ped)?\b|\babnormal\b/i, label: "alarm / abnormal" },
    { pattern: /\burgent\b|\bemergency\b/i, label: "urgent / emergency" },
];

function matchPmCmPatterns(text, patterns) {
    const value = String(text || "");
    return patterns.filter((item) => item.pattern.test(value)).map((item) => item.label);
}

function normalizePreventiveCorrectiveMaintenanceType(value) {
    const normalized = String(value || "").trim().toUpperCase();
    if (normalized === "PREVENTIVE") return "Preventive";
    if (normalized === "CORRECTIVE") return "Corrective";
    return "";
}

function getPreventiveCorrectiveReviewKey(row = {}, index = 0) {
    return cleanExportIdentifier(row?.work_order_id || row?.wo_id)
        || cleanExportIdentifier(row?.maintenance_order_id || row?.request_id)
        || cleanExportIdentifier(row?.mr_id || row?.mr_no || row?.mr_number || row?.request_no)
        || getWorkOrderSlaRowId(row, index);
}

function loadPreventiveCorrectiveReviewDecisions() {
    if (typeof window === "undefined" || !window.localStorage) return {};
    try {
        const parsed = JSON.parse(window.localStorage.getItem(PM_CM_REVIEW_STORAGE_KEY) || "{}");
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
        return Object.fromEntries(Object.entries(parsed).map(([id, decision]) => {
            const reviewDecision = normalizePreventiveCorrectiveMaintenanceType(decision?.reviewDecision || decision?.reviewedMaintenanceType);
            if (!id || !reviewDecision) return null;
            return [id, {
                originalType: normalizePreventiveCorrectiveMaintenanceType(decision?.originalType || decision?.originalMaintenanceType),
                reviewDecision,
                reviewStatus: "Reviewed",
                reviewedAt: String(decision?.reviewedAt || ""),
                reviewNote: String(decision?.reviewNote || ""),
            }];
        }).filter(Boolean));
    } catch (error) {
        console.warn("Preventive vs Corrective review decisions could not be loaded:", error);
        return {};
    }
}

function savePreventiveCorrectiveReviewDecisions() {
    if (typeof window === "undefined" || !window.localStorage) return;
    try {
        window.localStorage.setItem(PM_CM_REVIEW_STORAGE_KEY, JSON.stringify(preventiveCorrectiveReviewDecisions));
    } catch (error) {
        console.warn("Preventive vs Corrective review decisions could not be saved:", error);
    }
}

function getPreventiveCorrectiveReviewDecision(id) {
    const decision = preventiveCorrectiveReviewDecisions[id];
    const reviewDecision = normalizePreventiveCorrectiveMaintenanceType(decision?.reviewDecision || decision?.reviewedMaintenanceType);
    return reviewDecision ? { ...decision, reviewDecision, reviewStatus: "Reviewed" } : null;
}

function findWtEntryById(id) {
    if (!Array.isArray(preventiveCorrectiveSourceRows)) return null;
    return preventiveCorrectiveSourceRows.find((r) => getPreventiveCorrectiveReviewKey(r, 0) === id) || null;
}

function setPreventiveCorrectiveReviewDecision(id, reviewDecision, originalType = "") {
    const normalizedType = normalizePreventiveCorrectiveMaintenanceType(reviewDecision);
    if (!id || !normalizedType) return;
    const prev = preventiveCorrectiveReviewDecisions[id]?.reviewDecision || "Unreviewed";
    preventiveCorrectiveReviewDecisions[id] = {
        originalType: normalizePreventiveCorrectiveMaintenanceType(originalType),
        reviewDecision: normalizedType,
        reviewStatus: "Reviewed",
        reviewedAt: new Date().toISOString(),
        reviewNote: "",
    };
    savePreventiveCorrectiveReviewDecisions();
    const row = findWtEntryById(id);
    appendEditHistory({
        reviewType: "work-type",
        woId: id,
        mrId: row ? (cleanExportIdentifier(row.maintenance_order_id || row.request_id) || "") : "",
        equipment: row ? getMachineEquipmentName(row) : "",
        machineGroup: row ? (cleanMrValue(row.machine_group) || getPerformanceMachineGroup(row) || "") : "",
        severity: row ? getMrServiceLevel(row) : "",
        field: "reviewDecision",
        previousValue: prev,
        newValue: normalizedType,
        reviewStatus: "Reviewed",
        remark: "",
        followUpAction: "",
    });
    renderSectionEditHistory("wt-review-history-body", "work-type");
    renderGlobalEditHistory();
}

function clearPreventiveCorrectiveReviewDecision(id) {
    if (!id || !preventiveCorrectiveReviewDecisions[id]) return;
    delete preventiveCorrectiveReviewDecisions[id];
    savePreventiveCorrectiveReviewDecisions();
}

function getPreventiveCorrectiveTypeLabel(type) {
    const normalized = normalizePreventiveCorrectiveMaintenanceType(type);
    return normalized || "--";
}

function getPreventiveCorrectiveTypeText(row = {}) {
    return [
        row?.maintenance_job_type,
        row?.job_trade,
        row?.maintenance_type,
        row?.request_type,
        row?.work_order_type,
        row?.job_type,
        row?.system,
    ].map(cleanMrValue).filter(Boolean).join(" | ");
}

function getPreventiveCorrectiveNarrativeText(row = {}) {
    return [
        row?.description_original,
        row?.translated_description,
        row?.description,
        row?.remarks,
        row?.notes,
        row?.problem,
        row?.details,
        // Include MR/WO identifiers so PM-prefixed numbers (e.g. "PM-00001") are flagged
        row?.work_order_id,
        row?.maintenance_order_id,
        row?.mr_number,
        row?.mr_no,
    ].map(cleanMrValue).filter(Boolean).join(" | ");
}

function classifyPreventiveCorrectiveRow(row, index) {
    const typeText = getPreventiveCorrectiveTypeText(row);
    const narrativeText = getPreventiveCorrectiveNarrativeText(row);
    const typePreventiveMatches = matchPmCmPatterns(typeText, PM_CM_PREVENTIVE_PATTERNS);
    const typeCorrectiveMatches = matchPmCmPatterns(typeText, PM_CM_CORRECTIVE_PATTERNS);
    const narrativePreventiveMatches = matchPmCmPatterns(narrativeText, PM_CM_PREVENTIVE_PATTERNS);
    const loggedType = typePreventiveMatches.length ? "preventive" : "corrective";
    const originalType = loggedType === "preventive" ? "Preventive" : "Corrective";
    const reviewMatches = originalType === "Corrective" ? [...new Set([...typePreventiveMatches, ...narrativePreventiveMatches])] : [];
    const id = getPreventiveCorrectiveReviewKey(row, index);
    const savedDecision = getPreventiveCorrectiveReviewDecision(id);
    const reviewDecision = savedDecision?.reviewDecision || "";
    const reviewStatus = reviewDecision ? "Reviewed" : "Needs Review";
    const finalType = reviewDecision || originalType;
    const finalLoggedType = finalType === "Preventive" ? "preventive" : "corrective";
    const created = getWorkOrderSlaCreatedDate(row);
    const raised = getMrRaisedDate(row);
    const description = getMrDescription(row) || narrativeText;
    const translatedDescription = cleanMrValue(row?.translated_description);
    return {
        row,
        index,
        id,
        assetId: String(row?.asset_id || row?.machine_code || row?.equipment_id || "--").trim() || "--",
        equipmentName: getMachineEquipmentName(row),
        loggedType,
        originalType,
        reviewDecision,
        finalType,
        finalLoggedType,
        reviewStatus,
        reviewedAt: savedDecision?.reviewedAt || "",
        reviewNote: savedDecision?.reviewNote || "",
        typeText: typeText || (loggedType === "preventive" ? "Preventive" : "No preventive marker found"),
        reviewNeeded: originalType === "Corrective" && reviewMatches.length > 0 && reviewStatus !== "Reviewed",
        hasPreventiveSignal: originalType === "Corrective" && reviewMatches.length > 0,
        reviewReason: reviewMatches.length ? `Preventive signal: ${reviewMatches.slice(0, 3).join(", ")}` : "No preventive signal detected",
        correctiveReason: typeCorrectiveMatches.length ? typeCorrectiveMatches.slice(0, 3).join(", ") : "Logged/assumed corrective",
        createdDate: created.date || raised.date || null,
        description,
        translatedDescription,
    };
}

function buildPreventiveCorrectiveModel(rows = []) {
    const entries = rows.map((row, index) => classifyPreventiveCorrectiveRow(row, index));
    const preventive = entries.filter((entry) => entry.finalType === "Preventive");
    const corrective = entries.filter((entry) => entry.finalType === "Corrective");
    const review = entries.filter((entry) => entry.reviewNeeded);
    const reviewedPreventive = entries.filter((entry) => entry.reviewDecision === "Preventive");
    const reviewedCorrective = entries.filter((entry) => entry.reviewDecision === "Corrective");
    return {
        entries,
        preventive,
        corrective,
        review,
        reviewedPreventive,
        reviewedCorrective,
        chartRows: [
            { key: "preventive", label: "Logged Preventive", count: preventive.length, color: "#10b981" },
            { key: "corrective", label: "Logged Corrective", count: corrective.length, color: "#ef4444" },
            { key: "review", label: "Data Review", count: review.length, color: "#f59e0b" },
        ],
    };
}

const PREVENTIVE_CORRECTIVE_CALENDAR_MONTH_ORDER = MR_MONTH_LABELS.map((_, index) => index);

function getPreventiveCorrectiveAnalysisYearMode() {
    if (!["calendar", "financial"].includes(preventiveCorrectiveAnalysisYearModeFilter)) {
        preventiveCorrectiveAnalysisYearModeFilter = "financial";
    }
    return preventiveCorrectiveAnalysisYearModeFilter;
}

function getPreventiveCorrectiveAnalysisYearValue(entry, yearMode) {
    const date = entry?.createdDate;
    if (!(date instanceof Date) || Number.isNaN(date.getTime())) return null;
    return yearMode === "calendar" ? date.getFullYear() : getMrFinancialYearStart(date);
}

function getPreventiveCorrectiveAnalysisYearLabel(year, yearMode) {
    const numericYear = Number(year);
    if (!Number.isFinite(numericYear)) return yearMode === "calendar" ? "Year" : "Financial year";
    return yearMode === "calendar" ? String(numericYear) : getMrFinancialYearLabel(numericYear);
}

function getPreventiveCorrectiveAnalysisAvailableYears(entries = [], yearMode = getPreventiveCorrectiveAnalysisYearMode()) {
    const years = new Set();
    entries.forEach((entry) => {
        const yearValue = getPreventiveCorrectiveAnalysisYearValue(entry, yearMode);
        if (Number.isFinite(yearValue)) years.add(yearValue);
    });
    return [...years].sort((a, b) => b - a);
}

function getPreventiveCorrectiveAnalysisMonthLabels(selectedYear, monthOrder = MR_FINANCIAL_MONTH_ORDER, yearMode = getPreventiveCorrectiveAnalysisYearMode()) {
    const yearValue = Number(selectedYear);
    if (!Number.isFinite(yearValue)) return monthOrder.map((monthIndex) => MR_MONTH_LABELS[monthIndex] || "");
    return monthOrder.map((monthIndex) => {
        const year = yearMode === "calendar"
            ? yearValue
            : (monthIndex + 1 >= MR_FINANCIAL_YEAR_START_MONTH ? yearValue : yearValue + 1);
        return `${MR_MONTH_LABELS[monthIndex]}-${String(year).slice(-2)}`;
    });
}

const PREVENTIVE_CORRECTIVE_ANALYSIS_TYPES = {
    preventive: {
        key: "preventive",
        label: "Logged Preventive",
        metricLabel: "Total preventive MR",
        rowLabel: "logged preventive",
        axisLabel: "Logged Preventive",
        color: "#10b981",
    },
    corrective: {
        key: "corrective",
        label: "Logged Corrective",
        metricLabel: "Total corrective MR",
        rowLabel: "logged corrective",
        axisLabel: "Logged Corrective",
        color: "#ef4444",
    },
    review: {
        key: "review",
        label: "Data Review",
        metricLabel: "Data review MR",
        rowLabel: "data review",
        axisLabel: "Data Review",
        color: "#f59e0b",
    },
    comparison: {
        key: "comparison",
        label: "Preventive vs Corrective",
        metricLabel: "Logged MR",
        rowLabel: "logged",
        axisLabel: "Logged MR",
        color: "#10b981",
        preventiveColor: "#10b981",
        correctiveColor: "#ef4444",
    },
};

function getPreventiveCorrectiveAnalysisTypeConfig() {
    if (!PREVENTIVE_CORRECTIVE_ANALYSIS_TYPES[preventiveCorrectiveAnalysisTypeFilter]) {
        preventiveCorrectiveAnalysisTypeFilter = "preventive";
    }
    return PREVENTIVE_CORRECTIVE_ANALYSIS_TYPES[preventiveCorrectiveAnalysisTypeFilter];
}

function getPreventiveCorrectiveAnalysisPeriodKey() {
    if (!["full", "ytd"].includes(preventiveCorrectiveAnalysisPeriodFilter)) {
        preventiveCorrectiveAnalysisPeriodFilter = "full";
    }
    return preventiveCorrectiveAnalysisPeriodFilter;
}

function getPreventiveCorrectiveAnalysisEntries(model, typeKey) {
    if (typeKey === "corrective") return model.corrective;
    if (typeKey === "review") return model.review;
    return model.preventive;
}

function syncPreventiveCorrectiveAnalysisTypeOption(config) {
    const select = document.getElementById("topic-preventive-analysis-type-filter");
    if (!select) return;
    select.value = config.key;
}

function syncPreventiveCorrectiveAnalysisPeriodOption(periodKey) {
    const select = document.getElementById("topic-preventive-analysis-period-filter");
    if (!select) return;
    select.value = periodKey;
}

function syncPreventiveCorrectiveAnalysisYearModeOption(yearMode) {
    const select = document.getElementById("topic-preventive-analysis-year-mode-filter");
    if (select) select.value = yearMode;
    const label = document.getElementById("topic-preventive-analysis-year-label");
    if (label) label.textContent = yearMode === "calendar" ? "Year" : "Financial Year";
    const periodSelect = document.getElementById("topic-preventive-analysis-period-filter");
    const fullPeriodOption = periodSelect?.querySelector('option[value="full"]');
    if (fullPeriodOption) fullPeriodOption.textContent = yearMode === "calendar" ? "Full Year" : "Full FY";
}

function getPreventiveCorrectiveAnalysisMetricLabel(config, periodKey) {
    if (periodKey !== "ytd") return config.metricLabel;
    if (config.key === "corrective") return "YTD corrective MR";
    if (config.key === "review") return "YTD data review MR";
    return "YTD preventive MR";
}

function getPreventiveCorrectiveAnalysisYearRange(selectedYear, yearMode) {
    if (selectedYear === null || selectedYear === undefined) return null;
    const yearValue = Number(selectedYear);
    if (!Number.isFinite(yearValue)) return null;
    if (yearMode === "calendar") {
        return {
            start: new Date(yearValue, 0, 1, 0, 0, 0, 0),
            end: new Date(yearValue + 1, 0, 1, 0, 0, 0, 0),
        };
    }
    return getMrFinancialYearRange(yearValue);
}

function getPreventiveCorrectiveAnalysisYtdReferenceDate(selectedYear, yearMode) {
    const range = getPreventiveCorrectiveAnalysisYearRange(selectedYear, yearMode);
    if (!range) return null;
    const referenceDate = getWorkOrderSlaReferenceDate();
    if (!(referenceDate instanceof Date) || Number.isNaN(referenceDate.getTime())) {
        return new Date(range.end.getTime() - 1);
    }
    if (referenceDate < range.start) return null;
    if (referenceDate >= range.end) return new Date(range.end.getTime() - 1);
    const cutoff = new Date(referenceDate);
    cutoff.setHours(23, 59, 59, 999);
    return cutoff;
}

function getPreventiveCorrectiveAnalysisBaseMonthOrder(yearMode) {
    return yearMode === "calendar" ? PREVENTIVE_CORRECTIVE_CALENDAR_MONTH_ORDER : MR_FINANCIAL_MONTH_ORDER;
}

function getPreventiveCorrectiveAnalysisMonthOrder(selectedYear, periodKey, yearMode) {
    const baseMonthOrder = getPreventiveCorrectiveAnalysisBaseMonthOrder(yearMode);
    if (periodKey !== "ytd" || selectedYear === null || selectedYear === undefined) return baseMonthOrder;
    const cutoffDate = getPreventiveCorrectiveAnalysisYtdReferenceDate(selectedYear, yearMode);
    if (!cutoffDate) return [];
    const position = baseMonthOrder.indexOf(cutoffDate.getMonth());
    return position >= 0 ? baseMonthOrder.slice(0, position + 1) : [];
}

function populatePreventiveCorrectiveFinancialYearOptions(availableYears = [], yearMode = getPreventiveCorrectiveAnalysisYearMode()) {
    const select = document.getElementById("topic-preventive-financial-year-filter");
    if (!select) return;
    if (!availableYears.length) {
        preventiveCorrectiveFinancialYearFilter = "";
        select.innerHTML = `<option value="">${escapeHtml(yearMode === "calendar" ? "No year data" : "No financial year data")}</option>`;
        select.disabled = true;
        return;
    }
    if (!availableYears.includes(Number(preventiveCorrectiveFinancialYearFilter))) {
        preventiveCorrectiveFinancialYearFilter = String(availableYears[0]);
    }
    select.innerHTML = availableYears
        .map((year) => `<option value="${escapeHtml(String(year))}">${escapeHtml(getPreventiveCorrectiveAnalysisYearLabel(year, yearMode))}</option>`)
        .join("");
    select.disabled = false;
    select.value = preventiveCorrectiveFinancialYearFilter;
}

function buildPreventiveCorrectiveAnalysisModel(rows = []) {
    const baseModel = buildPreventiveCorrectiveModel(rows);
    const config = getPreventiveCorrectiveAnalysisTypeConfig();
    const periodKey = getPreventiveCorrectiveAnalysisPeriodKey();
    const yearMode = getPreventiveCorrectiveAnalysisYearMode();
    const datedEntries = baseModel.entries
        .filter((entry) => entry.createdDate instanceof Date && !Number.isNaN(entry.createdDate.getTime()));
    const selectedEntries = getPreventiveCorrectiveAnalysisEntries(baseModel, config.key)
        .filter((entry) => entry.createdDate instanceof Date && !Number.isNaN(entry.createdDate.getTime()));
    const availableYears = getPreventiveCorrectiveAnalysisAvailableYears(datedEntries, yearMode);
    if (!availableYears.includes(Number(preventiveCorrectiveFinancialYearFilter))) {
        preventiveCorrectiveFinancialYearFilter = availableYears.length ? String(availableYears[0]) : "";
    }
    const selectedYear = availableYears.includes(Number(preventiveCorrectiveFinancialYearFilter))
        ? Number(preventiveCorrectiveFinancialYearFilter)
        : null;
    const monthOrder = getPreventiveCorrectiveAnalysisMonthOrder(selectedYear, periodKey, yearMode);
    const monthLabels = getPreventiveCorrectiveAnalysisMonthLabels(selectedYear, monthOrder, yearMode);
    const ytdCutoffDate = periodKey === "ytd" ? getPreventiveCorrectiveAnalysisYtdReferenceDate(selectedYear, yearMode) : null;

    // Bucket a set of entries into the selected financial/calendar year's months.
    const countByMonth = (entries) => {
        const counts = new Array(monthOrder.length).fill(0);
        let total = 0;
        entries.forEach((entry) => {
            if (!(entry.createdDate instanceof Date) || Number.isNaN(entry.createdDate.getTime())) return;
            if (selectedYear !== null && getPreventiveCorrectiveAnalysisYearValue(entry, yearMode) !== selectedYear) return;
            if (periodKey === "ytd" && (!ytdCutoffDate || entry.createdDate > ytdCutoffDate)) return;
            const monthPosition = monthOrder.indexOf(entry.createdDate.getMonth());
            if (monthPosition < 0) return;
            total += 1;
            counts[monthPosition] += 1;
        });
        return { counts, total };
    };

    const isComparison = config.key === "comparison";
    let monthlyCounts;
    let totalRows;
    let comparison = null;
    if (isComparison) {
        const prev = countByMonth(baseModel.preventive);
        const corr = countByMonth(baseModel.corrective);
        comparison = {
            preventive: prev.counts,
            corrective: corr.counts,
            preventiveTotal: prev.total,
            correctiveTotal: corr.total,
        };
        monthlyCounts = prev.counts;            // kept for callers that read a single series
        totalRows = prev.total + corr.total;
    } else {
        const single = countByMonth(selectedEntries);
        monthlyCounts = single.counts;
        totalRows = single.total;
    }

    return {
        availableYears,
        selectedYear,
        selectedYearLabel: selectedYear !== null ? getPreventiveCorrectiveAnalysisYearLabel(selectedYear, yearMode) : getPreventiveCorrectiveAnalysisYearLabel(null, yearMode),
        monthLabels,
        monthlyCounts,
        comparison,
        totalRows,
        config,
        periodKey,
        yearMode,
        ytdCutoffDate,
    };
}

function renderPreventiveCorrectiveAnalysis(rows = []) {
    const model = buildPreventiveCorrectiveAnalysisModel(rows);
    populatePreventiveCorrectiveFinancialYearOptions(model.availableYears, model.yearMode);
    syncPreventiveCorrectiveAnalysisTypeOption(model.config);
    syncPreventiveCorrectiveAnalysisPeriodOption(model.periodKey);
    syncPreventiveCorrectiveAnalysisYearModeOption(model.yearMode);

    const title = document.getElementById("preventive-analysis-chart-title");
    const note = document.getElementById("preventive-analysis-note");
    const head = document.getElementById("preventive-analysis-table-head");
    const body = document.getElementById("preventive-analysis-table-body");
    const canvas = ensureCanvas("preventiveCorrectiveAnalysisChart");
    const scopeLabel = model.periodKey === "ytd" ? "YTD" : "Total";
    const metricLabel = getPreventiveCorrectiveAnalysisMetricLabel(model.config, model.periodKey);

    if (title) title.textContent = model.selectedYear ? `${model.config.label} ${scopeLabel} - ${model.selectedYearLabel}` : `${model.config.label} ${scopeLabel}`;
    if (head) {
        const monthHeaders = model.monthLabels.length
            ? model.monthLabels.map((label) => `<th>${escapeHtml(label)}</th>`).join("")
            : "<th>No data</th>";
        head.innerHTML = `<th>Metric</th>${monthHeaders}`;
    }

    if (!model.availableYears.length) {
        if (note) note.textContent = "No dated Preventive vs Corrective rows are available for financial year analysis.";
        if (body) body.innerHTML = `<tr><td colspan="2" class="empty-cell">No dated Preventive vs Corrective rows are available for financial year analysis.</td></tr>`;
        destroyChart("preventiveCorrectiveAnalysisChart");
        if (canvas) renderEmptyChart("preventiveCorrectiveAnalysisChart", "No Preventive vs Corrective financial year data available.");
        scheduleEmbeddedHeightPost();
        return;
    }

    if (body) {
        const metricRow = (label, values) => `
            <tr>
                <td>${escapeHtml(label)}</td>
                ${values.map((value) => `<td>${escapeHtml(fmtNumber(value))}</td>`).join("")}
            </tr>`;
        if (!model.monthLabels.length) {
            body.innerHTML = `<tr><td colspan="2" class="empty-cell">YTD has not started for ${escapeHtml(model.selectedYearLabel)}.</td></tr>`;
        } else if (model.comparison) {
            body.innerHTML = metricRow("Logged Preventive", model.comparison.preventive)
                + metricRow("Logged Corrective", model.comparison.corrective);
        } else {
            body.innerHTML = metricRow(metricLabel, model.monthlyCounts);
        }
    }

    const periodText = model.periodKey === "ytd" && model.ytdCutoffDate
        ? ` through ${fmtDateOnly(model.ytdCutoffDate)}`
        : "";
    const noteParts = model.comparison
        ? [`${fmtNumber(model.comparison.preventiveTotal)} preventive vs ${fmtNumber(model.comparison.correctiveTotal)} corrective rows counted by created or raised date in ${model.selectedYearLabel}${periodText}.`]
        : [`${fmtNumber(model.totalRows)} ${model.config.rowLabel} row${model.totalRows === 1 ? "" : "s"} counted by created or raised date in ${model.selectedYearLabel}${periodText}.`];
    if (note) note.textContent = noteParts.join(" ");

    destroyChart("preventiveCorrectiveAnalysisChart");
    if (!canvas) return;
    if (!model.totalRows) {
        const emptyMessage = model.periodKey === "ytd" && !model.ytdCutoffDate
            ? `YTD has not started for ${model.selectedYearLabel}.`
            : `No ${model.config.rowLabel} rows found in ${model.selectedYearLabel}${periodText}.`;
        renderEmptyChart("preventiveCorrectiveAnalysisChart", emptyMessage);
        scheduleEmbeddedHeightPost();
        return;
    }

    const analysisDatasets = model.comparison
        ? [
            {
                label: "Logged Preventive",
                data: model.comparison.preventive,
                backgroundColor: model.config.preventiveColor || "#10b981",
                borderRadius: 8,
                maxBarThickness: 34,
            },
            {
                label: "Logged Corrective",
                data: model.comparison.corrective,
                backgroundColor: model.config.correctiveColor || "#ef4444",
                borderRadius: 8,
                maxBarThickness: 34,
            },
        ]
        : [{
            label: metricLabel,
            data: model.monthlyCounts,
            backgroundColor: model.config.color,
            borderRadius: 8,
            maxBarThickness: 34,
        }];
    chartRefs.preventiveCorrectiveAnalysisChart = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
            labels: model.monthLabels,
            datasets: analysisDatasets,
        },
        options: {
            ...mrTrackingAxisOptions(model.config.axisLabel),
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { color: "#475569", maxRotation: 0, minRotation: 0 },
                },
                y: {
                    beginAtZero: true,
                    title: { display: true, text: model.config.axisLabel },
                    ticks: { precision: 0, color: "#475569" },
                    grid: { color: "rgba(148, 163, 184, 0.18)" },
                },
            },
        },
    });
    scheduleEmbeddedHeightPost();
}

function renderPreventiveCorrectiveChart(model) {
    const canvas = ensureCanvas("preventiveCorrectiveChart");
    if (!canvas) return;
    destroyChart("preventiveCorrectiveChart");
    if (!model.entries.length) {
        renderEmptyChart("preventiveCorrectiveChart", "No imported MR / work-order rows in the current scope.");
        return;
    }
    chartRefs.preventiveCorrectiveChart = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
            labels: model.chartRows.map((row) => row.label),
            datasets: [{
                label: "Rows",
                data: model.chartRows.map((row) => row.count),
                backgroundColor: model.chartRows.map((row) => row.color),
                borderRadius: 8,
                maxBarThickness: 58,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => `${fmtNumber(ctx.raw)} row(s)`,
                        afterLabel: (ctx) => ctx.dataIndex === 2 ? "Data Review is unresolved only; confirmed Preventive rows move out of Corrective." : "",
                    },
                },
            },
            scales: {
                x: { grid: { display: false }, ticks: { color: "#475569", font: { weight: "700" } } },
                y: { beginAtZero: true, ticks: { precision: 0, color: "#475569" }, grid: { color: "rgba(148, 163, 184, 0.2)" } },
            },
        },
    });
}

function getPreventiveCorrectiveListRows(model, filter) {
    if (filter === "preventive") return model.preventive;
    if (filter === "corrective") return model.corrective;
    if (filter === "all") return model.entries;
    return model.review;
}

function renderPreventiveCorrectiveReviewActions(entry) {
    const id = escapeHtml(entry.id);
    const originalType = escapeHtml(entry.originalType || "");
    if (entry.reviewNeeded) {
        return `
            <div class="pm-cm-action-group">
                <button type="button" class="pm-cm-review-btn preventive" data-pm-cm-review-action="confirm-preventive" data-pm-cm-review-id="${id}" data-pm-cm-original-type="${originalType}">Confirm Preventive</button>
                <button type="button" class="pm-cm-review-btn corrective" data-pm-cm-review-action="confirm-corrective" data-pm-cm-review-id="${id}" data-pm-cm-original-type="${originalType}">Confirm Corrective</button>
            </div>
        `;
    }
    if (entry.reviewStatus === "Reviewed") {
        return `
            <div class="pm-cm-action-group">
                <button type="button" class="pm-cm-review-btn reset" data-pm-cm-review-action="reset" data-pm-cm-review-id="${id}">Undo Review</button>
            </div>
        `;
    }
    return `<span class="cell-sub">No action</span>`;
}

function getPreventiveCorrectiveReviewStatusDisplay(entry) {
    if (entry.reviewStatus === "Reviewed") {
        return {
            className: "reviewed",
            label: `Reviewed ${getPreventiveCorrectiveTypeLabel(entry.reviewDecision)}`,
            detail: `Final: ${getPreventiveCorrectiveTypeLabel(entry.finalType)}${entry.reviewedAt ? ` | ${fmtDateTime(entry.reviewedAt)}` : ""}`,
        };
    }
    if (entry.reviewNeeded) {
        return {
            className: "review",
            label: entry.reviewStatus,
            detail: entry.reviewReason,
        };
    }
    return {
        className: entry.finalLoggedType,
        label: entry.finalType === "Preventive" ? "OK Preventive" : "OK Corrective",
        detail: `Final: ${getPreventiveCorrectiveTypeLabel(entry.finalType)}`,
    };
}

function renderPreventiveCorrectiveList(model) {
    const body = document.getElementById("pm-cm-list-body");
    const select = document.getElementById("pm-cm-list-filter");
    const subtitle = document.getElementById("pm-cm-list-subtitle");
    const currentFilter = select?.value || preventiveCorrectiveListFilter || "review";
    preventiveCorrectiveListFilter = currentFilter;
    if (select && select.value !== currentFilter) select.value = currentFilter;
    if (!body) return;
    const rows = getPreventiveCorrectiveListRows(model, currentFilter);
    const listLabel = currentFilter === "preventive" ? "logged preventive"
        : currentFilter === "corrective" ? "logged corrective"
            : currentFilter === "all" ? "all imported"
                : "data review";
    if (subtitle) {
        subtitle.textContent = `${fmtNumber(rows.length)} ${listLabel} row${rows.length === 1 ? "" : "s"} in the current scope.`;
    }
    setText(
        "pm-cm-review-summary",
        `${fmtNumber(model.review.length)} row${model.review.length === 1 ? "" : "s"} require review | Reviewed Preventive: ${fmtNumber(model.reviewedPreventive.length)} | Reviewed Corrective: ${fmtNumber(model.reviewedCorrective.length)}`
    );
    if (!rows.length) {
        const message = currentFilter === "review"
            ? "No unresolved corrective logs with preventive signals in the current scope."
            : "No rows match this maintenance type selection.";
        body.innerHTML = `<tr><td colspan="8" class="empty-cell">${escapeHtml(message)}</td></tr>`;
        return;
    }
    body.innerHTML = rows.slice(0, 250).map((entry) => {
        const reviewDisplay = getPreventiveCorrectiveReviewStatusDisplay(entry);
        return `
            <tr>
                <td>
                    <div class="cell-title">${escapeHtml(entry.id)}</div>
                    <div class="cell-sub">${escapeHtml(getMrStatus(entry.row))}</div>
                </td>
                <td>
                    <div class="cell-title">${escapeHtml(entry.equipmentName || "--")}</div>
                    <div class="cell-sub">${escapeHtml(entry.assetId)}</div>
                </td>
                <td>
                    <span class="pm-cm-type-pill ${escapeHtml(entry.loggedType)}">${escapeHtml(entry.loggedType === "preventive" ? "Preventive" : "Corrective")}</span>
                    <div class="cell-sub">Original: ${escapeHtml(getPreventiveCorrectiveTypeLabel(entry.originalType))}</div>
                    <div class="cell-sub">${escapeHtml(entry.typeText || "--")}</div>
                </td>
                <td>
                    <span class="pm-cm-type-pill ${escapeHtml(reviewDisplay.className)}">${escapeHtml(reviewDisplay.label)}</span>
                    <div class="cell-sub">${escapeHtml(reviewDisplay.detail)}</div>
                </td>
                <td>${escapeHtml(entry.createdDate ? fmtDateOnly(entry.createdDate) : "--")}</td>
                <td class="description-cell">${escapeHtml(entry.description || "--")}</td>
                <td class="description-cell">${escapeHtml(entry.translatedDescription || "--")}</td>
                <td class="pm-cm-action-cell">${renderPreventiveCorrectiveReviewActions(entry)}</td>
            </tr>
        `;
    }).join("");
}

function handlePreventiveCorrectiveReviewAction(event) {
    const button = event.target.closest("[data-pm-cm-review-action]");
    if (!button) return;
    const id = button.dataset.pmCmReviewId || "";
    const action = button.dataset.pmCmReviewAction || "";
    if (!id) return;
    if (action === "confirm-preventive") {
        setPreventiveCorrectiveReviewDecision(id, "Preventive", button.dataset.pmCmOriginalType || "Corrective");
    } else if (action === "confirm-corrective") {
        setPreventiveCorrectiveReviewDecision(id, "Corrective", button.dataset.pmCmOriginalType || "Corrective");
    } else if (action === "reset") {
        clearPreventiveCorrectiveReviewDecision(id);
    } else {
        return;
    }
    renderPreventiveCorrectiveSection(preventiveCorrectiveSourceRows);
}

// Source rows last passed to the Preventive vs Corrective section. Cached so
// the topic-scoped year/month filter can re-render against the same dataset
// without needing the SLA flow to re-run.
let preventiveCorrectiveSourceRows = [];

function renderPreventiveCorrectiveSection(rows = []) {
    preventiveCorrectiveSourceRows = Array.isArray(rows) ? rows : [];
    const scoped = filterOverviewRowsByDate(
        preventiveCorrectiveSourceRows,
        topicPreventiveYearFilter,
        topicPreventiveMonthFilter,
    );
    const model = buildPreventiveCorrectiveModel(scoped);
    window._lastPreventiveCorrectiveModel = model;
    renderPreventiveCorrectiveChart(model);
    renderPreventiveCorrectiveList(model);
    setText(
        "pm-cm-review-note",
        `${fmtNumber(model.review.length)} unresolved corrective row${model.review.length === 1 ? "" : "s"} contain preventive wording and need data review. Data Review excludes confirmed rows. Reviewed Preventive: ${fmtNumber(model.reviewedPreventive.length)} | Reviewed Corrective: ${fmtNumber(model.reviewedCorrective.length)}.`
    );
    renderPreventiveCorrectiveAnalysis(preventiveCorrectiveSourceRows);
    renderWorkTypeClassificationTable();
    renderSectionEditHistory("wt-review-history-body", "work-type");
}

function hasWorkOrderSlaFields(rows = []) {
    return Array.isArray(rows) && rows.some((row) => row?.request_created_time || row?.actual_start_time || row?.actual_end_time || row?.priority || row?.service_level);
}

function getWorkOrderSlaSourceRows(rows = []) {
    // Apply the page-level category filter to whichever source we resolve.
    if (hasWorkOrderSlaFields(allWorkOrderRowsCache)) return applyCategoryFilter(allWorkOrderRowsCache);
    if (hasWorkOrderSlaFields(rows)) return applyCategoryFilter(rows);
    const currentRows = getWorkOrderRows(getManagement());
    if (hasWorkOrderSlaFields(currentRows)) return currentRows;
    return applyCategoryFilter(Array.isArray(allWorkOrderRowsCache) && allWorkOrderRowsCache.length ? allWorkOrderRowsCache : currentRows);
}

function getWorkOrderSlaDefaultYear(rows = []) {
    const meta = downtimePayload?.meta || {};
    if (woSlaYearFilter) return woSlaYearFilter;
    if (meta.period === "all_years") return "all";
    const candidate = parseDateValue(meta.reference_end || meta.period_end || meta.period_start);
    return candidate ? String(candidate.getFullYear()) : "all";
}

function formatWorkOrderSlaMonthLabel(monthKey) {
    if (!monthKey || !monthKey.includes("-")) return monthKey || "All Months";
    const [year, month] = monthKey.split("-");
    const monthIndex = Number(month) - 1;
    return `${MR_MONTH_LABELS[monthIndex] || month} ${year}`;
}

function populateWorkOrderSlaFilters(rows = []) {
    const yearSelect = document.getElementById("wo-sla-year-filter");
    const monthSelect = document.getElementById("wo-sla-month-filter");
    if (!yearSelect || !monthSelect) return;

    const availableYears = [...new Set(
        rows
            .map((row) => getWorkOrderSlaCreatedDate(row).date)
            .filter(Boolean)
            .map((date) => String(date.getFullYear()))
    )].sort((a, b) => Number(b) - Number(a));

    const requestedYear = getWorkOrderSlaDefaultYear(rows);
    woSlaYearFilter = requestedYear === "all" || availableYears.includes(requestedYear) ? requestedYear : "all";
    yearSelect.innerHTML = `<option value="all">All Years</option>` + availableYears.map((year) => `<option value="${escapeHtml(year)}">${escapeHtml(year)}</option>`).join("");
    yearSelect.value = woSlaYearFilter;

    const availableMonths = [...new Set(
        rows
            .map((row) => getWorkOrderSlaCreatedDate(row).date)
            .filter((date) => date && (woSlaYearFilter === "all" || String(date.getFullYear()) === woSlaYearFilter))
            .map((date) => formatMonthKey(date))
    )].sort().reverse();

    if (!availableMonths.includes(woSlaMonthFilter)) woSlaMonthFilter = "all";
    monthSelect.innerHTML = `<option value="all">All Months</option>` + availableMonths.map((monthKey) => {
        return `<option value="${escapeHtml(monthKey)}">${escapeHtml(formatWorkOrderSlaMonthLabel(monthKey))}</option>`;
    }).join("");
    monthSelect.value = woSlaMonthFilter;
}

function getWorkOrderSlaScopeFilters() {
    return {
        equipment: document.getElementById("mr-tracking-equipment")?.value || "all",
        criticality: document.getElementById("group-criticality-filter")?.value || "",
        year: document.getElementById("wo-sla-year-filter")?.value || woSlaYearFilter || "all",
        month: document.getElementById("wo-sla-month-filter")?.value || woSlaMonthFilter || "all",
    };
}

function getScopedWorkOrderSlaRows(rows = []) {
    const scope = getWorkOrderSlaScopeFilters();
    return rows.filter((row) => {
        if (scope.equipment !== "all") {
            const equipment = getMrTrackingText(row, MR_EQUIPMENT_ALIASES, getMachineEquipmentName(row));
            if (equipment !== scope.equipment) return false;
        }
        if (scope.criticality) {
            const criticality = String(row?.criticality || row?.normalized_criticality || "").trim();
            if (criticality !== scope.criticality) return false;
        }
        const createdDate = getWorkOrderSlaCreatedDate(row).date;
        if (scope.year !== "all" && (!createdDate || String(createdDate.getFullYear()) !== scope.year)) return false;
        if (scope.month !== "all" && (!createdDate || formatMonthKey(createdDate) !== scope.month)) return false;
        return true;
    });
}

function getWorkOrderSlaRowId(row, index = 0) {
    return cleanExportIdentifier(row?.work_order_id || row?.wo_id)
        || cleanExportIdentifier(row?.maintenance_order_id || row?.request_id)
        || `WO-${index + 1}`;
}

function getWorkOrderSlaStatusPriority(status) {
    return WORK_ORDER_SLA_STATUS_ORDER[status] ?? 99;
}

function classifyWorkOrderSlaRow(row, index, referenceDate) {
    const severity = getWorkOrderSlaSeverity(row);
    const created = getWorkOrderSlaCreatedDate(row);
    const actualStart = getWorkOrderSlaStartDate(row);
    const actualEnd = getWorkOrderSlaEndDate(row);
    const statusText = getMrStatus(row);
    const isFinished = isWorkOrderSlaFinished(statusText, row);
    const normalizedStartDate = created.date && actualStart.date && actualStart.date < created.date ? created.date : actualStart.date;
    const responseHoursRaw = created.date && normalizedStartDate ? getDurationHours(created.date, normalizedStartDate) : null;
    const completionHoursRaw = created.date && actualEnd.date ? getDurationHours(created.date, actualEnd.date) : null;
    const startToEndHours = actualStart.date && actualEnd.date ? getDurationHours(actualStart.date, actualEnd.date) : null;
    const openAgeHoursRaw = created.date ? getDurationHours(created.date, referenceDate) : null;
    const responseHours = Number.isFinite(responseHoursRaw) && responseHoursRaw >= 0 ? responseHoursRaw : null;
    const completionHours = Number.isFinite(completionHoursRaw) && completionHoursRaw >= 0 ? completionHoursRaw : null;
    const openAgeHours = Number.isFinite(openAgeHoursRaw) && openAgeHoursRaw >= 0 ? openAgeHoursRaw : null;
    const targetLines = [];
    const actualLines = [];
    const delayLines = [];
    const issues = [];
    let delayHours = null;

    if (severity.responseTargetHours !== null) targetLines.push(`Response <= ${formatSlaDuration(severity.responseTargetHours)}`);
    if (severity.completionTargetHours !== null) targetLines.push(`Completion <= ${formatSlaDuration(severity.completionTargetHours)}`);

    if (created.invalid || actualStart.invalid || actualEnd.invalid) issues.push("Invalid date format");
    if (completionHoursRaw !== null && completionHoursRaw < 0) issues.push("Actual End before Created Date");
    if (startToEndHours !== null && startToEndHours < 0) issues.push("Actual End before Actual Start");

    if (responseHours !== null) actualLines.push(`Response ${formatSlaDuration(responseHours)}`);
    else if (!isFinished && severity.responseTargetHours !== null && openAgeHours !== null && !actualStart.date) actualLines.push(`Response clock ${formatSlaDuration(openAgeHours)}`);
    if (completionHours !== null) actualLines.push(`Completion ${formatSlaDuration(completionHours)}`);
    else if (!isFinished && severity.completionTargetHours !== null && openAgeHours !== null && !(severity.responseTargetHours !== null && !actualStart.date)) actualLines.push(`Open age ${formatSlaDuration(openAgeHours)}`);
    if (!actualLines.length && openAgeHours !== null) actualLines.push(`Open age ${formatSlaDuration(openAgeHours)}`);

    let slaStatus = "Met Target";
    let validForSla = false;
    let breachedChecks = [];

    if (issues.length) {
        slaStatus = "Missing Data";
        delayLines.push(issues[0]);
    } else if (isFinished && !actualStart.date) {
        slaStatus = "Missing Start Date";
        delayLines.push("Completed record missing Actual Start Date");
    } else if (isFinished && !actualEnd.date) {
        slaStatus = "Missing End Date";
        delayLines.push("Completed record missing Actual End Date");
    } else if (!created.date) {
        slaStatus = "Missing Data";
        delayLines.push("Missing Created Date");
    } else if (severity.key === WORK_ORDER_SLA_UNCLASSIFIED.key) {
        slaStatus = "Missing Data";
        delayLines.push("Unclassified Severity/Priority");
    } else if (severity.responseTargetHours === null && severity.completionTargetHours === null) {
        slaStatus = "No SLA Target";
        delayLines.push("No response/completion target configured");
    } else {
        validForSla = true;
        const checks = [];
        if (severity.responseTargetHours !== null) {
            const responseActual = responseHours !== null ? responseHours : (!isFinished ? openAgeHours : null);
            const responseBreached = Number.isFinite(responseActual) && responseActual > severity.responseTargetHours;
            checks.push({
                label: "Response",
                actualHours: responseActual,
                targetHours: severity.responseTargetHours,
                deltaHours: Number.isFinite(responseActual) ? responseActual - severity.responseTargetHours : null,
                breached: responseBreached,
            });
        }
        if (severity.completionTargetHours !== null) {
            const completionActual = isFinished ? completionHours : openAgeHours;
            const completionBreached = Number.isFinite(completionActual) && completionActual > severity.completionTargetHours;
            checks.push({
                label: "Completion",
                actualHours: completionActual,
                targetHours: severity.completionTargetHours,
                deltaHours: Number.isFinite(completionActual) ? completionActual - severity.completionTargetHours : null,
                breached: completionBreached,
            });
        }
        breachedChecks = checks.filter((check) => check.breached);
        if (breachedChecks.length) {
            slaStatus = isFinished ? "Late" : "Open Overdue";
            delayHours = Math.max(...breachedChecks.map((check) => Number(check.deltaHours || 0)), 0);
            breachedChecks.forEach((check) => {
                delayLines.push(`${check.label} ${formatSlaDuration(check.deltaHours, { signed: true })}`);
            });
        } else {
            slaStatus = "Met Target";
        }
    }

    if (!delayLines.length && slaStatus === "Met Target") delayLines.push("Within target");

    return {
        row,
        index,
        id: getWorkOrderSlaRowId(row, index),
        requestId: cleanExportIdentifier(row?.maintenance_order_id || row?.request_id),
        equipmentName: getMachineEquipmentName(row),
        severity,
        severityRaw: cleanMrValue(row?.service_level ?? row?.priority ?? row?.severity),
        statusText,
        created,
        actualStart,
        actualEnd,
        responseHours,
        completionHours,
        openAgeHours,
        targetLines,
        actualLines,
        delayLines,
        delayHours,
        validForSla,
        slaStatus,
        repairHours: Number.isFinite(startToEndHours) && startToEndHours >= 0 ? startToEndHours : null,
    };
}

function buildWorkOrderSlaModel(rows = [], referenceDate = getWorkOrderSlaReferenceDate()) {
    warnWorkOrderSlaFieldAvailability(rows);
    const entries = rows.map((row, index) => classifyWorkOrderSlaRow(row, index, referenceDate));
    const severityRows = [...WORK_ORDER_SLA_TARGETS, WORK_ORDER_SLA_UNCLASSIFIED].map((severity) => {
        const severityEntries = entries.filter((entry) => entry.severity.key === severity.key);
        const metTarget = severityEntries.filter((entry) => entry.slaStatus === "Met Target").length;
        const late = severityEntries.filter((entry) => entry.slaStatus === "Late").length;
        const openOverdue = severityEntries.filter((entry) => entry.slaStatus === "Open Overdue").length;
        const missingData = severityEntries.filter((entry) => isWorkOrderSlaMissingStatus(entry.slaStatus)).length;
        const validCount = metTarget + late + openOverdue;
        const responseValues = severityEntries.map((entry) => entry.responseHours).filter((value) => Number.isFinite(value));
        const completionValues = severityEntries.map((entry) => entry.completionHours).filter((value) => Number.isFinite(value));
        const responseAverage = responseValues.length ? responseValues.reduce((sum, value) => sum + value, 0) / responseValues.length : null;
        const completionAverage = completionValues.length ? completionValues.reduce((sum, value) => sum + value, 0) / completionValues.length : null;
        return {
            severity,
            entries: severityEntries,
            total: severityEntries.length,
            metTarget,
            late,
            openOverdue,
            missingData,
            validCount,
            slaPct: validCount ? (metTarget / validCount) * 100 : null,
            responseAverage,
            responseCount: responseValues.length,
            completionAverage,
            completionCount: completionValues.length,
        };
    });

    const validCount = severityRows.reduce((sum, row) => sum + row.validCount, 0);
    const metTarget = severityRows.reduce((sum, row) => sum + row.metTarget, 0);
    const late = severityRows.reduce((sum, row) => sum + row.late, 0);
    const openOverdue = severityRows.reduce((sum, row) => sum + row.openOverdue, 0);
    const missingData = severityRows.reduce((sum, row) => sum + row.missingData, 0);
    const overallCompliance = validCount ? (metTarget / validCount) * 100 : null;
    const worstSeverity = severityRows
        .filter((row) => row.severity.key !== WORK_ORDER_SLA_UNCLASSIFIED.key && row.validCount > 0)
        .sort((a, b) => {
            const pctDiff = (a.slaPct ?? Infinity) - (b.slaPct ?? Infinity);
            if (pctDiff !== 0) return pctDiff;
            const impactDiff = (b.late + b.openOverdue) - (a.late + a.openOverdue);
            if (impactDiff !== 0) return impactDiff;
            return a.severity.rank - b.severity.rank;
        })[0] || null;
    const drilldownRows = entries
        .filter((entry) => entry.slaStatus !== "Met Target")
        .sort((a, b) => {
            const statusDiff = getWorkOrderSlaStatusPriority(a.slaStatus) - getWorkOrderSlaStatusPriority(b.slaStatus);
            if (statusDiff !== 0) return statusDiff;
            const delayDiff = Number(b.delayHours || 0) - Number(a.delayHours || 0);
            if (delayDiff !== 0) return delayDiff;
            return (b.created.date?.getTime() || 0) - (a.created.date?.getTime() || 0);
        });

    return {
        entries,
        severityRows,
        totalRows: entries.length,
        validCount,
        metTarget,
        late,
        openOverdue,
        missingData,
        overallCompliance,
        worstSeverity,
        drilldownRows,
        referenceDate,
    };
}

function renderWorkOrderSlaPills(counts = []) {
    if (!counts.length || !counts.some((item) => item.count > 0)) {
        return `<span class="wo-response-pill neutral">No overdue open WO</span>`;
    }
    return counts
        .filter((item) => item.count > 0)
        .map((item) => `<span class="wo-response-pill danger">${escapeHtml(item.label)}: ${escapeHtml(fmtNumber(item.count))}</span>`)
        .join("");
}

function renderWorkOrderSlaSeverityLabel(severity) {
    const label = severity?.label || WORK_ORDER_SLA_UNCLASSIFIED.label;
    const code = severity?.shortLabel || "N/A";
    return `
        <span class="wo-response-severity-label">
            <span class="wo-response-severity-code">${escapeHtml(code)}</span>
            <span>${escapeHtml(label)}</span>
        </span>
    `;
}

function renderWorkOrderSlaSummaryTable(model) {
    const body = document.getElementById("wo-sla-severity-body");
    if (!body) return;
    if (!model.severityRows.some((row) => row.total > 0)) {
        body.innerHTML = `<tr><td colspan="8" class="empty-cell">No work orders match the current SLA scope.</td></tr>`;
        return;
    }
    body.innerHTML = model.severityRows
        .filter((row) => row.total > 0)
        .map((row) => {
            const completeness = row.total ? (row.validCount / row.total) * 100 : null;
            const completenessTone = completeness === null ? "" : (completeness >= 90 ? "good" : (completeness >= 70 ? "" : "bad"));
            const missingCell = row.missingData > 0
                ? `<button type="button" class="sla-missing-link" data-missing-severity="${escapeHtml(row.severity.key)}" title="Show the ${escapeHtml(fmtNumber(row.missingData))} ${escapeHtml(row.severity.label)} missing-data records">${escapeHtml(fmtNumber(row.missingData))}</button>`
                : `<span class="wo-response-metric missing">0</span>`;
            return `
            <tr>
                <td>${renderWorkOrderSlaSeverityLabel(row.severity)}</td>
                <td>${escapeHtml(fmtNumber(row.total))}</td>
                <td><span class="wo-response-metric good">${escapeHtml(fmtNumber(row.metTarget))}</span></td>
                <td><span class="wo-response-metric bad">${escapeHtml(fmtNumber(row.late))}</span></td>
                <td><span class="wo-response-metric bad">${escapeHtml(fmtNumber(row.openOverdue))}</span></td>
                <td>${missingCell}</td>
                <td><span class="wo-response-metric ${escapeHtml(completenessTone)}">${escapeHtml(completeness === null ? "N/A" : fmtPercent(completeness))}</span></td>
                <td>${escapeHtml(row.slaPct === null ? "N/A" : fmtPercent(row.slaPct))}</td>
            </tr>
        `;
        })
        .join("");
}

function renderWorkOrderSlaAverageTable(bodyId, rows, type) {
    const body = document.getElementById(bodyId);
    if (!body) return;
    const metricRows = rows.filter((row) => row.total > 0);
    if (!metricRows.length) {
        body.innerHTML = `<tr><td colspan="4" class="empty-cell">No work orders match the current SLA scope.</td></tr>`;
        return;
    }
    body.innerHTML = metricRows.map((row) => {
        const average = type === "response" ? row.responseAverage : row.completionAverage;
        const count = type === "response" ? row.responseCount : row.completionCount;
        const targetHours = type === "response" ? row.severity.responseTargetHours : row.severity.completionTargetHours;
        const tone = getSlaMetricTone(average, targetHours);
        const variance = Number.isFinite(average) && Number.isFinite(targetHours) ? average - targetHours : null;
        return `
            <tr>
                <td>${renderWorkOrderSlaSeverityLabel(row.severity)}</td>
                <td>
                    <div class="cell-title wo-response-metric ${escapeHtml(tone)}">${escapeHtml(Number.isFinite(average) ? formatSlaDuration(average) : "--")}</div>
                    <div class="cell-sub">${escapeHtml(fmtNumber(count))} measured WO</div>
                </td>
                <td>${escapeHtml(Number.isFinite(targetHours) ? formatSlaDuration(targetHours) : "N/A")}</td>
                <td class="wo-response-metric ${escapeHtml(variance === null ? "missing" : tone)}">${escapeHtml(variance === null ? "N/A" : formatSlaDuration(variance, { signed: true }))}</td>
            </tr>
        `;
    }).join("");
}

function renderWorkOrderSlaDrilldownTable(model) {
    const body = document.getElementById("wo-sla-drilldown-body");
    const countNode = document.getElementById("wo-sla-drilldown-count");
    if (countNode) countNode.textContent = `${fmtNumber(model.drilldownRows.length)} row${model.drilldownRows.length === 1 ? "" : "s"}`;
    if (!body) return;
    if (!model.drilldownRows.length) {
        body.innerHTML = `<tr><td colspan="10" class="empty-cell">No late, overdue, or missing-data work orders in the current SLA scope.</td></tr>`;
        return;
    }
    body.innerHTML = model.drilldownRows.map((entry) => {
        const severityDetail = entry.severityRaw || entry.severity.label;
        const targetHtml = entry.targetLines.length
            ? `<div class="cell-title">${escapeHtml(entry.targetLines[0])}</div>${entry.targetLines[1] ? `<div class="cell-sub">${escapeHtml(entry.targetLines[1])}</div>` : ""}`
            : `<div class="cell-title">No SLA target</div>`;
        const actualHtml = entry.actualLines.length
            ? `<div class="cell-title">${escapeHtml(entry.actualLines[0])}</div>${entry.actualLines[1] ? `<div class="cell-sub">${escapeHtml(entry.actualLines[1])}</div>` : ""}`
            : `<div class="cell-title">--</div>`;
        const delayHtml = entry.delayLines.length
            ? `<div class="cell-title">${escapeHtml(entry.delayLines[0])}</div>${entry.delayLines[1] ? `<div class="cell-sub">${escapeHtml(entry.delayLines[1])}</div>` : ""}`
            : `<div class="cell-title">--</div>`;
        return `
            <tr>
                <td>
                    <div class="cell-title">${escapeHtml(entry.id)}</div>
                    ${entry.requestId ? `<div class="cell-sub">${escapeHtml(entry.requestId)}</div>` : ""}
                </td>
                <td>
                    <div class="cell-title">${escapeHtml(entry.equipmentName)}</div>
                    <div class="cell-sub">${escapeHtml(String(entry.row?.asset_id || entry.row?.machine_code || "--").trim() || "--")}</div>
                </td>
                <td>
                    <div class="cell-title">${escapeHtml(entry.severity.label)}</div>
                    <div class="cell-sub">${escapeHtml(severityDetail || "--")}</div>
                </td>
                <td>${escapeHtml(fmtDateTime(entry.created.date || entry.created.raw))}</td>
                <td>${escapeHtml(fmtDateTime(entry.actualStart.date || entry.actualStart.raw))}</td>
                <td>${escapeHtml(fmtDateTime(entry.actualEnd.date || entry.actualEnd.raw))}</td>
                <td>${targetHtml}</td>
                <td>${actualHtml}</td>
                <td>${delayHtml}</td>
                <td><span class="wo-response-status ${escapeHtml(getSlaStatusTone(entry.slaStatus))}">${escapeHtml(entry.slaStatus)}</span></td>
            </tr>
        `;
    }).join("");
}

// ── Data-cleansing review layer (localStorage; never touches raw source) ──────
function loadDataCleansingReview() {
    if (typeof window === "undefined" || !window.localStorage) return {};
    try {
        const parsed = JSON.parse(window.localStorage.getItem(DATA_CLEANSING_STORAGE_KEY) || "{}");
        return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch (error) {
        console.warn("Data cleansing review could not be loaded:", error);
        return {};
    }
}

function saveDataCleansingReview() {
    if (typeof window === "undefined" || !window.localStorage) return;
    try {
        window.localStorage.setItem(DATA_CLEANSING_STORAGE_KEY, JSON.stringify(dataCleansingReview));
    } catch (error) {
        console.warn("Data cleansing review could not be saved:", error);
    }
}

function getCleansingKey(entry) {
    const wo = cleanExportIdentifier(entry.row?.work_order_id || entry.row?.wo_id);
    if (wo) return `wo:${wo}`;
    const mr = cleanExportIdentifier(entry.row?.maintenance_order_id || entry.row?.request_id || entry.row?.mr_id);
    if (mr) return `mr:${mr}`;
    return `k:${entry.id}:${entry.created?.raw || ""}`;
}

function getCleansingRecord(key) {
    const rec = dataCleansingReview[key] || {};
    const status = CLEANSING_STATUS_OPTIONS.includes(rec.cleansingStatus) ? rec.cleansingStatus : "Open";
    return { cleansingStatus: status, remark: String(rec.remark || ""), followUpAction: String(rec.followUpAction || ""), updatedAt: rec.updatedAt || "" };
}

function findSlaEntryByCleansingKey(key) {
    if (!lastWorkOrderSlaModel) return null;
    return lastWorkOrderSlaModel.entries.find((e) => getCleansingKey(e) === key) || null;
}

function setCleansingField(key, field, value) {
    if (!key || !["cleansingStatus", "remark", "followUpAction"].includes(field)) return;
    const prev = getCleansingRecord(key);
    const prevValue = prev[field];
    if (prevValue === value) return;
    const next = { ...prev, [field]: value, updatedAt: new Date().toISOString() };
    if (next.cleansingStatus === "Open" && !next.remark.trim() && !next.followUpAction.trim()) {
        delete dataCleansingReview[key];
    } else {
        dataCleansingReview[key] = next;
    }
    saveDataCleansingReview();
    // Capture history context from the SLA model entry.
    const entry = findSlaEntryByCleansingKey(key);
    appendEditHistory({
        reviewType: "missing-data",
        woId: entry ? entry.id : key,
        mrId: entry ? (entry.requestId || "") : "",
        equipment: entry ? entry.equipmentName : "",
        machineGroup: entry ? getSlaMachineGroup(entry.row) : "",
        severity: entry ? entry.severity.label : "",
        field,
        previousValue: prevValue,
        newValue: value,
        cleansingStatus: next.cleansingStatus,
        remark: next.remark,
        followUpAction: next.followUpAction,
    });
    renderSectionEditHistory("missing-data-history-body", "missing-data");
    renderGlobalEditHistory();
}

function isClearedStatus(status) {
    return CLEANSING_CLEARED_STATUSES.has(status);
}

// ── Unified Edit History (Missing Data + Work Type + future review sections) ──
const EDIT_HISTORY_KEY = "downtime.editHistory.v1";
const EDIT_HISTORY_LIMIT = 2000;
let editHistory = loadEditHistory();
let editHistoryPage = 0;
const EDIT_HISTORY_PAGE_SIZE = 30;
let editHistoryFilters = { reviewType: "all", search: "", machineGroup: "all", severity: "all" };

function loadEditHistory() {
    if (typeof window === "undefined" || !window.localStorage) return [];
    try {
        const p = JSON.parse(window.localStorage.getItem(EDIT_HISTORY_KEY) || "[]");
        return Array.isArray(p) ? p : [];
    } catch { return []; }
}

function saveEditHistory() {
    if (typeof window === "undefined" || !window.localStorage) return;
    try {
        if (editHistory.length > EDIT_HISTORY_LIMIT) editHistory = editHistory.slice(0, EDIT_HISTORY_LIMIT);
        window.localStorage.setItem(EDIT_HISTORY_KEY, JSON.stringify(editHistory));
    } catch (e) { console.warn("Edit history save failed:", e); }
}

function appendEditHistory(entry) {
    editHistory.unshift({
        id: `eh-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        editedAt: new Date().toISOString(),
        ...entry,
    });
    saveEditHistory();
}

function renderSectionEditHistory(containerId, reviewType) {
    const body = document.getElementById(containerId);
    if (!body) return;
    const entries = editHistory.filter((h) => h.reviewType === reviewType);
    if (!entries.length) {
        body.innerHTML = `<p class="dr-history-empty">No edits recorded yet for this section.</p>`;
        return;
    }
    body.innerHTML = entries.slice(0, 100).map((h) => `
        <div class="eh-item">
            <span class="eh-time">${escapeHtml(fmtDateTime(new Date(h.editedAt)))}</span>
            <span class="eh-who">${escapeHtml(h.woId || h.mrId || "--")}</span>
            <span class="eh-field">${escapeHtml(h.field)}</span>
            <span class="eh-from">${escapeHtml(String(h.previousValue || ""))}</span>
            <span class="eh-arrow">→</span>
            <span class="eh-to">${escapeHtml(String(h.newValue || ""))}</span>
            <span class="eh-equip">${escapeHtml(h.equipment || "")}</span>
        </div>`).join("");
}

function renderGlobalEditHistory() {
    const body = document.getElementById("global-edit-history-body");
    const countEl = document.getElementById("global-edit-history-count");
    if (!body) return;
    const f = editHistoryFilters;
    const term = (f.search || "").trim().toLowerCase();
    const filtered = editHistory.filter((h) => {
        if (f.reviewType !== "all" && h.reviewType !== f.reviewType) return false;
        if (f.machineGroup !== "all" && h.machineGroup !== f.machineGroup) return false;
        if (f.severity !== "all" && h.severity !== f.severity) return false;
        if (term) {
            const hay = [h.woId, h.mrId, h.equipment, h.field, h.newValue].join(" ").toLowerCase();
            if (!hay.includes(term)) return false;
        }
        return true;
    });
    const pageStart = editHistoryPage * EDIT_HISTORY_PAGE_SIZE;
    const pageEntries = filtered.slice(pageStart, pageStart + EDIT_HISTORY_PAGE_SIZE);
    const totalPages = Math.ceil(filtered.length / EDIT_HISTORY_PAGE_SIZE) || 1;
    if (countEl) countEl.textContent = `${fmtNumber(filtered.length)} edits across all review sections`;
    renderGlobalEditHistoryPagination(filtered.length, totalPages);
    body.innerHTML = pageEntries.length ? pageEntries.map((h) => `
        <tr>
            <td><span class="eh-type-badge eh-type-${escapeHtml(h.reviewType || "")}">${escapeHtml(REVIEW_TYPE_LABELS[h.reviewType] || h.reviewType || "--")}</span></td>
            <td>${escapeHtml(h.woId || "--")}</td>
            <td>${escapeHtml(h.mrId || "--")}</td>
            <td>${escapeHtml(h.equipment || "--")}</td>
            <td>${escapeHtml(h.machineGroup || "--")}</td>
            <td>${escapeHtml(h.severity || "--")}</td>
            <td>${escapeHtml(h.field || "--")}</td>
            <td class="eh-prev">${escapeHtml(String(h.previousValue || ""))}</td>
            <td class="eh-next">${escapeHtml(String(h.newValue || ""))}</td>
            <td>${escapeHtml(h.cleansingStatus || h.reviewStatus || "--")}</td>
            <td>${escapeHtml(h.remark || "")}</td>
            <td>${escapeHtml(h.followUpAction || "")}</td>
            <td class="eh-time-cell">${escapeHtml(h.editedAt ? fmtDateTime(new Date(h.editedAt)) : "--")}</td>
        </tr>`).join("")
        : `<tr><td colspan="13" class="empty-cell">No edits match the current filters.</td></tr>`;
}

function renderGlobalEditHistoryPagination(total, totalPages) {
    const bar = document.getElementById("global-edit-history-pagination");
    if (!bar) return;
    bar.innerHTML = total <= EDIT_HISTORY_PAGE_SIZE ? "" : `
        <button type="button" class="page-btn" id="gh-prev" ${editHistoryPage === 0 ? "disabled" : ""}>← Prev</button>
        <span class="page-info">Page ${editHistoryPage + 1} / ${totalPages}</span>
        <button type="button" class="page-btn" id="gh-next" ${editHistoryPage >= totalPages - 1 ? "disabled" : ""}>Next →</button>`;
}

function populateGlobalEditHistoryFilters() {
    const typeSelect = document.getElementById("gh-reviewtype-filter");
    const groupSelect = document.getElementById("gh-group-filter");
    if (!groupSelect) return;
    const groups = [...new Set(editHistory.map((h) => h.machineGroup).filter(Boolean))].sort();
    const cur = editHistoryFilters.machineGroup;
    groupSelect.innerHTML = `<option value="all">All Machine Groups</option>` +
        groups.map((g) => `<option value="${escapeHtml(g)}"${g === cur ? " selected" : ""}>${escapeHtml(g)}</option>`).join("");
    groupSelect.value = groups.includes(cur) ? cur : "all";
    if (typeSelect) typeSelect.value = editHistoryFilters.reviewType;
}

const REVIEW_TYPE_LABELS = {
    "missing-data": "Missing Data",
    "work-type": "Work Type",
    "data-reliability": "Data Reliability",
};

// ── Work Type Classification Review ──────────────────────────────────────────
const WORK_TYPE_REVIEW_KEY = "downtime.workTypeReview.v1";
const WORK_TYPE_STATUS_OPTIONS = ["Open", "Checking", "Confirmed Incorrect", "Confirmed Correct", "Corrected", "To Exclude"];
const WORK_TYPE_CLEARED_STATUSES = new Set(["Confirmed Correct", "Corrected", "To Exclude"]);
let workTypeReview = loadWorkTypeReview();
let workTypeReviewPage = 0;
const WORK_TYPE_PAGE_SIZE = 25;
let workTypeReviewFilter = "open";  // all | open | cleared
let workTypeReviewSearch = "";

function loadWorkTypeReview() {
    if (typeof window === "undefined" || !window.localStorage) return {};
    try {
        const p = JSON.parse(window.localStorage.getItem(WORK_TYPE_REVIEW_KEY) || "{}");
        return p && typeof p === "object" && !Array.isArray(p) ? p : {};
    } catch { return {}; }
}

function saveWorkTypeReview() {
    if (typeof window === "undefined" || !window.localStorage) return;
    try {
        window.localStorage.setItem(WORK_TYPE_REVIEW_KEY, JSON.stringify(workTypeReview));
    } catch (e) { console.warn("Work type review save failed:", e); }
}

function getWorkTypeReviewRecord(key) {
    const r = workTypeReview[key] || {};
    return {
        reviewStatus: WORK_TYPE_STATUS_OPTIONS.includes(r.reviewStatus) ? r.reviewStatus : "Open",
        suggestedWorkType: String(r.suggestedWorkType || "Possible Preventive / PM"),
        remark: String(r.remark || ""),
        followUpAction: String(r.followUpAction || ""),
        updatedAt: r.updatedAt || "",
    };
}

function setWorkTypeReviewField(key, field, value, entryCtx) {
    const prev = getWorkTypeReviewRecord(key);
    const prevValue = prev[field];
    if (prevValue === value) return;
    const allowed = ["reviewStatus", "suggestedWorkType", "remark", "followUpAction"];
    if (!allowed.includes(field)) return;
    const next = { ...prev, [field]: value, updatedAt: new Date().toISOString() };
    if (next.reviewStatus === "Open" && !next.remark.trim() && !next.followUpAction.trim() && next.suggestedWorkType === "Possible Preventive / PM") {
        delete workTypeReview[key];
    } else {
        workTypeReview[key] = next;
    }
    saveWorkTypeReview();
    appendEditHistory({
        reviewType: "work-type",
        woId: entryCtx?.woId || "",
        mrId: entryCtx?.mrId || "",
        equipment: entryCtx?.equipment || "",
        machineGroup: entryCtx?.machineGroup || "",
        severity: entryCtx?.severity || "",
        field,
        previousValue: prevValue,
        newValue: value,
        reviewStatus: next.reviewStatus,
        remark: next.remark,
        followUpAction: next.followUpAction,
    });
    if (field === "reviewStatus") renderWorkTypeClassificationTable();
    renderSectionEditHistory("wt-review-history-body", "work-type");
    renderGlobalEditHistory();
}

function buildWorkTypeClassificationEntries() {
    // Uses the existing Preventive/Corrective model (already computed from preventiveCorrectiveSourceRows).
    // "Work type classification" entries are Corrective rows that have preventive signals
    // in their description — these are the `reviewNeeded === true` entries.
    const model = window._lastPreventiveCorrectiveModel;
    if (!model) return [];
    return model.entries.filter((e) => e.reviewNeeded || (e.originalType === "Corrective" && e.hasPreventiveSignal));
}

function renderWorkTypeClassificationTable() {
    const body = document.getElementById("wt-classification-body");
    const countEl = document.getElementById("wt-classification-count");
    if (!body) return;
    let entries = buildWorkTypeClassificationEntries();
    const term = workTypeReviewSearch.trim().toLowerCase();
    if (workTypeReviewFilter !== "all") {
        entries = entries.filter((e) => {
            const rec = getWorkTypeReviewRecord(e.id);
            const cleared = WORK_TYPE_CLEARED_STATUSES.has(rec.reviewStatus);
            return workTypeReviewFilter === "cleared" ? cleared : !cleared;
        });
    }
    if (term) {
        entries = entries.filter((e) => {
            const hay = [e.id, e.requestId, e.equipmentName, e.description].join(" ").toLowerCase();
            return hay.includes(term);
        });
    }
    const totalPages = Math.ceil(entries.length / WORK_TYPE_PAGE_SIZE) || 1;
    if (workTypeReviewPage >= totalPages) workTypeReviewPage = 0;
    const pageEntries = entries.slice(workTypeReviewPage * WORK_TYPE_PAGE_SIZE, (workTypeReviewPage + 1) * WORK_TYPE_PAGE_SIZE);
    if (countEl) {
        const total = buildWorkTypeClassificationEntries().length;
        const cleared = buildWorkTypeClassificationEntries().filter((e) => WORK_TYPE_CLEARED_STATUSES.has(getWorkTypeReviewRecord(e.id).reviewStatus)).length;
        countEl.textContent = `${fmtNumber(entries.length)} shown | ${fmtNumber(total)} flagged (${fmtNumber(cleared)} reviewed)`;
    }
    renderWorkTypeClassificationPagination(entries.length, totalPages);
    if (!pageEntries.length) {
        body.innerHTML = `<tr><td colspan="12" class="empty-cell">No flagged records match the current filters.</td></tr>`;
        return;
    }
    body.innerHTML = pageEntries.map((entry) => {
        const key = entry.id;
        const rec = getWorkTypeReviewRecord(key);
        const cleared = WORK_TYPE_CLEARED_STATUSES.has(rec.reviewStatus);
        const statusOpts = WORK_TYPE_STATUS_OPTIONS
            .map((o) => `<option value="${escapeHtml(o)}"${o === rec.reviewStatus ? " selected" : ""}>${escapeHtml(o)}</option>`).join("");
        const machineGroup = cleanMrValue(entry.row?.machine_group) || getPerformanceMachineGroup(entry.row) || "--";
        const ctxAttr = `data-wt-key="${escapeHtml(key)}" data-wt-wo="${escapeHtml(entry.id)}" data-wt-mr="${escapeHtml(entry.requestId || "")}" data-wt-equip="${escapeHtml(entry.equipmentName)}" data-wt-group="${escapeHtml(machineGroup)}" data-wt-sev="${escapeHtml(entry.severity?.label || "")}"`;
        return `
            <tr class="${cleared ? "cleansing-cleared-row" : ""}">
                <td><div class="cell-title">${escapeHtml(entry.id)}</div></td>
                <td>${escapeHtml(entry.requestId || "--")}</td>
                <td>${escapeHtml(entry.equipmentName)}</td>
                <td>${escapeHtml(getMachineAssetId(entry.row) || "--")}</td>
                <td>${escapeHtml(machineGroup)}</td>
                <td><span class="pm-cm-type-pill ${escapeHtml(entry.loggedType)}">${escapeHtml(entry.originalType || entry.loggedType)}</span></td>
                <td><span class="missing-field-tag">${escapeHtml(rec.suggestedWorkType)}</span></td>
                <td class="cleansing-desc-cell" title="${escapeHtml(entry.description || "")}">${escapeHtml(entry.description || "--")}</td>
                <td class="cleansing-desc-cell">${escapeHtml(entry.reviewReason || "--")}</td>
                <td><select class="cleansing-input cleansing-status-select wt-input" data-wt-field="reviewStatus" ${ctxAttr}>${statusOpts}</select></td>
                <td><input type="text" class="cleansing-input cleansing-text wt-input" data-wt-field="remark" ${ctxAttr} value="${escapeHtml(rec.remark)}" placeholder="Remark"></td>
                <td><input type="text" class="cleansing-input cleansing-text wt-input" data-wt-field="followUpAction" ${ctxAttr} value="${escapeHtml(rec.followUpAction)}" placeholder="Action"></td>
            </tr>`;
    }).join("");
}

function renderWorkTypeClassificationPagination(total, totalPages) {
    const bar = document.getElementById("wt-pagination");
    if (!bar) return;
    bar.innerHTML = total <= WORK_TYPE_PAGE_SIZE ? "" : `
        <button type="button" class="page-btn" id="wt-prev" ${workTypeReviewPage === 0 ? "disabled" : ""}>← Prev</button>
        <span class="page-info">Page ${workTypeReviewPage + 1} / ${totalPages} (${fmtNumber(total)} records)</span>
        <button type="button" class="page-btn" id="wt-next" ${workTypeReviewPage >= totalPages - 1 ? "disabled" : ""}>Next →</button>`;
}

// ── Missing-data classification + views ──────────────────────────────────────
function getSlaMachineGroup(row) {
    return cleanMrValue(row?.machine_group) || getPerformanceMachineGroup(row) || "Unknown / Review";
}

function classifyMissingFieldType(entry) {
    if (entry.slaStatus === "Missing Start Date") return "Missing Actual Start";
    if (entry.slaStatus === "Missing End Date") return "Missing Actual End";
    const delayText = (entry.delayLines || []).join(" ").toLowerCase();
    if (delayText.includes("invalid") || delayText.includes("before")) return "Invalid Date";
    if (!getMachineAssetId(entry.row)) return "Missing Asset";
    if (entry.actualStart?.date && entry.actualEnd?.date && entry.repairHours === null) return "Missing Duration";
    return "Others";
}

function isOneMinuteMttrEntry(entry) {
    return entry.repairHours !== null && entry.repairHours >= 0 && Math.round(entry.repairHours * 60) <= 1;
}

function slaExportDateTime(part) {
    if (!part) return "--";
    if (part.date) return fmtDateTime(part.date);
    return part.raw ? String(part.raw) : "--";
}

function slaDurationText(hours) {
    return Number.isFinite(hours) ? formatSlaDuration(hours) : "--";
}

function missingEntryView(entry) {
    const key = getCleansingKey(entry);
    const rec = getCleansingRecord(key);
    return {
        key,
        woId: entry.id,
        mrId: entry.requestId || "",
        equipment: entry.equipmentName,
        assetId: getMachineAssetId(entry.row) || "--",
        machineGroup: getSlaMachineGroup(entry.row),
        severityLabel: entry.severity.label,
        severityRaw: entry.severityRaw || "",
        created: slaExportDateTime(entry.created),
        actualStart: slaExportDateTime(entry.actualStart),
        actualEnd: slaExportDateTime(entry.actualEnd),
        duration: slaDurationText(entry.repairHours),
        missingFieldType: classifyMissingFieldType(entry),
        slaStatus: entry.slaStatus,
        openAge: slaDurationText(entry.openAgeHours),
        completionDelay: entry.delayHours != null ? formatSlaDuration(entry.delayHours, { signed: true }) : "--",
        description: getMrDescription(entry.row) || "--",
        translatedDescription: String(entry.row?.translated_description || "").trim(),
        cleansingStatus: rec.cleansingStatus,
        remark: rec.remark,
        followUpAction: rec.followUpAction,
        cleared: isClearedStatus(rec.cleansingStatus),
    };
}

function getMissingDataEntries(model) {
    if (!model) return [];
    return model.entries.filter((entry) => isWorkOrderSlaMissingStatus(entry.slaStatus));
}

function getOneMinuteMttrEntries(model) {
    if (!model) return [];
    return model.entries.filter(isOneMinuteMttrEntry);
}

function entryMatchesSlaStatusFilter(entry, value) {
    if (value === "all") return true;
    if (value === "Missing Data") return isWorkOrderSlaMissingStatus(entry.slaStatus);
    return entry.slaStatus === value;
}

function getFilteredMissingDataEntries(model) {
    if (!model) return [];
    const f = missingDataFilters;
    const term = f.search.trim().toLowerCase();
    // Default scope is missing-data rows, but the SLA Status filter can broaden it.
    const base = f.slaStatus === "all" || !["all", "Missing Data"].includes(f.slaStatus)
        ? model.entries
        : getMissingDataEntries(model);
    return base.filter((entry) => {
        if (!entryMatchesSlaStatusFilter(entry, f.slaStatus)) return false;
        if (f.severity !== "all" && entry.severity.key !== f.severity) return false;
        if (f.fieldType !== "all" && classifyMissingFieldType(entry) !== f.fieldType) return false;
        if (f.machineGroup !== "all" && getSlaMachineGroup(entry.row) !== f.machineGroup) return false;
        const status = getCleansingRecord(getCleansingKey(entry)).cleansingStatus;
        if (f.cleared === "open" && isClearedStatus(status)) return false;
        if (f.cleared === "cleared" && !isClearedStatus(status)) return false;
        if (term) {
            const hay = [entry.id, entry.requestId, entry.equipmentName, getMachineAssetId(entry.row), getMrDescription(entry.row)].join(" ").toLowerCase();
            if (!hay.includes(term)) return false;
        }
        return true;
    });
}

function populateMissingDataFilters(model) {
    const select = document.getElementById("missing-data-group-filter");
    if (!select || !model) return;
    const groups = [...new Set(model.entries.map((entry) => getSlaMachineGroup(entry.row)).filter(Boolean))].sort();
    const current = missingDataFilters.machineGroup;
    select.innerHTML = `<option value="all">All Machine Groups</option>` +
        groups.map((group) => `<option value="${escapeHtml(group)}">${escapeHtml(group)}</option>`).join("");
    select.value = groups.includes(current) ? current : "all";
    missingDataFilters.machineGroup = select.value;
}

function syncMissingDataFilterControls() {
    const map = {
        "missing-data-severity-filter": missingDataFilters.severity,
        "missing-data-fieldtype-filter": missingDataFilters.fieldType,
        "missing-data-status-filter": missingDataFilters.slaStatus,
        "missing-data-group-filter": missingDataFilters.machineGroup,
        "missing-data-cleared-filter": missingDataFilters.cleared,
        "missing-data-search": missingDataFilters.search,
    };
    Object.entries(map).forEach(([id, value]) => {
        const el = document.getElementById(id);
        if (el && el.value !== value) el.value = value;
    });
}

function renderMissingDataRow(entry) {
    const v = missingEntryView(entry);
    const statusOptions = CLEANSING_STATUS_OPTIONS
        .map((opt) => `<option value="${escapeHtml(opt)}"${opt === v.cleansingStatus ? " selected" : ""}>${escapeHtml(opt)}</option>`)
        .join("");
    return `
        <tr class="${v.cleared ? "cleansing-cleared-row" : ""}">
            <td><div class="cell-title">${escapeHtml(v.woId)}</div></td>
            <td>${escapeHtml(v.mrId || "--")}</td>
            <td>${escapeHtml(v.equipment)}</td>
            <td>${escapeHtml(v.assetId)}</td>
            <td>${escapeHtml(v.machineGroup)}</td>
            <td>
                <div class="cell-title">${escapeHtml(v.severityLabel)}</div>
                ${v.severityRaw ? `<div class="cell-sub">${escapeHtml(v.severityRaw)}</div>` : ""}
            </td>
            <td>${escapeHtml(v.created)}</td>
            <td>${escapeHtml(v.actualStart)}</td>
            <td>${escapeHtml(v.actualEnd)}</td>
            <td>${escapeHtml(v.duration)}</td>
            <td><span class="missing-field-tag">${escapeHtml(v.missingFieldType)}</span></td>
            <td><span class="wo-response-status ${escapeHtml(getSlaStatusTone(v.slaStatus))}">${escapeHtml(v.slaStatus)}</span></td>
            <td>${escapeHtml(v.openAge)}</td>
            <td>${escapeHtml(v.completionDelay)}</td>
            <td class="cleansing-desc-cell" title="${escapeHtml(v.description)}">${escapeHtml(v.description)}</td>
            <td><select class="cleansing-input cleansing-status-select" data-cleansing-key="${escapeHtml(v.key)}" data-cleansing-field="cleansingStatus" aria-label="Cleansing status">${statusOptions}</select></td>
            <td><input type="text" class="cleansing-input cleansing-text" data-cleansing-key="${escapeHtml(v.key)}" data-cleansing-field="remark" value="${escapeHtml(v.remark)}" placeholder="Add remark"></td>
            <td><input type="text" class="cleansing-input cleansing-text" data-cleansing-key="${escapeHtml(v.key)}" data-cleansing-field="followUpAction" value="${escapeHtml(v.followUpAction)}" placeholder="Action"></td>
        </tr>`;
}

function renderMissingDataDrilldown() {
    const body = document.getElementById("missing-data-body");
    const countNode = document.getElementById("missing-data-count");
    if (!body) return;
    const model = lastWorkOrderSlaModel;
    populateMissingDataFilters(model);
    syncMissingDataFilterControls();
    if (!model) {
        body.innerHTML = `<tr><td colspan="18" class="empty-cell">Open the SLA Compliance section to load work-order data.</td></tr>`;
        if (countNode) countNode.textContent = "--";
        return;
    }
    if (!window.missingDataPage) window.missingDataPage = 0;
    const MISSING_PAGE_SIZE = 25;
    const entries = getFilteredMissingDataEntries(model);
    const totalPages = Math.ceil(entries.length / MISSING_PAGE_SIZE) || 1;
    if (window.missingDataPage >= totalPages) window.missingDataPage = 0;
    const pageStart = window.missingDataPage * MISSING_PAGE_SIZE;
    const pageEntries = entries.slice(pageStart, pageStart + MISSING_PAGE_SIZE);
    if (countNode) {
        const missingAll = getMissingDataEntries(model);
        const clearedCount = missingAll.filter((e) => isClearedStatus(getCleansingRecord(getCleansingKey(e)).cleansingStatus)).length;
        countNode.textContent = `${fmtNumber(entries.length)} shown | ${fmtNumber(missingAll.length)} total missing-data records (${fmtNumber(clearedCount)} cleared)`;
    }
    const paginationBar = document.getElementById("missing-data-pagination");
    if (paginationBar) {
        paginationBar.innerHTML = entries.length <= MISSING_PAGE_SIZE ? "" : `
            <button type="button" class="page-btn" id="md-prev" ${window.missingDataPage === 0 ? "disabled" : ""}>&#8592; Prev</button>
            <span class="page-info">Page ${window.missingDataPage + 1} / ${totalPages} &nbsp;&bull;&nbsp; ${fmtNumber(entries.length)} records</span>
            <button type="button" class="page-btn" id="md-next" ${window.missingDataPage >= totalPages - 1 ? "disabled" : ""}>Next &#8594;</button>`;
    }
    body.innerHTML = pageEntries.length
        ? pageEntries.map(renderMissingDataRow).join("")
        : `<tr><td colspan="18" class="empty-cell">No records match the current cleansing filters.</td></tr>`;
}

function openMissingDataDrilldownForSeverity(severityKey) {
    missingDataFilters = { severity: severityKey || "all", fieldType: "all", slaStatus: "Missing Data", machineGroup: "all", search: "", cleared: "all" };
    const card = document.getElementById("missing-data-drilldown");
    if (card && card.tagName === "DETAILS") card.open = true;
    renderMissingDataDrilldown();
    if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ── Excel exports (SheetJS) ──────────────────────────────────────────────────
const MISSING_LINE_HEADERS = ["WO ID", "MR ID", "Equipment / Asset Name", "Asset ID", "Machine Group", "Severity / Priority", "Created Date", "Actual Start Date", "Actual End Date", "Actual Duration", "Missing Field Type", "SLA Status", "Open Age", "Completion Delay", "Description", "Cleansing Status", "Remark", "Follow-up Action"];
const ONE_MIN_MTTR_HEADERS = ["WO ID", "MR ID", "Equipment / Asset Name", "Asset ID", "Machine Group", "Severity", "Actual Start Date", "Actual End Date", "Duration", "Description", "Cleansing Status", "Remark", "Follow-up Action"];

function missingLineItemRow(entry) {
    const v = missingEntryView(entry);
    return [v.woId, v.mrId || "", v.equipment, v.assetId, v.machineGroup, [v.severityLabel, v.severityRaw].filter(Boolean).join(" / "), v.created, v.actualStart, v.actualEnd, v.duration, v.missingFieldType, v.slaStatus, v.openAge, v.completionDelay, v.description, v.cleansingStatus, v.remark, v.followUpAction];
}

function oneMinMttrRow(entry) {
    const v = missingEntryView(entry);
    return [v.woId, v.mrId || "", v.equipment, v.assetId, v.machineGroup, v.severityLabel, v.actualStart, v.actualEnd, v.duration, v.description, v.cleansingStatus, v.remark, v.followUpAction];
}

function buildSlaMissingSummary(model) {
    const header = ["Severity", "Total WO", "Valid SLA Records", "Missing Data Count", "Data Completeness %", "Met Target", "Late", "Open Overdue", "SLA % based on valid records only"];
    const rows = model.severityRows.filter((r) => r.total > 0).map((r) => [
        r.severity.label, r.total, r.validCount, r.missingData,
        r.total ? exportPercent((r.validCount / r.total) * 100) : "",
        r.metTarget, r.late, r.openOverdue,
        r.slaPct === null ? "N/A" : exportPercent(r.slaPct),
    ]);
    rows.push([
        "TOTAL", model.totalRows, model.validCount, model.missingData,
        model.totalRows ? exportPercent((model.validCount / model.totalRows) * 100) : "",
        model.metTarget, model.late, model.openOverdue,
        model.overallCompliance === null ? "N/A" : exportPercent(model.overallCompliance),
    ]);
    return { header, rows };
}

function isOpenFollowupStatus(status) {
    return status === "Open" || status === "Checking";
}

function setMissingDataExportStatus(message, tone = "") {
    const node = document.getElementById("missing-data-export-status");
    if (!node) return;
    node.textContent = message || "";
    node.className = `missing-data-export-status${tone ? ` ${tone}` : ""}`;
}

// Export exactly the records currently shown in the Missing Data Drill-down table
// (respecting all active filters: severity, field type, SLA status, machine group,
// cleared toggle, and search). Styled to match the Machine Explorer export.
function exportDataCleansingTracker() {
    if (typeof XLSX === "undefined") { setMissingDataExportStatus("SheetJS not loaded yet — please wait and retry.", "error"); return; }
    const model = lastWorkOrderSlaModel;
    if (!model) { setMissingDataExportStatus("No SLA data loaded yet — open the SLA Compliance section first.", "error"); return; }

    // Respect all active filters — export exactly what is visible in the table
    const entries = getFilteredMissingDataEntries(model);
    if (!entries.length) { setMissingDataExportStatus("No records match the current filters — nothing to export.", "error"); return; }

    // ── Style helpers (matching Machine Explorer export) ─────────────────────
    const solid = (rgb) => ({ patternType: "solid", fgColor: { rgb }, bgColor: { rgb } });
    const fnt = (rgb, bold = false) => ({ color: { rgb }, bold, sz: 9, name: "Calibri" });
    const aln = (h = "left", wrap = false) => ({ horizontal: h, vertical: "middle", wrapText: wrap });
    const mk = (fillRgb, textRgb, bold = false, h = "left") => ({
        font: fnt(textRgb, bold),
        fill: solid(fillRgb),
        alignment: aln(h),
    });
    const HEADER = {
        font: { color: { rgb: "FFFFFF" }, bold: true, sz: 9, name: "Calibri" },
        fill: solid("0F766E"),
        alignment: aln("center", true),
        border: { bottom: { style: "medium", color: { rgb: "0D5C56" } } },
    };
    const STRIPE = [mk("FFFFFF", "1E293B"), mk("F0FDF8", "1E293B")];
    const cs = (ws, r, c, style) => { const addr = XLSX.utils.encode_cell({ r, c }); if (ws[addr]) ws[addr].s = style; };

    // Missing-field-type badge colour
    const fieldTypeStyle = (ft) => {
        if (ft === "Missing Actual Start" || ft === "Missing Actual End") return mk("FEE2E2", "991B1B", true);
        if (ft === "Invalid Date") return mk("FFEDD5", "9A3412", true);
        if (ft === "Missing Asset") return mk("EDE9FE", "4C1D95", true);
        return mk("FEF3C7", "92400E");
    };
    const slaStatusStyle = (s) => {
        if (s === "Open Overdue") return mk("FEE2E2", "991B1B", true, "center");
        if (s === "Late")         return mk("FFEDD5", "9A3412", true, "center");
        if (s === "Missing Data" || s === "Missing Start Date" || s === "Missing End Date")
            return mk("F1F5F9", "64748B", false, "center");
        return mk("D1FAE5", "065F46", false, "center");
    };
    const cleansingStyle = (s) => {
        if (s === "Open")     return mk("FEE2E2", "991B1B", false);
        if (s === "Checking") return mk("FEF3C7", "92400E", false);
        if (CLEANSING_CLEARED_STATUSES.has(s)) return mk("D1FAE5", "065F46", true);
        return mk("F1F5F9", "475569");
    };
    const sevStyle = (sev) => {
        const s = String(sev || "").trim();
        if (s.startsWith("S1")) return mk("FEE2E2", "991B1B", true, "center");
        if (s.startsWith("S2")) return mk("FFEDD5", "9A3412", true, "center");
        if (s.startsWith("S3")) return mk("FEF9C3", "713F12", false, "center");
        if (s.startsWith("S4")) return mk("DBEAFE", "1E40AF", false, "center");
        return mk("F1F5F9", "64748B", false, "center");
    };

    // ── Build the single sheet ───────────────────────────────────────────────
    // Columns ordered: identity → SLA context → missing info → narrative → review
    const HEADERS = [
        "WO ID", "MR ID",                                             // 0-1  identity
        "Equipment / Asset Name", "Asset ID", "Machine Group",        // 2-4  asset
        "Severity / Priority",                                         // 5    priority
        "Created Date", "Actual Start", "Actual End",                 // 6-8  timeline
        "Actual Duration", "Open Age",                                 // 9-10 time in flight
        "Missing Field Type", "SLA Status",                           // 11-12 issue
        "Completion Delay",                                            // 13
        "Description", "Translated Description",                      // 14-15 narrative
        "Remark", "Follow-up Action",                                  // 16-17 review layer
    ];
    const colWidths = [18, 15, 34, 16, 24, 14, 20, 20, 20, 14, 12, 22, 16, 14, 52, 52, 30, 30];

    const data = [HEADERS];
    entries.forEach((entry) => {
        const v = missingEntryView(entry);
        data.push([
            v.woId, v.mrId || "",
            v.equipment, v.assetId, v.machineGroup,
            v.severityLabel,
            v.created, v.actualStart, v.actualEnd,
            v.duration, v.openAge,
            v.missingFieldType, v.slaStatus,
            v.completionDelay,
            v.description, v.translatedDescription,
            v.remark, v.followUpAction,
        ]);
    });

    const ws = XLSX.utils.aoa_to_sheet(data);
    ws["!cols"]  = colWidths.map((w) => ({ wch: w }));
    ws["!rows"]  = [{ hpt: 34 }];
    ws["!autofilter"] = { ref: XLSX.utils.encode_range({ s: { r: 0, c: 0 }, e: { r: data.length - 1, c: HEADERS.length - 1 } }) };

    // Style header row
    HEADERS.forEach((_, c) => cs(ws, 0, c, HEADER));

    // Style data rows
    // Col indices after removing Cleansing Status and adding Translated Description:
    // 5=Severity  11=Missing Field Type  12=SLA Status  (Cleansing Status removed)
    entries.forEach((entry, i) => {
        const r = i + 1;
        const v = missingEntryView(entry);
        HEADERS.forEach((_, c) => cs(ws, r, c, STRIPE[i % 2]));   // base stripe
        cs(ws, r, 5,  sevStyle(v.severityLabel));
        cs(ws, r, 11, fieldTypeStyle(v.missingFieldType));
        cs(ws, r, 12, slaStatusStyle(v.slaStatus));
    });

    // ── File name encodes active filters so it's self-documenting ────────────
    const f = missingDataFilters;
    const sevPart   = f.severity !== "all"   ? `_${f.severity}` : "";
    const typePart  = f.fieldType !== "all"  ? `_${f.fieldType.replace(/\s+/g, "-")}` : "";
    const grpPart   = f.machineGroup !== "all" ? `_${f.machineGroup.replace(/[^a-zA-Z0-9]/g, "-")}` : "";
    const clearPart = f.cleared !== "all"    ? `_${f.cleared}` : "";
    const datePart  = new Date().toISOString().slice(0, 10).replace(/-/g, "");
    const fileName  = `Missing_Data_Review${sevPart}${typePart}${grpPart}${clearPart}_${datePart}.xlsx`;

    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, "Missing Data Review");
    XLSX.writeFile(wb, fileName);

    setMissingDataExportStatus(`Exported ${fmtNumber(entries.length)} records → ${fileName}`, "success");
}

function renderWorkOrderResponseSection(rows = []) {
    const sourceRows = getWorkOrderSlaSourceRows(rows);
    populateWorkOrderSlaFilters(sourceRows);
    const scopedRows = getScopedWorkOrderSlaRows(sourceRows);
    const model = buildWorkOrderSlaModel(scopedRows, getWorkOrderSlaReferenceDate());
    lastWorkOrderSlaModel = model;
    const scope = getWorkOrderSlaScopeFilters();
    const usingAllYearSource = hasWorkOrderSlaFields(allWorkOrderRowsCache) && sourceRows === allWorkOrderRowsCache;
    const subtitleParts = [usingAllYearSource ? "Using all-year work-order source" : `Using ${getPeriodLabel(downtimePayload?.meta || {})} work-order rows`];
    subtitleParts.push(`Year: ${scope.year === "all" ? "All Years" : scope.year}`);
    subtitleParts.push(`Month: ${scope.month === "all" ? "All Months" : formatWorkOrderSlaMonthLabel(scope.month)}`);
    if (scope.equipment !== "all") subtitleParts.push(`Equipment: ${scope.equipment}`);
    if (scope.criticality) subtitleParts.push(`Criticality: ${scope.criticality}`);
    subtitleParts.push(`Reference date: ${fmtDateOnly(model.referenceDate)}`);
    setText("wo-response-subtitle", `${subtitleParts.join(" | ")}.`);
    setText("wo-sla-target-source", getSlaTargetSourceText());
    setText("wo-sla-overall", model.overallCompliance === null ? "N/A" : fmtPercent(model.overallCompliance));
    setText("wo-sla-overall-sub", `${fmtNumber(model.metTarget)} of ${fmtNumber(model.validCount)} valid work orders are currently within target.`);
    setText("wo-sla-met-count", fmtNumber(model.metTarget));
    setText("wo-sla-valid-count", fmtNumber(model.validCount));
    setText("wo-sla-open-overdue", fmtNumber(model.openOverdue));
    setText("wo-sla-open-overdue-sub", `${fmtNumber(model.late)} completed work orders also missed target.`);
    setHtml("wo-sla-open-overdue-breakdown", renderWorkOrderSlaPills(
        model.severityRows.map((row) => ({ label: row.severity.shortLabel, count: row.openOverdue }))
    ));
    setText("wo-sla-worst-severity", model.worstSeverity ? model.worstSeverity.severity.label : "N/A");
    setText("wo-sla-worst-sub", model.worstSeverity ? `${fmtNumber(model.worstSeverity.validCount)} valid work orders in scope.` : "No valid severity bucket available.");
    setText("wo-sla-worst-rate", model.worstSeverity && model.worstSeverity.slaPct !== null ? fmtPercent(model.worstSeverity.slaPct) : "N/A");
    setText(
        "wo-sla-worst-late",
        model.worstSeverity ? `${fmtNumber(model.worstSeverity.late + model.worstSeverity.openOverdue)} impacted WO` : "N/A"
    );
    setText("wo-sla-review-count", fmtNumber(model.missingData));
    setText("wo-sla-review-sub", `${fmtNumber(model.totalRows)} total work orders in current SLA scope.`);
    setText(
        "wo-sla-unclassified-count",
        fmtNumber(model.severityRows.find((row) => row.severity.key === WORK_ORDER_SLA_UNCLASSIFIED.key)?.total || 0)
    );
    setText(
        "wo-sla-missing-dates",
        fmtNumber(model.entries.filter((entry) => entry.slaStatus === "Missing Start Date" || entry.slaStatus === "Missing End Date").length)
    );
    renderWorkOrderSlaSummaryTable(model);
    renderWorkOrderSlaAverageTable("wo-sla-response-body", model.severityRows, "response");
    renderWorkOrderSlaAverageTable("wo-sla-completion-body", model.severityRows, "completion");
    renderWorkOrderSlaDrilldownTable(model);
    renderMissingDataDrilldown();
    renderOpenSeveritySection(scopedRows);
    // The Preventive/Corrective section has its OWN year/month filter and the
    // analysis chart its own financial-year selector, so feed it the full all-year
    // source rows (NOT the SLA-scoped rows) — otherwise the SLA year filter hides
    // every financial year except the SLA-selected one (past-year data vanished).
    renderPreventiveCorrectiveSection(sourceRows);
    syncTopicMirrors();
}

function renderDataReliabilityActionList(entries = []) {
    const body = document.getElementById("data-quality-action-body");
    if (!body) return;
    dataReviewActionSnapshots = {};
    const actionRows = (entries || []).filter((entry) => {
        const qualityFlags = getDataQualityFlags(entry.row).filter((flag) => flag !== "Valid");
        return qualityFlags.length || isWorkOrderSlaMissingStatus(entry.slaStatus);
    }).slice(0, 250);
    if (!actionRows.length) {
        body.innerHTML = `<tr><td colspan="9" class="empty-cell">No data-quality action records in the current scope. Confirmed rows now count toward KPIs.</td></tr>`;
        return;
    }
    body.innerHTML = actionRows.map((entry, index) => {
        const row = entry.row;
        const qualityFlags = getDataQualityFlags(row).filter((flag) => flag !== "Valid");
        const slaIssue = isWorkOrderSlaMissingStatus(entry.slaStatus)
            ? [entry.slaStatus, ...(entry.delayLines || []).filter((line) => line !== "Within target")].join(" | ")
            : "--";
        const originalCreated = entry.created?.date || getMrRaisedDate(row).date;
        const translatedDescription = cleanMrValue(row?.translated_description);
        const mrId = getMrRequestId(row, index) || entry.id || "--";
        const assetName = getMachineEquipmentName(row) || "--";
        const assetId = row?.asset_id || row?.machine_code || "--";
        const key = getDataReviewRowKey(row);
        const ov = getCorrectionOverride(key);
        dataReviewActionSnapshots[key] = {
            mrId, assetName, assetId,
            createdISO: originalCreated instanceof Date && !Number.isNaN(originalCreated.getTime()) ? originalCreated.toISOString().slice(0, 10) : "",
            flags: qualityFlags,
        };
        const correctedCreated = ov?.corrections?.createdDate ? new Date(ov.corrections.createdDate) : null;
        const displayCreated = (correctedCreated && !Number.isNaN(correctedCreated.getTime())) ? correctedCreated : originalCreated;
        const editedBadge = ov && ov.status === "edited" ? ` <span class="dr-pending-chip">edited · not confirmed</span>` : "";
        return `
            <tr>
                <td>
                    <div class="cell-title">${escapeHtml(mrId)}${editedBadge}</div>
                    <div class="cell-sub">${escapeHtml(getMrWorkOrderOnlyId(row) || "--")}</div>
                </td>
                <td>
                    <div class="cell-title">${escapeHtml(ov?.corrections?.assetName || assetName)}</div>
                    <div class="cell-sub">${escapeHtml(ov?.corrections?.assetId || assetId)}</div>
                </td>
                <td>${renderBadgeCell("status", getMrStatus(row))}</td>
                <td>${escapeHtml(qualityFlags.length ? qualityFlags.join("; ") : "Valid")}</td>
                <td>${escapeHtml(slaIssue)}</td>
                <td>${escapeHtml(displayCreated ? fmtDateOnly(displayCreated) : "--")}</td>
                <td class="description-cell">${escapeHtml(getMrDescription(row) || "--")}</td>
                <td class="description-cell">${escapeHtml(translatedDescription || "--")}</td>
                <td class="dr-action-cell">
                    <button type="button" class="dr-btn dr-btn-edit" data-dr-action="edit-correction" data-dr-key="${escapeHtml(key)}">Edit</button>
                    <button type="button" class="dr-btn dr-btn-confirm" data-dr-action="confirm-correction" data-dr-key="${escapeHtml(key)}">Confirm</button>
                </td>
            </tr>
        `;
    }).join("");
}

function buildDataReliabilityHistoryRows(rows = []) {
    const normalized = normalizeMrTrackingRows(rows);
    const buckets = new Map();
    let excludedRaisedDateCount = 0;

    normalized.items.forEach((item) => {
        const raisedDate = item.raised?.date;
        if (!(raisedDate instanceof Date) || Number.isNaN(raisedDate.getTime())) {
            excludedRaisedDateCount += 1;
            return;
        }

        const year = String(raisedDate.getFullYear());
        const bucket = buckets.get(year) || {
            year,
            mrRaised: 0,
            woIds: new Set(),
            open: 0,
            newCount: 0,
            inProgress: 0,
            closed: 0,
            review: 0,
        };

        bucket.mrRaised += 1;

        const workOrderId = getMrWorkOrderOnlyId(item.row);
        if (workOrderId) bucket.woIds.add(workOrderId);

        if (isNormalOpenMrStatus(item.status)) bucket.open += 1;
        if (isMrNewStatus(item.status)) bucket.newCount += 1;
        if (isMrInProgressStatus(item.status)) bucket.inProgress += 1;
        if (isMrFinishedStatus(item.status)) bucket.closed += 1;
        if (isMrReviewStatus(item.status) || (!isNormalOpenMrStatus(item.status) && !isMrFinishedStatus(item.status))) {
            bucket.review += 1;
        }

        buckets.set(year, bucket);
    });

    return {
        rows: [...buckets.values()]
            .sort((a, b) => Number(b.year) - Number(a.year))
            .map((bucket) => ({
                ...bucket,
                woLogged: bucket.woIds.size,
            })),
        excludedRaisedDateCount,
        duplicateCount: normalized.duplicateCount,
    };
}

function renderDataReliabilityHistoryTable(rows = []) {
    const body = document.getElementById("data-reliability-history-body");
    const note = document.getElementById("data-reliability-history-note");
    if (!body || !note) return;

    if (!rows.length) {
        note.textContent = "Counts use created / raised year and the current lifecycle status of those records.";
        body.innerHTML = `<tr><td colspan="8" class="empty-cell">No historical MR / WO totals in the current scope.</td></tr>`;
        return;
    }

    const history = buildDataReliabilityHistoryRows(rows);
    const noteParts = [];
    if (history.excludedRaisedDateCount) {
        noteParts.push(`${fmtNumber(history.excludedRaisedDateCount)} row${history.excludedRaisedDateCount === 1 ? "" : "s"} without a valid raised date are excluded.`);
    }
    if (history.duplicateCount) {
        noteParts.push(`${fmtNumber(history.duplicateCount)} duplicate MR ID/key row${history.duplicateCount === 1 ? "" : "s"} were skipped.`);
    }
    note.textContent = `Counts use created / raised calendar year within the current Data Reliability scope. Still Open, New, In Progress, and Closed reflect the current lifecycle status of those same records.${noteParts.length ? ` ${noteParts.join(" ")}` : ""}`;

    if (!history.rows.length) {
        body.innerHTML = `<tr><td colspan="8" class="empty-cell">No records with a valid raised year are available in the current scope.</td></tr>`;
        return;
    }

    body.innerHTML = history.rows.map((row) => `
        <tr>
            <td>${escapeHtml(row.year)}</td>
            <td>${escapeHtml(fmtNumber(row.mrRaised))}</td>
            <td>${escapeHtml(fmtNumber(row.woLogged))}</td>
            <td>${escapeHtml(fmtNumber(row.open))}</td>
            <td>${escapeHtml(fmtNumber(row.newCount))}</td>
            <td>${escapeHtml(fmtNumber(row.inProgress))}</td>
            <td>${escapeHtml(fmtNumber(row.closed))}</td>
            <td>${escapeHtml(fmtNumber(row.review))}</td>
        </tr>
    `).join("");
}

function setSelectOptions(id, values, label) {
    const select = document.getElementById(id);
    if (!select) return;
    const current = select.value;
    const unique = [...new Set(values.filter((value) => String(value || "").trim()).map(String))].sort((a, b) => a.localeCompare(b));
    select.innerHTML = `<option value="">${escapeHtml(label)}</option>` + unique.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
    if (unique.includes(current)) select.value = current;
}

function formatDateInputValue(date) {
    if (!(date instanceof Date) || Number.isNaN(date.getTime())) return "";
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
}

function getMachineEquipmentName(row) {
    return String(
        row?.asset_display_name
        || row?.machine_name_display
        || row?.machine_equipment_name
        || row?.raw_machine_name
        || row?.machine_name
        || row?.machine_group
        || "--"
    ).trim();
}

function getMachineAssetId(row) {
    return String(row?.asset_id || row?.machine_code || "").trim();
}

function getMrDescription(row) {
    return String(row?.description_original || row?.description || row?.remarks || "").trim();
}

function getMachineExplorerFilterValue(id) {
    return document.getElementById(id)?.value || "";
}

function getMachineExplorerFilterState() {
    const groupValue = normalizeMachineExplorerGroupLabel(getMachineExplorerFilterValue("machine-explorer-group") || machineExplorerSelectedGroup || MACHINE_EXPLORER_ALL_GROUP);
    return {
        group: groupValue === "" ? MACHINE_EXPLORER_ALL_GROUP : groupValue,
        asset: getMachineExplorerFilterValue("machine-explorer-asset"),
        machine: getMachineExplorerFilterValue("machine-explorer-machine"),
        criticality: getMachineExplorerFilterValue("machine-explorer-criticality"),
        status: getMachineExplorerFilterValue("machine-explorer-status"),
        serviceLevel: getMachineExplorerFilterValue("machine-explorer-service-level"),
        year: getMachineExplorerFilterValue("machine-explorer-year"),
        month: getMachineExplorerFilterValue("machine-explorer-month"),
        createdBy: getMachineExplorerFilterValue("machine-explorer-created-by"),
        startedBy: getMachineExplorerFilterValue("machine-explorer-started-by"),
        quality: getMachineExplorerFilterValue("machine-explorer-quality"),
        assetCriticality: getMachineExplorerFilterValue("machine-explorer-asset-criticality") || machineExplorerAssetCriticalityFilter,
        acknowledgement: getMachineExplorerFilterValue("machine-explorer-ack-filter") || machineExplorerAckFilter,
    };
}

function getMachineExplorerOptions(rows = []) {
    const byAsset = new Map();
    rows.forEach((row) => {
        const assetId = getMachineAssetId(row);
        if (!assetId) return;
        const name = getMachineEquipmentName(row);
        const existing = byAsset.get(assetId) || {
            assetId,
            name,
            count: 0,
            openCount: 0,
        };
        existing.count += 1;
        existing.openCount += isNormalOpenMrStatus(getMrStatus(row)) ? 1 : 0;
        if ((!existing.name || existing.name === "--") && name) existing.name = name;
        byAsset.set(assetId, existing);
    });
    return [...byAsset.values()].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name) || a.assetId.localeCompare(b.assetId));
}

function populateMachineExplorerFilters(rows = []) {
    const groupSelect = document.getElementById("machine-explorer-group");
    if (groupSelect) {
        const requested = normalizeMachineExplorerGroupLabel(machineExplorerSelectedGroup || groupSelect.value || MACHINE_EXPLORER_ALL_GROUP);
        groupSelect.innerHTML = MACHINE_EXPLORER_GROUPS.map((group) => (
            `<option value="${escapeHtml(group)}">${escapeHtml(group)}</option>`
        )).join("");
        machineExplorerSelectedGroup = MACHINE_EXPLORER_GROUPS.includes(requested) ? requested : MACHINE_EXPLORER_ALL_GROUP;
        groupSelect.value = machineExplorerSelectedGroup;
    }

    const years = new Set();
    const months = new Set();
    rows.forEach((row) => {
        const raised = getMrRaisedDate(row).date;
        if (!raised) return;
        years.add(String(raised.getFullYear()));
        months.add(String(raised.getMonth() + 1).padStart(2, "0"));
    });
    setSelectOptions("machine-explorer-criticality", rows.map((row) => row.criticality || row.normalized_criticality), "All Criticalities");
    setSelectOptions("machine-explorer-status", rows.map(getMrStatus), "All Statuses");
    setSelectOptions("machine-explorer-service-level", rows.map(getMrServiceLevel), "All Service Levels");
    setSelectOptions("machine-explorer-year", [...years].sort((a, b) => b.localeCompare(a)), "All Years");
    setSelectOptions("machine-explorer-month", [...months].sort(), "All Months");
    setSelectOptions("machine-history-year", [...years].sort((a, b) => b.localeCompare(a)), "All Years");
    setSelectOptions("machine-history-month", [...months].sort(), "All Months");
    setSelectOptions("machine-explorer-created-by", rows.map(getMrCreatedBy).filter((value) => value !== "--"), "All Creators");
    setSelectOptions("machine-explorer-started-by", rows.map(getMrStartedBy).filter((value) => value !== "--"), "All Starters");
    setSelectOptions("machine-explorer-quality", rows.flatMap(getDataQualityFlags), "All Quality Flags");

    const selectorRows = filterMachineExplorerRows(rows, { includeAssetFilter: false });
    const machineOptions = getMachineExplorerOptions(selectorRows);
    const machineSelect = document.getElementById("machine-explorer-machine");
    if (machineSelect) {
        const previous = machineSelect.value;
        machineSelect.innerHTML = `<option value="">All Machines</option>` + machineOptions.map((item) => (
            `<option value="${escapeHtml(item.assetId)}">${escapeHtml(item.name)} | ${escapeHtml(item.assetId)}</option>`
        )).join("");
        machineSelect.value = machineOptions.some((item) => item.assetId === previous) ? previous : "";
    }

    const assetSelect = document.getElementById("machine-explorer-asset");
    if (assetSelect) {
        const previous = assetSelect.value;
        assetSelect.innerHTML = `<option value="">All Assets</option>` + machineOptions.map((item) => (
            `<option value="${escapeHtml(item.assetId)}">${escapeHtml(item.assetId)}</option>`
        )).join("");
        assetSelect.value = machineOptions.some((item) => item.assetId === previous) ? previous : "";
    }

    const raisedDates = rows
        .map((row) => getMrRaisedDate(row).date)
        .filter((date) => date instanceof Date && !Number.isNaN(date.getTime()))
        .sort((a, b) => a.getTime() - b.getTime());
    const minRaised = raisedDates.length ? formatDateInputValue(raisedDates[0]) : "";
    const maxRaised = raisedDates.length ? formatDateInputValue(raisedDates[raisedDates.length - 1]) : "";
    ["machine-history-date-from", "machine-history-date-to"].forEach((id) => {
        const input = document.getElementById(id);
        if (!input) return;
        input.min = minRaised;
        input.max = maxRaised;
    });
    syncMachineHistoryPeriodInputs();
}

function rowIsCritical(row) {
    return String(row?.criticality || row?.normalized_criticality || "") === "Critical" || isProductionCritical(row);
}

function rowMatchesMachineExplorerGroup(row, group) {
    // Group drill-down uses operational Machine Groups; criticality remains a row tag/filter.
    // Refrigeration is resolved inside getPerformanceMachineGroup() so it keeps priority over other group matches.
    const selected = normalizeMachineExplorerGroupLabel(group || MACHINE_EXPLORER_ALL_GROUP);
    if (selected === MACHINE_EXPLORER_ALL_GROUP) return true;
    const rowGroup = normalizeMachineExplorerGroupLabel(getPerformanceMachineGroup(row));
    return rowGroup === selected;
}

function isUnknownMachineExplorerGroup(row) {
    const rawGroup = String(row?.machine_group || row?.normalized_machine_group || "").trim();
    const normalizedRaw = normalizeClassification(rawGroup);
    return getPerformanceMachineGroup(row) === "Unknown / Review"
        || normalizedRaw.includes("unknown")
        || normalizedRaw.includes("unmapped")
        || normalizedRaw.includes("review");
}

function rowMatchesMachineExplorerCriticality(row, value) {
    if (!value) return true;
    if (value === "Critical") return rowIsCritical(row);
    if (value === "Non-Critical") return !rowIsCritical(row);
    return true;
}

function rowMatchesMachineExplorerAcknowledgement(row, value) {
    if (!value) return true;
    const ackStatus = normalizeClassification(getAcknowledgementStatus(row));
    if (value === "Not Acknowledged") return ackStatus === "not acknowledged";
    if (value === "Acknowledged") return ackStatus === "acknowledged in progress" || ackStatus === "closed" || ackStatus === "acknowledged";
    return true;
}

function filterMachineExplorerRows(rows = [], options = {}) {
    const {
        includeGroupFilter = true,
        includeAssetFilter = true,
        selectedAssetId = "",
    } = options;
    const state = getMachineExplorerFilterState();
    const assetFilter = selectedAssetId || (includeAssetFilter ? (state.asset || state.machine) : "");
    return rows.filter((row) => {
        const rowAssetId = getMachineAssetId(row);
        const raised = getMrRaisedDate(row).date;
        if (state.year && (!raised || String(raised.getFullYear()) !== state.year)) return false;
        if (state.month && (!raised || String(raised.getMonth() + 1).padStart(2, "0") !== state.month)) return false;
        if (includeGroupFilter && !rowMatchesMachineExplorerGroup(row, state.group)) return false;
        if (assetFilter && rowAssetId !== assetFilter) return false;
        if (state.status && getMrStatus(row) !== state.status) return false;
        if (state.criticality && String(row.criticality || row.normalized_criticality || "") !== state.criticality) return false;
        if (!rowMatchesMachineExplorerCriticality(row, state.assetCriticality)) return false;
        if (!rowMatchesMachineExplorerAcknowledgement(row, state.acknowledgement)) return false;
        if (state.serviceLevel && getMrServiceLevel(row) !== state.serviceLevel) return false;
        if (state.startedBy && getMrStartedBy(row) !== state.startedBy) return false;
        if (state.createdBy && getMrCreatedBy(row) !== state.createdBy) return false;
        if (state.quality && !getDataQualityFlags(row).includes(state.quality)) return false;
        return true;
    });
}

function getLatestMrDate(row) {
    return [
        getMrRaisedDate(row).date,
        getMrFinishedDate(row).date,
        parseDateValue(row?.latest_event_time),
        parseDateValue(row?.actual_end_time || row?.maintenance_end_time),
        parseDateValue(row?.actual_start_time || row?.maintenance_start_time),
    ].filter(Boolean).sort((a, b) => b - a)[0] || null;
}

function summarizeMachineExplorerRows(rows = []) {
    const openRows = rows.filter((row) => isNormalOpenMrStatus(getMrStatus(row)));
    const notAckRows = rows.filter((row) => isMrNewStatus(getMrStatus(row)) && !getMrWorkOrderOnlyId(row));
    const inProgressRows = rows.filter((row) => isMrInProgressStatus(getMrStatus(row)));
    const finishedRows = rows.filter((row) => isMrFinishedStatus(getMrStatus(row)));
    const validTtr = finishedRows.map(getTtrHours).filter((value) => value !== null);
    const oldestOpenAge = Math.max(
        -1,
        ...openRows.map((row) => getAgeDaysFrom(getMrRaisedDate(row).date)).filter((days) => days !== null)
    );
    const latestDate = rows.map(getLatestMrDate).filter(Boolean).sort((a, b) => b - a)[0] || null;
    const assetIds = new Set(rows.map(getMachineAssetId).filter(Boolean));
    const invalidRows = rows.filter((row) => !isDataQualityValid(row));
    return {
        total: rows.length,
        assetCount: assetIds.size,
        open: openRows.length,
        notAcknowledged: notAckRows.length,
        inProgress: inProgressRows.length,
        finished: finishedRows.length,
        closureRate: rows.length ? (finishedRows.length / rows.length) * 100 : null,
        averageTtr: validTtr.length ? validTtr.reduce((sum, value) => sum + value, 0) / validTtr.length : null,
        oldestOpenAge: oldestOpenAge >= 0 ? oldestOpenAge : null,
        latestDate,
        invalid: invalidRows.length,
    };
}

function renderBadgeCell(kind, value) {
    const text = String(value || "--");
    const normalized = normalizeClassification(text);
    if (kind === "status") {
        const level = normalized === "finished" ? "ok" : (normalized === "new" ? "requires_attention" : (normalized === "in progress" || normalized === "inprogress" ? "warning" : "offline"));
        return buildStatusPill(level, text);
    }
    if (kind === "criticality") {
        return buildStatusPill(text === "Critical" ? "critical" : "offline", text);
    }
    if (kind === "ack") {
        const level = normalized === "not acknowledged" ? "requires_attention" : (normalized === "review" ? "warning" : "ok");
        return buildStatusPill(level, text);
    }
    if (kind === "quality") {
        const level = text === "Valid" ? "stable" : (normalizeClassification(text).includes("review") ? "warning" : "critical");
        return buildStatusPill(level, text);
    }
    if (kind === "refrig-type") {
        return buildStatusPill(text === "Condenser" ? "warning" : "ok", text);
    }
    if (kind === "wo-type") {
        if (isPmJobTrade(value)) return buildStatusPill("ok", "PM");
        if (value && value !== "--") return buildStatusPill("warning", "CM");
        return buildStatusPill("offline", "—");
    }
    return `<span class="priority-badge p${escapeHtml(text.replace(/[^A-Za-z0-9_-]/g, ""))}">${escapeHtml(text)}</span>`;
}

function resetMachineExplorerKpis() {
    [
        "machine-kpi-total",
        "machine-kpi-open",
        "machine-kpi-not-ack",
        "machine-kpi-progress",
        "machine-kpi-finished",
        "machine-kpi-closure",
        "machine-kpi-mttr",
        "machine-kpi-oldest",
        "machine-kpi-invalid",
    ].forEach((id) => setMachineExplorerKpiValue(id, "--"));
}

function setMachineExplorerKpiValue(id, value) {
    const text = value == null || value === "" ? "--" : value;
    setText(id, text);
    const card = document.getElementById(id)?.closest(".kpi-card");
    if (card) card.classList.toggle("is-empty", text === "--");
}

function setMachineHistoryContext(text, isEmpty = false) {
    const node = document.getElementById("machine-history-context");
    if (!node) return;
    node.textContent = text || "";
    node.classList.toggle("empty", Boolean(isEmpty));
}

function renderMachineExplorerKpis(filteredRows = []) {
    const summary = summarizeMachineExplorerRows(filteredRows);
    setMachineExplorerKpiValue("machine-kpi-total", fmtNumber(summary.total));
    setMachineExplorerKpiValue("machine-kpi-open", fmtNumber(summary.open));
    setMachineExplorerKpiValue("machine-kpi-not-ack", fmtNumber(summary.notAcknowledged));
    setMachineExplorerKpiValue("machine-kpi-progress", fmtNumber(summary.inProgress));
    setMachineExplorerKpiValue("machine-kpi-finished", fmtNumber(summary.finished));
    setMachineExplorerKpiValue("machine-kpi-closure", summary.closureRate !== null ? fmtPercent(summary.closureRate) : "--");
    setMachineExplorerKpiValue("machine-kpi-mttr", summary.averageTtr !== null ? fmtHours(summary.averageTtr) : "--");
    setMachineExplorerKpiValue("machine-kpi-oldest", summary.oldestOpenAge !== null ? formatDays(summary.oldestOpenAge) : "--");
    setMachineExplorerKpiValue("machine-kpi-invalid", fmtNumber(summary.invalid));
}

function buildMachineExplorerGroupRows(rows = []) {
    return MACHINE_EXPLORER_GROUPS.map((group) => {
        const groupRows = group === MACHINE_EXPLORER_ALL_GROUP
            ? rows
            : rows.filter((row) => rowMatchesMachineExplorerGroup(row, group));
        return { group, rows: groupRows, ...summarizeMachineExplorerRows(groupRows) };
    });
}

function renderMachineExplorerGroupCards(rows = []) {
    const wrap = document.getElementById("machine-group-cards");
    if (!wrap) return;
    const cards = buildMachineExplorerGroupRows(rows);
    const unknownRows = rows.filter(isUnknownMachineExplorerGroup);
    const dataQualityPrefix = unknownRows.length
        ? `${fmtNumber(unknownRows.length)} WO/MR records have unknown or unmapped machine group - review asset mapping. `
        : "";
    wrap.innerHTML = cards.map((card) => `
        <button type="button" class="machine-group-card ${card.group === machineExplorerSelectedGroup ? "active" : ""}" data-machine-group="${escapeHtml(card.group)}">
            <span class="machine-group-card-title">${escapeHtml(card.group)}</span>
            <span class="machine-group-card-sub">${fmtNumber(card.assetCount)} machine${card.assetCount === 1 ? "" : "s"} / ${fmtNumber(card.total)} WO/MR</span>
            <span class="machine-group-card-metrics">
                <span><strong>${fmtNumber(card.open)}</strong> open</span>
                <span><strong>${fmtNumber(card.finished)}</strong> finished</span>
                <span><strong>${fmtNumber(card.notAcknowledged)}</strong> not ack</span>
                <span><strong>${card.averageTtr !== null ? fmtHours(card.averageTtr) : "--"}</strong> avg TTR</span>
                <span><strong>${card.oldestOpenAge !== null ? formatDays(card.oldestOpenAge) : "--"}</strong> oldest</span>
                <span><strong>${fmtNumber(card.invalid)}</strong> invalid</span>
            </span>
        </button>
    `).join("") + `
        <p class="machine-group-data-quality-note">
            ${escapeHtml(dataQualityPrefix)}Unmapped/unknown assets are included in All Groups and flagged under Data Quality for review.
        </p>
    `;
    wrap.querySelectorAll("[data-machine-group]").forEach((button) => {
        button.addEventListener("click", () => {
            machineExplorerSelectedGroup = normalizeMachineExplorerGroupLabel(button.dataset.machineGroup || MACHINE_EXPLORER_ALL_GROUP);
            machineExplorerSelectedAssetId = "";
            machineExplorerRefrigSubgroup = "";
            machineExplorerRefrigCondenserGroupId = "";
            machineExplorerRefrigExpandedCondensers = new Set();
            const groupSelect = document.getElementById("machine-explorer-group");
            if (groupSelect) groupSelect.value = machineExplorerSelectedGroup;
            renderMachineExplorer(getCategoryScopedAllRows());
        });
    });
}

// ---- Refrigeration System hierarchy helpers ----
// The Machine Explorer uses a 4-level drill-down for Refrigeration:
// 1. Group (Refrigeration) → 2. Sub Machine Group → 3. Asset/Condenser → 4. WO/MR History
// "Condenser / Evaporator" subgroup uses a compact expandable tree instead of the standard asset table.

function getRefrigCondenserEntry(assetId) {
    if (!assetId) return null;
    return REFRIGERATION_NETWORK.find(
        (entry) => entry.condenserId === assetId || entry.evaporators.some((ev) => ev.id === assetId)
    ) || null;
}

function getRefrigAssetType(assetId) {
    if (!assetId) return null;
    if (REFRIGERATION_NETWORK.some((e) => e.condenserId === assetId)) return "Condenser";
    if (REFRIGERATION_NETWORK.some((e) => e.evaporators.some((ev) => ev.id === assetId))) return "Evaporator";
    return null;
}

function getRefrigAssetDisplayName(assetId) {
    for (const entry of REFRIGERATION_NETWORK) {
        if (entry.condenserId === assetId) return entry.condenserName;
        const ev = entry.evaporators.find((e) => e.id === assetId);
        if (ev) return ev.name;
    }
    return assetId;
}

// Returns true when the given asset ID belongs to the requested refrigeration subgroup.
function assetMatchesRefrigSubgroup(assetId, subgroupKey) {
    if (!subgroupKey || subgroupKey === "all") return true;
    if (!assetId) return false;
    const inCdu = REFRIG_CDU_ALL_IDS.has(assetId);
    if (subgroupKey === "condenser-evaporator") return inCdu;
    if (subgroupKey === "other") return !inCdu;
    const entry = getRefrigCondenserEntry(assetId);
    if (!entry) return false;
    return REFRIG_CDU_SUBGROUP.get(entry.condenserId) === subgroupKey;
}

function filterRowsByRefrigSubgroup(rows, subgroupKey) {
    if (!subgroupKey || subgroupKey === "all") return rows;
    return rows.filter((row) => assetMatchesRefrigSubgroup(getMachineAssetId(row), subgroupKey));
}

// Builds a per-condenser summary (total/open counts) for the compact tree.
function buildRefrigCondenserSummaryRows(rows) {
    return REFRIGERATION_NETWORK.map((entry) => {
        const allIds = new Set([entry.condenserId, ...entry.evaporators.map((ev) => ev.id)]);
        const networkRows = rows.filter((row) => allIds.has(getMachineAssetId(row)));
        const open = networkRows.filter((row) => isNormalOpenMrStatus(getMrStatus(row))).length;
        return { ...entry, allIds, rows: networkRows, total: networkRows.length, open, evaporatorCount: entry.evaporators.length };
    });
}

// ---- Step 2: Refrigeration sub machine group buttons ----
function renderRefrigSubgroupSection(allRows) {
    const section = document.getElementById("refrig-subgroup-section");
    if (!section) return;
    const isRefrig = machineExplorerSelectedGroup === "Refrigeration";
    section.style.display = isRefrig ? "" : "none";
    if (!isRefrig) return;
    const buttons = document.getElementById("refrig-subgroup-buttons");
    if (!buttons) return;
    const refrigRows = allRows.filter((row) => getPerformanceMachineGroup(row) === "Refrigeration");
    buttons.innerHTML = REFRIG_SUBGROUPS.map((sg) => {
        const count = filterRowsByRefrigSubgroup(refrigRows, sg.key).length;
        const active = machineExplorerRefrigSubgroup === sg.key;
        return `<button type="button" class="refrig-subgroup-btn${active ? " active" : ""}" data-refrig-subgroup="${escapeHtml(sg.key)}">
            ${escapeHtml(sg.label)}<span class="refrig-subgroup-count">${fmtNumber(count)}</span>
        </button>`;
    }).join("");
    buttons.querySelectorAll("[data-refrig-subgroup]").forEach((btn) => {
        btn.addEventListener("click", () => {
            machineExplorerRefrigSubgroup = btn.dataset.refrigSubgroup || "";
            machineExplorerRefrigCondenserGroupId = "";
            machineExplorerSelectedAssetId = "";
            renderMachineExplorer(getCategoryScopedAllRows());
        });
    });
}

// ---- Step 3: Condenser/Evaporator split panel (tree + detail) ----
// Only shown when Refrigeration + "condenser-evaporator" subgroup is active.
function renderRefrigCdeSection(rows) {
    const section = document.getElementById("refrig-cde-section");
    const step2 = document.getElementById("machine-asset-drill-section");
    const step3 = document.getElementById("machine-detail-panel");
    if (!section) return;
    const isCde = machineExplorerSelectedGroup === "Refrigeration" && machineExplorerRefrigSubgroup === "condenser-evaporator";
    section.style.display = isCde ? "" : "none";
    // Hide standard step-2 and step-3 panels while the CDE split panel is active.
    if (step2) step2.style.display = isCde ? "none" : "";
    if (step3) step3.style.display = isCde ? "none" : "";
    if (!isCde) return;
    renderRefrigCdeTree(rows);
    renderRefrigCdeDetail(rows);
}

function renderRefrigCdeTree(rows) {
    const treeList = document.getElementById("refrig-tree-list");
    if (!treeList) return;
    const search = (document.getElementById("refrig-tree-search")?.value || "").toLowerCase().trim();
    const allSummary = buildRefrigCondenserSummaryRows(rows);
    const visible = search
        ? allSummary.filter((c) =>
            c.condenserName.toLowerCase().includes(search) || c.condenserId.toLowerCase().includes(search)
            || c.evaporators.some((ev) => ev.name.toLowerCase().includes(search) || ev.id.toLowerCase().includes(search)))
        : allSummary;
    if (!visible.length) {
        treeList.innerHTML = `<div class="refrig-tree-empty">No condensers match the search.</div>`;
        return;
    }
    treeList.innerHTML = visible.map((entry) => {
        const isGroupSel = machineExplorerRefrigCondenserGroupId === entry.condenserId && !machineExplorerSelectedAssetId;
        const isCondenserSel = machineExplorerSelectedAssetId === entry.condenserId;
        const childSelected = entry.evaporators.some((ev) => ev.id === machineExplorerSelectedAssetId);
        const expanded = machineExplorerRefrigExpandedCondensers.has(entry.condenserId) || isGroupSel || isCondenserSel || childSelected;
        const openBadge = entry.open > 0 ? `<span class="refrig-tree-open-badge">${entry.open} open</span>` : "";
        return `<div class="refrig-tree-condenser${isGroupSel ? " group-selected" : ""}">
            <div class="refrig-tree-condenser-row">
                <button type="button" class="refrig-tree-toggle" data-cond-toggle="${escapeHtml(entry.condenserId)}">${expanded ? "▾" : "▸"}</button>
                <button type="button" class="refrig-tree-condenser-name${isGroupSel ? " active" : ""}${isCondenserSel ? " asset-active" : ""}"
                    data-sel-group="${escapeHtml(entry.condenserId)}">
                    <span class="refrig-tree-label">${escapeHtml(entry.condenserName)}</span>
                    <span class="refrig-tree-id">${escapeHtml(entry.condenserId)}</span>
                </button>
                <span class="refrig-tree-count">${fmtNumber(entry.total)}${openBadge}</span>
            </div>
            ${expanded ? `<div class="refrig-tree-evaporators">
                ${entry.evaporators.length === 0
                    ? `<div class="refrig-tree-ev-empty">No evaporators mapped.</div>`
                    : entry.evaporators.map((ev) => {
                        const evOpen = rows.filter((r) => getMachineAssetId(r) === ev.id && isNormalOpenMrStatus(getMrStatus(r))).length;
                        const evTotal = rows.filter((r) => getMachineAssetId(r) === ev.id).length;
                        const evSel = machineExplorerSelectedAssetId === ev.id;
                        return `<button type="button" class="refrig-tree-evaporator${evSel ? " active" : ""}" data-sel-asset="${escapeHtml(ev.id)}">
                            <span class="refrig-tree-ev-label">${escapeHtml(ev.name)}</span>
                            <span class="refrig-tree-id">${escapeHtml(ev.id)}</span>
                            <span class="refrig-tree-count">${fmtNumber(evTotal)}${evOpen > 0 ? `<span class="refrig-tree-open-badge">${evOpen}</span>` : ""}</span>
                        </button>`;
                    }).join("")}
            </div>` : ""}
        </div>`;
    }).join("");

    treeList.querySelectorAll("[data-cond-toggle]").forEach((btn) => {
        btn.addEventListener("click", (ev) => {
            ev.stopPropagation();
            const id = btn.dataset.condToggle;
            if (machineExplorerRefrigExpandedCondensers.has(id)) machineExplorerRefrigExpandedCondensers.delete(id);
            else machineExplorerRefrigExpandedCondensers.add(id);
            renderRefrigCdeTree(rows);
        });
    });
    // Selecting a condenser name shows group history (condenser + all evaporators).
    treeList.querySelectorAll("[data-sel-group]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const id = btn.dataset.selGroup;
            machineExplorerRefrigCondenserGroupId = id;
            machineExplorerSelectedAssetId = "";
            machineExplorerRefrigExpandedCondensers.add(id);
            renderRefrigCdeDetail(rows);
            renderRefrigCdeTree(rows);
        });
    });
    // Selecting an evaporator shows history for that specific asset only.
    treeList.querySelectorAll("[data-sel-asset]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const id = btn.dataset.selAsset;
            machineExplorerSelectedAssetId = id;
            const entry = getRefrigCondenserEntry(id);
            if (entry) {
                machineExplorerRefrigCondenserGroupId = entry.condenserId;
                machineExplorerRefrigExpandedCondensers.add(entry.condenserId);
            }
            renderRefrigCdeDetail(rows);
            renderRefrigCdeTree(rows);
        });
    });
}

function renderRefrigCdeDetail(rows) {
    const placeholder = document.getElementById("refrig-detail-placeholder");
    const kpiArea = document.getElementById("refrig-kpi-area");
    const historyWrapper = document.getElementById("refrig-history-wrapper");
    const titleEl = document.getElementById("refrig-detail-title");
    const metaEl = document.getElementById("refrig-detail-meta");
    const historyBody = document.getElementById("refrig-history-body");
    const hasSelection = machineExplorerRefrigCondenserGroupId || machineExplorerSelectedAssetId;
    if (placeholder) placeholder.style.display = hasSelection ? "none" : "";
    if (kpiArea) kpiArea.style.display = hasSelection ? "" : "none";
    if (historyWrapper) historyWrapper.style.display = hasSelection ? "" : "none";
    if (!hasSelection) return;

    let selRows = [];
    let titleText = "";
    let metaText = "";

    if (machineExplorerSelectedAssetId) {
        // Specific condenser or evaporator asset selected.
        const assetType = getRefrigAssetType(machineExplorerSelectedAssetId) || "--";
        const entry = getRefrigCondenserEntry(machineExplorerSelectedAssetId);
        selRows = rows.filter((row) => getMachineAssetId(row) === machineExplorerSelectedAssetId);
        titleText = `${getRefrigAssetDisplayName(machineExplorerSelectedAssetId)} | ${machineExplorerSelectedAssetId}`;
        metaText = `Asset Type: ${assetType}${assetType === "Evaporator" && entry ? ` | Parent: ${entry.condenserName}` : ""} | Machine Group: Refrigeration System | Subgroup: Condenser-Evaporator Network`;
    } else {
        // Condenser group selected — condenser parent + all connected evaporators.
        const entry = REFRIGERATION_NETWORK.find((e) => e.condenserId === machineExplorerRefrigCondenserGroupId);
        if (!entry) return;
        const allIds = new Set([entry.condenserId, ...entry.evaporators.map((ev) => ev.id)]);
        selRows = rows.filter((row) => allIds.has(getMachineAssetId(row)));
        titleText = `${entry.condenserName} — Condenser Group`;
        metaText = `${entry.condenserId} | ${entry.evaporators.length} connected evaporator${entry.evaporators.length === 1 ? "" : "s"} | Machine Group: Refrigeration System | Subgroup: Condenser-Evaporator Network`;
    }

    selRows = filterMachineHistoryPeriodRows(selRows);

    if (titleEl) titleEl.textContent = titleText;
    if (metaEl) metaEl.textContent = metaText;

    const summary = summarizeMachineExplorerRows(selRows);
    setText("refrig-kpi-total", fmtNumber(summary.total));
    setText("refrig-kpi-open", fmtNumber(summary.open));
    setText("refrig-kpi-not-ack", fmtNumber(summary.notAcknowledged));
    setText("refrig-kpi-progress", fmtNumber(summary.inProgress));
    setText("refrig-kpi-finished", fmtNumber(summary.finished));
    setText("refrig-kpi-closure", summary.closureRate !== null ? fmtPercent(summary.closureRate) : "--");
    setText("refrig-kpi-mttr", summary.averageTtr !== null ? fmtHours(summary.averageTtr) : "--");
    setText("refrig-kpi-oldest", summary.oldestOpenAge !== null ? formatDays(summary.oldestOpenAge) : "--");
    setText("refrig-kpi-invalid", fmtNumber(summary.invalid));

    if (!historyBody) return;
    if (!selRows.length) {
        historyBody.innerHTML = `<tr><td colspan="18" class="empty-cell">No WO/MR records for this selection.</td></tr>`;
        return;
    }
    const sorted = [...selRows].sort(compareMachineHistoryRows);
    historyBody.innerHTML = sorted.map((row, i) => {
        const raised = getMrRaisedDate(row).date;
        const actualStart = parseDateValue(row.actual_start_time || row.maintenance_start_time);
        const actualEnd = parseDateValue(row.actual_end_time || row.maintenance_end_time);
        const description = getMrDescription(row);
        const assetId = getMachineAssetId(row);
        const assetName = getRefrigAssetDisplayName(assetId) || getMachineEquipmentName(row);
        const assetType = getRefrigAssetType(assetId) || "--";
        const parentEntry = getRefrigCondenserEntry(assetId);
        const parentLabel = assetType === "Evaporator" && parentEntry ? parentEntry.condenserName : (assetType === "Condenser" ? assetId : "--");
        return `<tr>
            <td>${escapeHtml(getMrRequestId(row, i) || "--")}</td>
            <td>${escapeHtml(getMrWorkOrderOnlyId(row) || "--")}</td>
            <td class="asset-id-cell">${escapeHtml(assetId || "--")}</td>
            <td><div class="cell-title">${escapeHtml(assetName || "--")}</div></td>
            <td>${renderBadgeCell("refrig-type", assetType)}</td>
            <td>${escapeHtml(parentLabel)}</td>
            <td>${renderBadgeCell("status", getMrStatus(row))}</td>
            <td>${renderBadgeCell("service", getMrServiceLevel(row))}</td>
            <td class="description-cell">${escapeHtml(description || "--")}</td>
            <td class="description-cell" title="${escapeHtml(row.translated_description || "")}">${escapeHtml(row.translated_description && row.translated_description !== description ? row.translated_description : "--")}</td>
            <td>${escapeHtml(getMrCreatedBy(row))}</td>
            <td>${escapeHtml(getMrStartedBy(row))}</td>
            <td>${escapeHtml(raised ? fmtDateTime(raised) : "--")}</td>
            <td>${escapeHtml(actualStart ? fmtDateTime(actualStart) : "--")}</td>
            <td>${escapeHtml(actualEnd ? fmtDateTime(actualEnd) : "--")}</td>
            <td>${escapeHtml(getRowAgeOrDuration(row))}</td>
            <td>${renderBadgeCell("ack", getAcknowledgementStatus(row))}</td>
            <td>${renderBadgeCell("quality", getDataQualityFlag(row))}</td>
        </tr>`;
    }).join("");
}

// ---- End Refrigeration System helpers ----

function buildMachineExplorerAssetRows(rows = []) {
    const byAsset = new Map();
    rows.forEach((row) => {
        const assetId = getMachineAssetId(row) || getMachineEquipmentName(row);
        if (!assetId || assetId === "--") return;
        const bucket = byAsset.get(assetId) || { assetId, rows: [] };
        bucket.rows.push(row);
        byAsset.set(assetId, bucket);
    });
    const assetRows = [...byAsset.values()].map((bucket) => {
        const first = bucket.rows[0] || {};
        const summary = summarizeMachineExplorerRows(bucket.rows);
        return {
            ...bucket,
            ...summary,
            name: getMachineEquipmentName(first),
            criticality: bucket.rows.some(rowIsCritical) ? "Critical" : "Non-Critical / Facility",
            machineGroup: getPerformanceMachineGroup(first),
        };
    });
    return assetRows.sort(compareMachineExplorerAssetRows);
}

function compareMachineExplorerAssetRows(a, b) {
    const oldestA = a.oldestOpenAge ?? -1;
    const oldestB = b.oldestOpenAge ?? -1;
    const latestA = a.latestDate ? a.latestDate.getTime() : 0;
    const latestB = b.latestDate ? b.latestDate.getTime() : 0;
    const closureA = a.closureRate ?? -1;
    const closureB = b.closureRate ?? -1;
    if (machineExplorerSort === "latest_wo_mr") return latestB - latestA || b.total - a.total || a.name.localeCompare(b.name);
    if (machineExplorerSort === "least_closure_rate") return closureA - closureB || b.total - a.total || oldestB - oldestA || a.name.localeCompare(b.name);
    if (machineExplorerSort === "highest_closure_rate") return closureB - closureA || b.total - a.total || latestB - latestA || a.name.localeCompare(b.name);
    if (machineExplorerSort === "oldest_open_age") return oldestB - oldestA || b.open - a.open || b.total - a.total || a.name.localeCompare(b.name);
    if (machineExplorerSort === "most_invalid") return b.invalid - a.invalid || b.total - a.total || oldestB - oldestA || a.name.localeCompare(b.name);
    return b.total - a.total || latestB - latestA || b.open - a.open || oldestB - oldestA || a.name.localeCompare(b.name);
}

function renderMachineExplorerAssetSummary(assetRows = []) {
    const body = document.getElementById("machine-asset-summary-body");
    if (!body) return;
    const groupLabel = machineExplorerSelectedGroup || MACHINE_EXPLORER_ALL_GROUP;
    setText(
        "machine-asset-summary-meta",
        assetRows.length
            ? `${fmtNumber(assetRows.length)} machine${assetRows.length === 1 ? "" : "s"} in ${groupLabel}. Select a row below to continue to Step 3.`
            : `No machines match the current filters in ${groupLabel}.`
    );
    if (!assetRows.length) {
        body.innerHTML = `<tr><td colspan="14" class="empty-cell">No machines match the current Machine Explorer filters.</td></tr>`;
        return;
    }
    const shownRows = assetRows.slice(0, MACHINE_EXPLORER_ASSET_RENDER_LIMIT);
    const hiddenCount = Math.max(0, assetRows.length - shownRows.length);
    body.innerHTML = shownRows.map((row) => `
        <tr class="machine-asset-row ${row.assetId === machineExplorerSelectedAssetId ? "selected" : ""}" data-asset-id="${escapeHtml(row.assetId)}" tabindex="0">
            <td class="asset-id-cell">${escapeHtml(row.assetId)}</td>
            <td><div class="cell-title">${escapeHtml(row.name || "--")}</div></td>
            <td>${renderBadgeCell("criticality", row.criticality)}</td>
            <td>${escapeHtml(row.machineGroup || "--")}</td>
            <td>${fmtNumber(row.total)}</td>
            <td>${fmtNumber(row.open)}</td>
            <td>${fmtNumber(row.notAcknowledged)}</td>
            <td>${fmtNumber(row.inProgress)}</td>
            <td>${fmtNumber(row.finished)}</td>
            <td>${row.closureRate !== null ? fmtPercent(row.closureRate) : "--"}</td>
            <td>${row.averageTtr !== null ? fmtHours(row.averageTtr) : "--"}</td>
            <td>${escapeHtml(row.latestDate ? fmtDateTime(row.latestDate) : "--")}</td>
            <td>${row.oldestOpenAge !== null ? formatDays(row.oldestOpenAge) : "--"}</td>
            <td>${fmtNumber(row.invalid)}</td>
        </tr>
    `).join("") + (hiddenCount
        ? `<tr><td colspan="14" class="empty-cell">${fmtNumber(hiddenCount)} more machine rows hidden for performance. Use the group, criticality, acknowledgement, or search filters to narrow the list.</td></tr>`
        : "");
    body.querySelectorAll("[data-asset-id]").forEach((row) => {
        const selectAsset = () => {
            machineExplorerSelectedAssetId = row.dataset.assetId || "";
            renderMachineExplorer(getCategoryScopedAllRows());
        };
        row.addEventListener("click", selectAsset);
        row.addEventListener("keydown", (event) => {
            if (event.key !== "Enter" && event.key !== " ") return;
            event.preventDefault();
            selectAsset();
        });
    });
}

function getMachineHistoryPeriodState() {
    const yearSelect = document.getElementById("machine-history-year");
    const monthSelect = document.getElementById("machine-history-month");
    const fromInput = document.getElementById("machine-history-date-from");
    const toInput = document.getElementById("machine-history-date-to");
    return {
        year: yearSelect ? yearSelect.value : machineHistoryYearFilter,
        month: monthSelect ? monthSelect.value : machineHistoryMonthFilter,
        from: fromInput ? fromInput.value : machineHistoryDateFrom,
        to: toInput ? toInput.value : machineHistoryDateTo,
    };
}

function syncMachineHistoryPeriodInputs() {
    const yearSelect = document.getElementById("machine-history-year");
    if (yearSelect) yearSelect.value = machineHistoryYearFilter;
    const monthSelect = document.getElementById("machine-history-month");
    if (monthSelect) monthSelect.value = machineHistoryMonthFilter;
    const fromInput = document.getElementById("machine-history-date-from");
    if (fromInput) fromInput.value = machineHistoryDateFrom;
    const toInput = document.getElementById("machine-history-date-to");
    if (toInput) toInput.value = machineHistoryDateTo;
}

function normalizeMachineHistoryDateRange() {
    if (machineHistoryDateFrom && machineHistoryDateTo && machineHistoryDateFrom > machineHistoryDateTo) {
        [machineHistoryDateFrom, machineHistoryDateTo] = [machineHistoryDateTo, machineHistoryDateFrom];
    }
}

function rowMatchesMachineHistoryPeriod(row) {
    // Step 3 period filters use the raised/created date, matching the MR Raised logic used elsewhere on Downtime.
    const { year, month, from, to } = getMachineHistoryPeriodState();
    if (!year && !month && !from && !to) return true;
    const raised = getMrRaisedDate(row).date;
    if (!raised) return false;
    const raisedDay = formatDateInputValue(raised);
    if (from || to) {
        if (from && raisedDay < from) return false;
        if (to && raisedDay > to) return false;
        return true;
    }
    if (year && String(raised.getFullYear()) !== String(year)) return false;
    if (month && String(raised.getMonth() + 1).padStart(2, "0") !== String(month)) return false;
    return true;
}

function filterMachineHistoryPeriodRows(rows = []) {
    return rows.filter(rowMatchesMachineHistoryPeriod);
}

function compareLatestMrDateDesc(a, b) {
    const aDate = getLatestMrDate(a);
    const bDate = getLatestMrDate(b);
    return (bDate ? bDate.getTime() : 0) - (aDate ? aDate.getTime() : 0);
}

function getMachineHistoryAckRank(row, acknowledgedFirst = false) {
    const ackStatus = normalizeClassification(getAcknowledgementStatus(row));
    const isNotAcknowledged = ackStatus === "not acknowledged";
    const isAcknowledged = ackStatus === "acknowledged in progress" || ackStatus === "acknowledged" || ackStatus === "closed";
    if (isAcknowledged) return acknowledgedFirst ? 0 : 2;
    if (isNotAcknowledged) return acknowledgedFirst ? 2 : 0;
    if (ackStatus === "review") return 1;
    return 3;
}

function getMachineHistoryServiceRank(row) {
    const service = String(getMrServiceLevel(row) || "").trim();
    const match = service.match(/-?\d+(\.\d+)?/);
    return match ? Number(match[0]) : Number.POSITIVE_INFINITY;
}

function getMachineHistoryStatusRank(row) {
    const status = normalizeClassification(getMrStatus(row));
    if (status === "new") return 0;
    if (status === "confirm") return 1;
    if (status === "rejected" || status === "reject") return 2;
    if (status.replace(/\s+/g, "") === "inprogress") return 3;
    if (status === "rework" || status === "re work") return 4;
    if (status === "finished") return 5;
    return Number.POSITIVE_INFINITY;
}

function compareKnownRank(aRank, bRank, reverse = false) {
    const aUnknown = !Number.isFinite(aRank);
    const bUnknown = !Number.isFinite(bRank);
    if (aUnknown !== bUnknown) return aUnknown ? 1 : -1;
    if (aUnknown && bUnknown) return 0;
    return reverse ? bRank - aRank : aRank - bRank;
}

function compareMachineHistoryRows(a, b) {
    // Step 3 history sorting is separate from Machine Explorer filters so users can reorder the same result set.
    if (machineHistorySort === "ack_not_first" || machineHistorySort === "ack_yes_first") {
        const ackCompare = getMachineHistoryAckRank(a, machineHistorySort === "ack_yes_first")
            - getMachineHistoryAckRank(b, machineHistorySort === "ack_yes_first");
        return ackCompare || compareLatestMrDateDesc(a, b);
    }
    if (machineHistorySort === "service_low_first" || machineHistorySort === "service_high_first") {
        const serviceCompare = compareKnownRank(
            getMachineHistoryServiceRank(a),
            getMachineHistoryServiceRank(b),
            machineHistorySort === "service_high_first"
        );
        return serviceCompare || compareLatestMrDateDesc(a, b);
    }
    if (machineHistorySort === "status_forward" || machineHistorySort === "status_reverse") {
        const statusCompare = compareKnownRank(
            getMachineHistoryStatusRank(a),
            getMachineHistoryStatusRank(b),
            machineHistorySort === "status_reverse"
        );
        return statusCompare || compareLatestMrDateDesc(a, b);
    }
    return compareLatestMrDateDesc(a, b);
}

function renderMatchCell(row) {
    const sm = row.smartMatch;
    if (!sm) return `<td class="match-cell"><span class="match-na">--</span></td>`;
    const conf = String(sm.confidence || "").toLowerCase();
    const mismatch = sm.possibleAssetCodingMismatch
        ? `<span class="match-flag" title="The record's own Asset ID differs from the matched asset — possible asset coding mismatch.">Possible asset coding mismatch</span>`
        : "";
    return `
        <td class="match-cell">
            <span class="match-source">${escapeHtml(sm.matchSource || "")}</span>
            <span class="match-conf match-conf-${escapeHtml(conf)}">${escapeHtml(sm.confidence || "")}</span>
            ${mismatch}
        </td>`;
}

function renderMachineHistoryRow(row, index) {
    const raised = getMrRaisedDate(row).date;
    const actualStart = parseDateValue(row.actual_start_time || row.maintenance_start_time);
    const actualEnd = parseDateValue(row.actual_end_time || row.maintenance_end_time);
    const qualityFlag = getDataQualityFlag(row);
    const description = getMrDescription(row);
    return `
        <tr>
            <td>${escapeHtml(getMrRequestId(row, index) || "--")}</td>
            <td>${escapeHtml(getMrWorkOrderOnlyId(row) || "--")}</td>
            ${renderMatchCell(row)}
            <td>${renderBadgeCell("status", getMrStatus(row))}</td>
            <td>${renderBadgeCell("service", getMrServiceLevel(row))}</td>
            <td>${renderBadgeCell("wo-type", row.job_trade || row.maintenance_job_type || "")}</td>
            <td class="description-cell" title="${escapeHtml(description || "")}">${escapeHtml(description || "--")}</td>
            <td class="description-cell" title="${escapeHtml(row.translated_description || "")}">${escapeHtml(row.translated_description && row.translated_description !== description ? row.translated_description : "--")}</td>
            <td>${escapeHtml(getMrStartedBy(row))}</td>
            <td>${escapeHtml(getMrCreatedBy(row))}</td>
            <td>${escapeHtml(raised ? fmtDateTime(raised) : "--")}</td>
            <td>${escapeHtml(actualStart ? fmtDateTime(actualStart) : "--")}</td>
            <td>${escapeHtml(actualEnd ? fmtDateTime(actualEnd) : "--")}</td>
            <td>${escapeHtml(getRowAgeOrDuration(row))}</td>
            <td>${renderBadgeCell("ack", getAcknowledgementStatus(row))}</td>
            <td>${renderBadgeCell("quality", qualityFlag)}</td>
        </tr>
    `;
}

function _applyHistorySearch(rows) {
    const search = machineHistorySearch.toLowerCase().trim();
    if (!search) return rows;
    const substr = rows.filter((row) => {
        const desc = (getMrDescription(row) || "").toLowerCase();
        const translated = (row.translated_description || "").toLowerCase();
        const mrId = (getMrRequestId(row, 0) || "").toLowerCase();
        const woId = (getMrWorkOrderOnlyId(row) || "").toLowerCase();
        const assetId = (getMachineAssetId(row) || "").toLowerCase();
        return desc.includes(search) || translated.includes(search) || mrId.includes(search)
            || woId.includes(search) || assetId.includes(search);
    });
    // In All-Assets mode, also fold in smart asset matches (aliases / acronyms /
    // number-aware), so "Combi 1" / "SBF 1" find differently-worded records.
    if (machineHistoryViewMode !== "all" || !window.AssetMatcher || !Object.keys(assetProfiles).length) {
        return substr;
    }
    const smart = window.AssetMatcher.searchRecords(rows, machineHistorySearch, assetProfiles, { includeRelated: includeRelatedMatches });
    const seen = new Set(substr.map((r) => r.work_order_id || r));
    const merged = substr.slice();
    smart.forEach((r) => { const key = r.work_order_id || r; if (!seen.has(key)) { seen.add(key); merged.push(r); } });
    return merged;
}

function renderMachineExplorerHistory(rows = [], assetRows = []) {
    const body = document.getElementById("machine-history-body");
    if (!body) return;
    const wrapper = body.closest(".machine-history-wrapper");
    const setHistoryEmpty = (isEmpty) => {
        if (wrapper) wrapper.classList.toggle("is-empty", Boolean(isEmpty));
    };

    const viewAll = machineHistoryViewMode === "all";

    // Sync toggle button states
    document.querySelectorAll("#history-view-toggle .toggle-btn").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.historyMode === machineHistoryViewMode);
    });

    if (viewAll) {
        const filtered = _applyHistorySearch(filterMachineHistoryPeriodRows(filterMachineExplorerRows(rows, { includeAssetFilter: false })).sort(compareMachineHistoryRows));
        renderMachineExplorerKpis(filtered);
        const groupLabel = machineExplorerSelectedGroup && machineExplorerSelectedGroup !== MACHINE_EXPLORER_ALL_GROUP
            ? machineExplorerSelectedGroup
            : "All Groups";
        setMachineHistoryContext("Viewing history for: All assets in current filters.");
        setText("machine-explorer-title", "WO/MR History");
        setText("machine-explorer-meta", `Showing all ${fmtNumber(filtered.length)} WO/MR record${filtered.length === 1 ? "" : "s"} for current filters.`);
        setText("machine-explorer-helper", `All Assets view includes every WO/MR that matches the current Machine Explorer filters in ${groupLabel}, plus any history filters applied below.`);
        if (!filtered.length) {
            setHistoryEmpty(true);
            body.innerHTML = `<tr><td colspan="16" class="empty-cell">No WO/MR records match the current filters.</td></tr>`;
            return;
        }
        setHistoryEmpty(false);
        const shownHistoryRows = filtered.slice(0, MACHINE_HISTORY_RENDER_LIMIT);
        const hiddenCount = Math.max(0, filtered.length - shownHistoryRows.length);
        body.innerHTML = shownHistoryRows.map((row, index) => renderMachineHistoryRow(row, index)).join("") + (hiddenCount
            ? `<tr><td colspan="16" class="empty-cell">${fmtNumber(hiddenCount)} more WO/MR record${hiddenCount === 1 ? "" : "s"} hidden for performance. Use year, month, date range, or search filters to narrow the list.</td></tr>`
            : "");
        return;
    }

    // Selected-asset mode
    const selectedAsset = assetRows.find((row) => row.assetId === machineExplorerSelectedAssetId);
    if (!selectedAsset) {
        resetMachineExplorerKpis();
        setMachineHistoryContext("Select an asset above to view WO/MR history.", true);
        setText("machine-explorer-title", "WO/MR History");
        setText("machine-explorer-meta", "Select a machine/asset from Step 2. The table stays empty until an asset is selected.");
        setText("machine-explorer-helper", "Selected Asset view includes direct Asset ID records and related records detected from asset name, description, translated description, and functional location.");
        setHistoryEmpty(true);
        body.innerHTML = `<tr><td colspan="16" class="empty-cell">Select an asset above to view WO/MR history.</td></tr>`;
        return;
    }

    // Smart matching: direct Asset ID records + related records detected from
    // name / description / translated description / functional location.
    const matched = getSelectedAssetMatchedRows(rows, selectedAsset.assetId);
    const filtered = _applyHistorySearch(filterMachineHistoryPeriodRows(matched).sort(compareMachineHistoryRows));
    renderMachineExplorerKpis(filtered);
    setHistoryEmpty(!filtered.length);
    const refrigType = getRefrigAssetType(selectedAsset.assetId);
    const refrigEntry = refrigType ? getRefrigCondenserEntry(selectedAsset.assetId) : null;
    const refrigSubtext = refrigType
        ? ` | Asset Type: ${refrigType}${refrigEntry && refrigType === "Evaporator" ? ` | Parent: ${refrigEntry.condenserName}` : " | Subgroup: Condenser-Evaporator Network"}`
        : "";
    setMachineHistoryContext(`Viewing history for: ${selectedAsset.assetId} - ${selectedAsset.name || "--"}`);
    setText("machine-explorer-title", "WO/MR History");
    const profile = assetProfiles[selectedAsset.assetId];
    const summary = (profile && window.AssetMatcher)
        ? window.AssetMatcher.summarizeSelectedAsset(filtered, profile)
        : null;
    const matchMeta = summary
        ? ` | ${fmtNumber(summary.directAssetIdMatches)} direct ID, ${fmtNumber(summary.relatedMatches)} related, ${fmtNumber(summary.possibleCodingMismatches)} possible coding mismatch${summary.possibleCodingMismatches === 1 ? "" : "es"}`
        : "";
    setText(
        "machine-explorer-meta",
        `Criticality: ${selectedAsset.criticality} | Machine Group: ${selectedAsset.machineGroup || "--"}${refrigSubtext} | ${fmtNumber(filtered.length)} WO/MR record${filtered.length === 1 ? "" : "s"} after filters${matchMeta}.`
    );
    setText(
        "machine-explorer-helper",
        summary
            ? `${summary.summaryText} Toggle "Include possible related matches" to also show low-confidence matches.`
            : 'Selected Asset view includes direct Asset ID records and related records detected from asset name, description, translated description, and functional location. Toggle "Include possible related matches" to also show low-confidence matches.'
    );
    if (!filtered.length) {
        body.innerHTML = `<tr><td colspan="16" class="empty-cell">No WO/MR records match this asset and the current filters.</td></tr>`;
        return;
    }
    const shownHistoryRows = filtered.slice(0, MACHINE_HISTORY_RENDER_LIMIT);
    const hiddenCount = Math.max(0, filtered.length - shownHistoryRows.length);
    body.innerHTML = shownHistoryRows.map((row, index) => renderMachineHistoryRow(row, index)).join("") + (hiddenCount
        ? `<tr><td colspan="16" class="empty-cell">${fmtNumber(hiddenCount)} more WO/MR record${hiddenCount === 1 ? "" : "s"} hidden for performance. Use year, month, date range, or search filters to narrow the list.</td></tr>`
        : "");
}

function getSelectedAssetMatchedRows(rows, assetId) {
    const profile = assetProfiles[assetId];
    if (profile && window.AssetMatcher) {
        // Apply every non-asset, non-group filter (period/status/etc.), then smart
        // match so records mis-coded under a general area are still found.
        const base = filterMachineExplorerRows(rows, { includeAssetFilter: false, includeGroupFilter: false });
        return window.AssetMatcher.filterRecordsForSelectedAsset(base, profile, { includeRelated: includeRelatedMatches });
    }
    // Fallback (profile not loaded yet): strict Asset ID match.
    return filterMachineExplorerRows(rows, { selectedAssetId: assetId });
}

function exportMachineExplorerData() {
    if (typeof XLSX === "undefined") {
        alert("Export library not loaded. Please refresh the page and try again.");
        return;
    }
    const rows = getCategoryScopedAllRows();

    // Resolve history rows (same as what's displayed in section 3)
    let historyRows;
    if (machineHistoryViewMode === "all") {
        historyRows = _applyHistorySearch(filterMachineHistoryPeriodRows(
            filterMachineExplorerRows(rows, { includeAssetFilter: false })
        ).sort(compareMachineHistoryRows));
    } else {
        const baseRows = filterMachineExplorerRows(rows, { includeAssetFilter: false });
        const assetSummary = buildMachineExplorerAssetRows(baseRows);
        const selectedAsset = assetSummary.find((r) => r.assetId === machineExplorerSelectedAssetId);
        if (!selectedAsset) {
            alert("Please select an asset first, or switch to \"All Assets\" view before exporting.");
            return;
        }
        historyRows = _applyHistorySearch(filterMachineHistoryPeriodRows(
            filterMachineExplorerRows(rows, { selectedAssetId: selectedAsset.assetId })
        ).sort(compareMachineHistoryRows));
    }
    if (!historyRows.length) {
        alert("No WO/MR records to export for the current selection and filters.");
        return;
    }

    // Asset summary = step-2 list (all assets in current group/period filters)
    const assetRows = buildMachineExplorerAssetRows(
        filterMachineExplorerRows(rows, { includeAssetFilter: false })
    );

    // ── Style helpers ────────────────────────────────────────────────────────
    const solid = (rgb) => ({ patternType: "solid", fgColor: { rgb }, bgColor: { rgb } });
    const fnt = (rgb, bold = false) => ({ color: { rgb }, bold, sz: 9, name: "Calibri" });
    const aln = (h = "left", wrap = false) => ({ horizontal: h, vertical: "middle", wrapText: wrap });

    const mk = (fillRgb, textRgb, bold = false, h = "left") => ({
        font: fnt(textRgb, bold),
        fill: solid(fillRgb),
        alignment: aln(h),
    });

    const HEADER = {
        font: { color: { rgb: "FFFFFF" }, bold: true, sz: 9, name: "Calibri" },
        fill: solid("0F766E"),
        alignment: aln("center", true),
        border: { bottom: { style: "medium", color: { rgb: "0D5C56" } } },
    };

    const STRIPE = [mk("FFFFFF", "1E293B"), mk("F0FDF8", "1E293B")]; // white / very-light-teal

    // Status color map
    const statusStyle = (s) => {
        const n = normalizeClassification(s);
        if (n === "finished") return mk("D1FAE5", "065F46", true);
        if (n === "in progress" || n === "inprogress") return mk("FEF3C7", "92400E", true);
        if (n === "new") return mk("FEE2E2", "991B1B", true);
        return mk("F1F5F9", "475569");
    };

    // PM / CM
    const typeStyle = (jt) => {
        if (isPmJobTrade(jt)) return mk("D1FAE5", "065F46", true, "center");
        if (jt && jt.trim() && jt.trim() !== "--") return mk("FEF3C7", "92400E", true, "center");
        return mk("F1F5F9", "94A3B8", false, "center");
    };

    // Service Level 1-4
    const slStyle = (sl) => {
        const SL_MAP = {
            "1": mk("FEE2E2", "991B1B", true, "center"),
            "2": mk("FFEDD5", "9A3412", true, "center"),
            "3": mk("FEF9C3", "713F12", true, "center"),
            "4": mk("DBEAFE", "1E40AF", true, "center"),
        };
        return SL_MAP[String(sl).trim()] || mk("F1F5F9", "64748B", false, "center");
    };

    // Criticality
    const critStyle = (isCrit) => isCrit
        ? mk("FEE2E2", "991B1B", true)
        : mk("F1F5F9", "475569");

    // Acknowledgement
    const ackStyle = (a) => {
        const n = normalizeClassification(a);
        if (n === "not acknowledged") return mk("FEE2E2", "991B1B", true);
        if (n.includes("review")) return mk("FEF3C7", "92400E", true);
        return mk("ECFDF5", "065F46");
    };

    // Data quality
    const dqStyle = (dq) => dq === "Valid"
        ? mk("D1FAE5", "065F46")
        : mk("FEF3C7", "92400E", true);

    // Numeric traffic-light helpers
    const closureStyle = (r) => {
        if (r === null || r === undefined) return mk("F1F5F9", "64748B", false, "center");
        if (r >= 80) return mk("D1FAE5", "065F46", false, "center");
        if (r >= 50) return mk("FEF3C7", "92400E", false, "center");
        return mk("FEE2E2", "991B1B", true, "center");
    };
    const ageStyle = (d) => {
        if (d === null || d === undefined) return mk("D1FAE5", "065F46", false, "center");
        if (d <= 30) return mk("D1FAE5", "065F46", false, "center");
        if (d <= 90) return mk("FEF3C7", "92400E", false, "center");
        return mk("FEE2E2", "991B1B", true, "center");
    };
    const countStyle = (n, warnAt = 1, redAt = 6, h = "center") => {
        if (!n) return mk("D1FAE5", "065F46", false, h);
        if (n < redAt) return mk("FEF3C7", "92400E", false, h);
        return mk("FEE2E2", "991B1B", true, h);
    };

    // Apply style to a cell (no-op if cell absent)
    const cs = (ws, r, c, style) => {
        const addr = XLSX.utils.encode_cell({ r, c });
        if (ws[addr]) ws[addr].s = style;
    };

    // ── Sheet 1: WO/MR History ────────────────────────────────────────────────
    // Column order: identity → operational priority → references → timeline → narrative → admin
    const H1 = [
        "Asset ID", "Asset Name", "Machine Group", "Criticality",   // 0-3  identity
        "Status", "Type", "Service Level", "Acknowledgement",        // 4-7  operational priority
        "MR / WO Number", "Work Order ID",                           // 8-9  references
        "Created Date", "Actual Start", "Actual End", "Duration",    // 10-13 timeline
        "Description", "Translated Description",                     // 14-15 narrative
        "Started By", "Created By", "Data Quality",                  // 16-18 admin
    ];
    const D1 = [H1];

    historyRows.forEach((row) => {
        const raised   = getMrRaisedDate(row).date;
        const actStart = parseDateValue(row.actual_start_time || row.maintenance_start_time);
        const actEnd   = parseDateValue(row.actual_end_time   || row.maintenance_end_time);
        const jt       = row.job_trade || row.maintenance_job_type || "";
        const woType   = isPmJobTrade(jt) ? "PM" : (jt && jt.trim() && jt.trim() !== "--" ? "CM" : "--");
        const desc     = getMrDescription(row);
        D1.push([
            getMachineAssetId(row) || "--",
            getMachineEquipmentName(row) || "--",
            getPerformanceMachineGroup(row) || "--",
            rowIsCritical(row) ? "Critical" : "Non-Critical",
            getMrStatus(row) || "--",
            woType,
            getMrServiceLevel(row) || "--",
            getAcknowledgementStatus(row) || "--",
            getMrRequestId(row) || "--",
            getMrWorkOrderOnlyId(row) || "--",
            raised    ? fmtDateTime(raised)    : "--",
            actStart  ? fmtDateTime(actStart)  : "--",
            actEnd    ? fmtDateTime(actEnd)    : "--",
            getRowAgeOrDuration(row) || "--",
            desc || "--",
            (row.translated_description && row.translated_description !== desc) ? row.translated_description : "--",
            getMrStartedBy(row) || "--",
            getMrCreatedBy(row) || "--",
            getDataQualityFlag(row) || "--",
        ]);
    });

    const ws1 = XLSX.utils.aoa_to_sheet(D1);
    ws1["!cols"] = [16,30,22,16, 14,8,14,20, 20,16, 20,20,20,18, 46,46, 22,22,24].map((w) => ({ wch: w }));
    ws1["!rows"] = [{ hpt: 34 }];

    // Header row
    H1.forEach((_, c) => cs(ws1, 0, c, HEADER));

    // Data rows
    historyRows.forEach((row, i) => {
        const r  = i + 1;
        const jt = row.job_trade || row.maintenance_job_type || "";
        // Stripe base
        H1.forEach((_, c) => cs(ws1, r, c, STRIPE[i % 2]));
        // Override styled columns
        cs(ws1, r, 3, critStyle(rowIsCritical(row)));
        cs(ws1, r, 4, statusStyle(getMrStatus(row)));
        cs(ws1, r, 5, typeStyle(jt));
        cs(ws1, r, 6, slStyle(getMrServiceLevel(row)));
        cs(ws1, r, 7, ackStyle(getAcknowledgementStatus(row)));
        cs(ws1, r, 18, dqStyle(getDataQualityFlag(row)));
    });

    // ── Sheet 2: Asset Summary ────────────────────────────────────────────────
    const H2 = [
        "Asset ID", "Asset Name", "Machine Group", "Criticality",   // 0-3
        "Total WO/MR", "Open WO/MR", "Not Acknowledged",            // 4-6
        "In Progress", "Finished", "Closure Rate (%)",              // 7-9
        "Avg MTTR (hrs)", "Oldest Open MR (days)",                  // 10-11
        "Latest WO/MR Date", "Invalid / Missing",                   // 12-13
    ];
    const D2 = [H2];

    assetRows.forEach((asset) => {
        const cr = asset.closureRate;
        D2.push([
            asset.assetId || "--",
            asset.name || "--",
            asset.machineGroup || "--",
            asset.criticality === "Critical" ? "Critical" : "Non-Critical",
            asset.total ?? 0,
            asset.open ?? 0,
            asset.notAcknowledged ?? 0,
            asset.inProgress ?? 0,
            asset.finished ?? 0,
            cr != null ? parseFloat(cr.toFixed(1)) : null,
            asset.averageTtr != null ? parseFloat(asset.averageTtr.toFixed(2)) : null,
            asset.oldestOpenAge ?? null,
            asset.latestDate ? fmtDateTime(asset.latestDate) : "--",
            asset.invalid ?? 0,
        ]);
    });

    const ws2 = XLSX.utils.aoa_to_sheet(D2);
    ws2["!cols"] = [16,30,22,16, 13,12,18, 13,12,16, 16,22, 22,18].map((w) => ({ wch: w }));
    ws2["!rows"] = [{ hpt: 34 }];

    H2.forEach((_, c) => cs(ws2, 0, c, HEADER));

    assetRows.forEach((asset, i) => {
        const r = i + 1;
        H2.forEach((_, c) => cs(ws2, r, c, STRIPE[i % 2]));
        cs(ws2, r, 3,  critStyle(asset.criticality === "Critical"));
        cs(ws2, r, 5,  countStyle(asset.open, 1, 6));
        cs(ws2, r, 6,  countStyle(asset.notAcknowledged, 1, 3));
        cs(ws2, r, 9,  closureStyle(asset.closureRate));
        cs(ws2, r, 11, ageStyle(asset.oldestOpenAge));
        cs(ws2, r, 13, countStyle(asset.invalid, 1, 6));
    });

    // ── Write ────────────────────────────────────────────────────────────────
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws1, "WO-MR History");
    XLSX.utils.book_append_sheet(wb, ws2, "Asset Summary");

    const now = new Date();
    const dateStr   = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, "0")}${String(now.getDate()).padStart(2, "0")}`;
    const groupPart = (machineExplorerSelectedGroup && machineExplorerSelectedGroup !== MACHINE_EXPLORER_ALL_GROUP)
        ? machineExplorerSelectedGroup.replace(/[^a-zA-Z0-9]/g, "-").replace(/-+/g, "-")
        : "All-Groups";
    const viewPart  = machineHistoryViewMode === "all"
        ? "All-Assets"
        : (machineExplorerSelectedAssetId || "Selected").replace(/[^a-zA-Z0-9]/g, "-").replace(/-+/g, "-");
    XLSX.writeFile(wb, `WO-MR_${groupPart}_${viewPart}_${dateStr}.xlsx`);
}

function renderMachineExplorer(rows = []) {
    // 4-level drill-down: 1. Group → 2. Sub Machine Group (Refrigeration) → 3. Machine/Asset → 4. WO/MR History.
    // "Condenser / Evaporator" subgroup replaces step 2+3 with a compact tree + detail split panel.
    populateMachineExplorerFilters(rows);
    const groupRows = filterMachineExplorerRows(rows, { includeGroupFilter: false });
    renderMachineExplorerGroupCards(groupRows);
    renderRefrigSubgroupSection(rows);    // step 2: subgroup buttons (Refrigeration only)
    renderRefrigCdeSection(rows);         // step 3: CDE split panel (Condenser/Evaporator subgroup only)

    // Standard step-2 asset table uses rows filtered by the active refrigeration subgroup when applicable.
    let standardRows = filterMachineExplorerRows(rows);
    if (machineExplorerSelectedGroup === "Refrigeration" && machineExplorerRefrigSubgroup
        && machineExplorerRefrigSubgroup !== "all" && machineExplorerRefrigSubgroup !== "condenser-evaporator") {
        standardRows = filterRowsByRefrigSubgroup(standardRows, machineExplorerRefrigSubgroup);
    }
    const assetRows = buildMachineExplorerAssetRows(standardRows);

    // Stub so the standard history panel can still show context for a refrig asset with no WO/MR records.
    const allAssetRows = [...assetRows];
    if (machineExplorerSelectedGroup === "Refrigeration" && machineExplorerSelectedAssetId
        && machineExplorerRefrigSubgroup !== "condenser-evaporator") {
        if (!assetRows.some((r) => r.assetId === machineExplorerSelectedAssetId)) {
            allAssetRows.push({
                assetId: machineExplorerSelectedAssetId,
                name: getRefrigAssetDisplayName(machineExplorerSelectedAssetId),
                criticality: "Non-Critical / Facility",
                machineGroup: "Refrigeration",
                rows: [],
            });
        }
    }

    if (machineExplorerSelectedAssetId && !allAssetRows.some((row) => row.assetId === machineExplorerSelectedAssetId)
        && machineExplorerRefrigSubgroup !== "condenser-evaporator") {
        machineExplorerSelectedAssetId = "";
    }
    renderMachineExplorerAssetSummary(assetRows);
    renderMachineExplorerHistory(rows, allAssetRows);
}

function renderDynamicsWorkOrderSections(rows = []) {
    renderDowntimeOverviewFromRows(rows);
    renderCriticalMrComparison(rows);
    renderMachineMrSection(rows);
    renderMachineExplorer(rows);
    populateFilters(getManagement());
    renderMachineGroupTable();
}

function scheduleAllYearWorkOrderRefresh(rows = []) {
    const token = ++allYearWorkOrderRenderToken;
    cancelLowPriorityWork(deferredAllYearWorkOrderHandle);
    cancelLowPriorityWork(deferredAllYearDynamicsHandle);
    deferredAllYearWorkOrderHandle = scheduleLowPriorityWork(() => {
        deferredAllYearWorkOrderHandle = null;
        if (token !== allYearWorkOrderRenderToken) return;
        renderWorkOrderResponseSection(rows);
        deferredAllYearDynamicsHandle = scheduleLowPriorityWork(() => {
            deferredAllYearDynamicsHandle = null;
            if (token !== allYearWorkOrderRenderToken) return;
            renderDynamicsWorkOrderSections(rows);
            renderDuplicateWoSection(rows);
        }, 1200);
    }, 900);
}

function requestMrMovementLoad() {
    renderMrMovementSection(allWorkOrderRowsCache);
    renderMrTrackingSection(allWorkOrderRowsCache);
    if (allWorkOrderRowsCache) {
        scheduleAllYearWorkOrderRefresh(allWorkOrderRowsCache);
        return;
    }
    renderDuplicateWoSection(null);
    if (allWorkOrderRowsPromise) return;
    loadAllWorkOrderRowsForMovement()
        .then((rows) => {
            syncMrMovementYearToPeriod(rows);
            renderMrMovementSection(rows);
            renderMrTrackingSection(rows);
            scheduleAllYearWorkOrderRefresh(rows);
        })
        .catch((error) => {
            console.error("MR movement load failed:", error);
            const fallbackRows = getFallbackAllWorkOrderRows();
            renderMrMovementSection(fallbackRows);
            renderMrTrackingSection(fallbackRows);
            scheduleAllYearWorkOrderRefresh(fallbackRows);
            setMrMovementWarning(`MR movement is using loaded dashboard rows because all-year data could not be loaded: ${error.message}`);
        });
}

function scheduleMrMovementLoad() {
    cancelLowPriorityWork(deferredMrMovementHandle);
    deferredMrMovementHandle = scheduleLowPriorityWork(() => {
        deferredMrMovementHandle = null;
        requestMrMovementLoad();
    }, 900);
}

function parseTimeToMinutes(value) {
    if (typeof value === "number" && Number.isFinite(value)) return value * 60;
    const match = String(value || "").trim().match(/^(\d{1,2}):(\d{2})$/);
    if (!match) return null;
    const hours = Number(match[1]);
    const minutes = Number(match[2]);
    if (hours < 0 || hours > 24 || minutes < 0 || minutes > 59 || (hours === 24 && minutes !== 0)) return null;
    return (hours * 60) + minutes;
}

function normalizeOperatingDay(value) {
    const dayLookup = {
        sunday: 0,
        sun: 0,
        monday: 1,
        mon: 1,
        tuesday: 2,
        tue: 2,
        wednesday: 3,
        wed: 3,
        thursday: 4,
        thu: 4,
        friday: 5,
        fri: 5,
        saturday: 6,
        sat: 6,
    };
    if (typeof value === "number" && value >= 0 && value <= 6) return value;
    const normalized = String(value || "").trim().toLowerCase();
    const numeric = Number(normalized);
    if (Number.isInteger(numeric) && numeric >= 0 && numeric <= 6) return numeric;
    return dayLookup[normalized] ?? null;
}

function normalizeOperatingWindows(config) {
    const rawWindows = Array.isArray(config)
        ? config
        : (config?.windows || config?.operating_windows || ((config?.start ?? config?.start_time ?? config?.from) !== undefined ? [config] : []));
    return rawWindows.map((operatingWindow) => {
        const startMinutes = parseTimeToMinutes(operatingWindow.start ?? operatingWindow.start_time ?? operatingWindow.from);
        const endMinutes = parseTimeToMinutes(operatingWindow.end ?? operatingWindow.end_time ?? operatingWindow.to);
        const rawDays = operatingWindow.days ?? operatingWindow.weekdays ?? operatingWindow.day ?? null;
        const days = rawDays === null
            ? [0, 1, 2, 3, 4, 5, 6]
            : (Array.isArray(rawDays) ? rawDays : [rawDays]).map(normalizeOperatingDay).filter((day) => day !== null);
        if (startMinutes === null || endMinutes === null || !days.length) return null;
        return { startMinutes, endMinutes, days };
    }).filter(Boolean);
}

function getOperatingHoursConfig(payload) {
    const config = payload?.operating_hours
        || payload?.config?.operating_hours
        || payload?.meta?.operating_hours
        || payload?.management?.operating_hours;
    if (!config || config.enabled === false) return OPERATING_HOURS_PLACEHOLDER;
    const windows = normalizeOperatingWindows(config);
    if (!windows.length) return OPERATING_HOURS_PLACEHOLDER;
    return {
        configured: true,
        source: config.source || "payload",
        windows,
    };
}

function addMinutesToDate(day, minutes) {
    return new Date(day.getFullYear(), day.getMonth(), day.getDate(), 0, minutes, 0, 0);
}

function calculateOperatingHoursOverlap(startValue, endValue, config) {
    if (!config?.configured) return null;
    const start = new Date(startValue);
    const end = new Date(endValue);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || end <= start) return null;

    let totalMs = 0;
    const cursor = new Date(start.getFullYear(), start.getMonth(), start.getDate());
    const lastDay = new Date(end.getFullYear(), end.getMonth(), end.getDate());

    while (cursor <= lastDay) {
        config.windows.forEach((operatingWindow) => {
            if (!operatingWindow.days.includes(cursor.getDay())) return;
            const windowStart = addMinutesToDate(cursor, operatingWindow.startMinutes);
            const windowEnd = operatingWindow.endMinutes > operatingWindow.startMinutes
                ? addMinutesToDate(cursor, operatingWindow.endMinutes)
                : addMinutesToDate(new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate() + 1), operatingWindow.endMinutes);
            const overlapStart = Math.max(start.getTime(), windowStart.getTime());
            const overlapEnd = Math.min(end.getTime(), windowEnd.getTime());
            if (overlapEnd > overlapStart) {
                totalMs += overlapEnd - overlapStart;
            }
        });
        cursor.setDate(cursor.getDate() + 1);
    }

    return totalMs / 3600000;
}

function buildMaintenanceImpactMetrics(management, summary = {}, downtimeSummary = {}, meta = {}) {
    const rows = getWorkOrderRows(management);
    const periodLabel = getPeriodLabel(meta);
    const workOrderRecordCount = Number(summary.total_work_orders ?? getWorkOrderRecordCount(summary, downtimeSummary, rows));
    const rowMetrics = rows.map((row) => ({ row, hours: getTtrHours(row) }));
    const validRecords = rowMetrics.filter((record) => record.hours !== null);
    const invalidRowCount = rowMetrics.filter((record) => record.hours === null).length;
    const invalidTtrCount = Number(summary.invalid_missing_ttr_count ?? (rows.length ? Math.max(invalidRowCount, workOrderRecordCount - validRecords.length, 0) : invalidRowCount));

    // Raw TTR is maintenance resolution time from work orders, not actual facility or production downtime.
    const summaryTotal = Number(summary.total_downtime_hours);
    const totalTtrHours = Number.isFinite(summaryTotal) && summaryTotal >= 0 ? summaryTotal : sumHours(validRecords);
    const averageFallbackCount = rows.length ? 0 : workOrderRecordCount;
    const validCountForAverage = validRecords.length || averageFallbackCount;
    const averageTtrHours = summary.overall_mttr_hours ?? (validCountForAverage ? totalTtrHours / validCountForAverage : null);
    const medianTtrHours = median(validRecords.map((record) => record.hours));
    const unclassifiedRecords = validRecords.filter((record) => !hasCriticality(record.row));
    const productionCriticalRecords = validRecords.filter((record) => isProductionCritical(record.row));
    const nonProductionRecords = validRecords.filter((record) => (
        hasCriticality(record.row) && !isProductionCritical(record.row) && isNonProductionClassification(record.row)
    ));
    const operatingHoursConfig = getOperatingHoursConfig(downtimePayload);

    let estimatedDowntimeHours = null;
    let estimatedRecordCount = 0;
    let missingTimeWindowCount = 0;
    const requiresAttentionCount = Number(summary.requires_attention_count || rows.filter((record) => record?.requires_attention).length || 0);
    const invalidWorkOrderCount = Math.max(0, requiresAttentionCount);
    const reliabilityInvalidCount = workOrderRecordCount > 0
        ? Math.min(invalidWorkOrderCount, workOrderRecordCount)
        : invalidWorkOrderCount;
    const dataReliabilityPercent = workOrderRecordCount > 0
        ? Math.max(0, Math.min(100, ((workOrderRecordCount - reliabilityInvalidCount) / workOrderRecordCount) * 100))
        : null;

    // Estimated downtime is only calculated after asset criticality and operating-hour overlap are available.
    if (operatingHoursConfig.configured && !unclassifiedRecords.length) {
        productionCriticalRecords.forEach((record) => {
            const overlapHours = calculateOperatingHoursOverlap(
                getWorkOrderStartTime(record.row),
                getWorkOrderEndTime(record.row),
                operatingHoursConfig
            );
            if (overlapHours === null) {
                missingTimeWindowCount += 1;
                return;
            }
            estimatedRecordCount += 1;
            estimatedDowntimeHours = (estimatedDowntimeHours || 0) + Math.min(overlapHours, record.hours);
        });
    }

    return {
        periodLabel,
        workOrderRecordCount,
        validTtrCount: validRecords.length,
        invalidTtrCount,
        requiresAttentionCount,
        invalidWorkOrderCount,
        dataReliabilityPercent,
        totalTtrHours,
        averageTtrHours,
        medianTtrHours,
        productionCriticalCount: productionCriticalRecords.length,
        nonProductionCount: nonProductionRecords.length,
        nonProductionHours: sumHours(nonProductionRecords),
        unclassifiedCount: unclassifiedRecords.length,
        unclassifiedHours: sumHours(unclassifiedRecords),
        operatingHoursConfigured: operatingHoursConfig.configured,
        estimatedDowntimeHours,
        estimatedRecordCount,
        missingTimeWindowCount,
    };
}

function setCardVisible(id, visible) {
    const el = document.getElementById(id);
    if (el) el.classList.toggle("hidden", !visible);
}

function setImportStatus(message, state = "") {
    const el = document.getElementById("work-order-import-status");
    if (!el) return;
    el.textContent = message || "";
    el.className = `import-status ${state}`.trim();
    el.classList.toggle("hidden", !message);
}

async function loadWorkOrderImportStatus() {
    try {
        const response = await fetch("/api/downtime/import-work-orders");
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const sources = payload?.sources || [];
        if (!payload?.using_uploaded_imports) {
            setImportStatus("Downtime work order import not loaded. Spare-parts linking and imported work-order analysis stay limited until a WO export is imported.", "error");
            return;
        }
        const latest = sources[0]?.name || "uploaded work order source";
        setImportStatus(`Using ${latest}${payload.source_count ? ` | ${payload.source_count} source file${payload.source_count === 1 ? "" : "s"}` : ""}`, "ok");
    } catch (error) {
        console.error("Work order import status load failed:", error);
        setImportStatus("Work order import status unavailable.", "error");
    }
}

function fmtDateTime(value) {
    if (!value) return "--";
    const dt = new Date(value);
    if (Number.isNaN(dt.getTime())) return "--";
    return dt.toLocaleString("en-GB", {
        day: "2-digit",
        month: "short",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
    });
}

function fmtDateOnly(value) {
    if (!value) return "";
    const dt = new Date(value);
    if (Number.isNaN(dt.getTime())) return "";
    return dt.toLocaleDateString("en-GB", {
        day: "2-digit",
        month: "short",
        year: "numeric",
    });
}

function getIsoDate(value) {
    if (!value) return "";
    const dt = new Date(value);
    if (Number.isNaN(dt.getTime())) return "";
    return dt.toISOString().slice(0, 10);
}

function buildStatusPill(status, label) {
    const normalized = String(status || "ok").toLowerCase();
    return `<span class="status-pill ${escapeHtml(normalized)}">${escapeHtml(label || normalized)}</span>`;
}

function renderReliabilityBadges(row, mtbfRow) {
    const badges = Array.isArray(row.reliability_badges) && row.reliability_badges.length
        ? [...row.reliability_badges]
        : [{ level: row.status_flag || "stable", label: row.status_flag === "critical" ? "CRITICAL" : (row.status_flag === "warning" ? "WARNING" : "STABLE") }];
    if (mtbfRow?.average_mtbf_hours && Number(mtbfRow.average_mtbf_hours) < 168 && !badges.some((badge) => badge.label === "CRITICAL")) {
        badges.unshift({ level: "critical", label: "CRITICAL" });
    }
    return badges.map((badge) => buildStatusPill(badge.level, badge.label)).join(" ");
}

function fmtRecordCount(count) {
    const n = Number(count || 0);
    return `${fmtNumber(n)} WO${n === 1 ? "" : "s"}`;
}

function uniqReasons(reasons) {
    return [...new Set((reasons || []).map((reason) => String(reason || "").trim()).filter(Boolean))];
}

function getCalculationFlags(row, mtbfRow) {
    const mttrReasons = uniqReasons(row?.mttr_missing_reasons);
    const mtbfReasons = uniqReasons(row?.mtbf_missing_reasons);

    if (!row?.mttr_hours && !mttrReasons.length) {
        mttrReasons.push("No valid TTR records");
    }
    if (!mtbfRow?.average_mtbf_hours && !mtbfReasons.length) {
        mtbfReasons.push("Insufficient history: fewer than 2 valid completed work orders for the same asset");
    }

    return [
        {
            type: "mttr",
            label: "MTTR missing",
            count: Number(row?.mttr_missing_count || row?.invalid_ttr_count || 0),
            reasons: mttrReasons,
        },
        {
            type: "mtbf",
            label: "MTBF missing",
            count: Number(row?.mtbf_missing_count || 0),
            reasons: mtbfReasons,
        },
    ].filter((flag) => flag.reasons.length);
}

function getRecordCalculationFlags(row) {
    const derivedTtrAvailable = getTtrHours(row) !== null;
    const mttrReasons = derivedTtrAvailable
        ? uniqReasons(row?.mttr_missing_reasons).filter((reason) => !/missing|invalid|zero ttr/i.test(reason))
        : uniqReasons(row?.mttr_missing_reasons);
    return [
        {
            type: "mttr",
            label: "MTTR excluded",
            count: 0,
            reasons: mttrReasons,
        },
        {
            type: "mtbf",
            label: "MTBF excluded",
            count: 0,
            reasons: uniqReasons(row?.mtbf_missing_reasons),
        },
    ].filter((flag) => flag.reasons.length);
}

function renderCalculationFlags(flags) {
    if (!flags.length) return "";
    return `<div class="calc-flag-list">${flags.map((flag) => {
        const countText = flag.count > 0 ? ` (${fmtRecordCount(flag.count)})` : "";
        return `
            <div class="calc-flag ${escapeHtml(flag.type)}">
                <span class="calc-flag-label">${escapeHtml(flag.label)}${escapeHtml(countText)}</span>
                <span class="calc-flag-reason">${escapeHtml(flag.reasons.join(", "))}</span>
            </div>
        `;
    }).join("")}</div>`;
}

function populateSelect(id, values, defaultLabel) {
    const select = document.getElementById(id);
    if (!select) return;
    const current = select.value;
    select.innerHTML = `<option value="">${escapeHtml(defaultLabel)}</option>` + values.map((value) => (
        `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`
    )).join("");
    if (values.includes(current)) {
        select.value = current;
    }
}

function destroyChart(id) {
    if (chartRefs[id]) {
        chartRefs[id].destroy();
        delete chartRefs[id];
    }
}

function renderEmptyChart(canvasId, message) {
    const canvas = document.getElementById(canvasId);
    const container = canvas?.parentElement || document.querySelector(`.chart-container[data-chart-id="${canvasId}"]`);
    if (!container) return;
    destroyChart(canvasId);
    container.innerHTML = `<div class="empty-state">${escapeHtml(message)}</div>`;
}

function ensureCanvas(canvasId) {
    const existing = document.getElementById(canvasId);
    if (existing) return existing;
    const target = document.querySelector(`.chart-container[data-chart-id="${canvasId}"]`);
    if (!target) return null;
    target.innerHTML = `<canvas id="${canvasId}"></canvas>`;
    return document.getElementById(canvasId);
}

function setChartContainerHeight(canvasId, heightPx) {
    const container = document.querySelector(`.chart-container[data-chart-id="${canvasId}"]`);
    if (!container) return;
    container.style.height = `${Math.max(260, Number(heightPx) || 260)}px`;
}

async function loadDowntimeCacheFile() {
    try {
        const response = await fetch(`./downtime-cache.json?v=20260504-binary-criticality&_=${Date.now()}`, {
            cache: "no-store",
        });
        if (!response.ok) {
            downtimeCachePayload = false;
            return null;
        }
        downtimeCachePayload = await response.json();
        return downtimeCachePayload;
    } catch (error) {
        console.warn("Downtime cache load failed:", error);
        downtimeCachePayload = false;
        return null;
    }
}

function getCachedDowntimePayload(period, month, start, end) {
    if (getSelectedDowntimeStage() !== DOWNTIME_STAGE_ALL) return null;
    const payloads = downtimeCachePayload?.payloads || {};
    if (period === "custom" && start && end) return null;
    const key = period === "this_month" && month ? `this_month:${month}` : period;
    return payloads[key] || null;
}

async function loadDowntimeData(period, month, start, end) {
    let payload = null;
    let liveError = null;
    try {
        const params = { period, work_orders_only: "1" };
        if (month) params.month = month;
        if (start) params.start = start;
        if (end) params.end = end;
        const url = buildDowntimeApiUrl(params);
        const response = await fetch(url, { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        payload = await response.json();
    } catch (error) {
        liveError = error;
    }

    if (!payload || !payload.management) {
        if (downtimeCachePayload === null) {
            await loadDowntimeCacheFile();
        }
        payload = getCachedDowntimePayload(period, month, start, end);
        if (!payload || !payload.management) {
            throw liveError || new Error("No downtime payload available");
        }
        console.warn("Using cached downtime payload because the live API was unavailable:", liveError);
    }

    downtimePayload = payload;
    applySlaTargetConfig(downtimePayload);
    renderDowntimePage();
    lastDowntimeRefreshAt = Date.now();
}

function getManagement() {
    return downtimePayload?.management || {
        summary: {},
        mtbf: {
            summary: {},
            criticality_rows: [],
            machine_group_rows: [],
            asset_rows: [],
            trend: { labels: [], mtbf_hours: [], pair_counts: [] },
        },
        criticality_rows: [],
        machine_group_rows: [],
        location_rows: [],
        trend: { labels: [], downtime_hours: [], work_order_counts: [] },
        work_orders: [],
        filters: { criticalities: [], machine_groups: [], locations: [], asset_ids: [], statuses: [] },
        alerts: [],
        mapping_meta: {},
    };
}

function getMtbfData() {
    return getManagement().mtbf || {
        summary: {},
        criticality_rows: [],
        machine_group_rows: [],
        asset_rows: [],
        trend: { labels: [], mtbf_hours: [], pair_counts: [] },
    };
}

function getSelectedMtbfPeriod() {
    return {
        year: document.getElementById("mtbf-year-filter")?.value || "",
        month: document.getElementById("mtbf-month-filter")?.value || "",
    };
}

function getSelectedMtbfTrendCriticality() {
    return document.getElementById("mtbf-trend-criticality-filter")?.value || "";
}

function getSelectedMtbfTrendCompareMode() {
    return document.getElementById("mtbf-trend-compare-mode")?.value || "";
}

function getAssetMetaFromLookup(assetLookup, assetId) {
    const id = String(assetId || "").trim();
    if (!id) return null;
    return assetLookup.get(id) || assetLookup.get(id.toUpperCase()) || null;
}

function getMtbfRecordCriticality(record, assetLookup) {
    const meta = getAssetMetaFromLookup(assetLookup, record?.asset_id);
    const raw = String(meta?.criticality || record?.criticality || record?.normalized_criticality || record?.raw_criticality || "").trim();
    // Check asset-master criticality first; fall back to machine-group name.
    if (raw && raw !== "Non-Critical / Facility" && raw !== "Non-Critical") {
        return PRODUCTION_CRITICAL_LABELS.has(normalizeClassification(raw)) ? "Critical" : "Non-Critical / Facility";
    }
    const machineGroup = String(meta?.machine_name || record?.machine_group || "").trim();
    if (CRITICAL_MACHINE_GROUPS.has(machineGroup)) return "Critical";
    if (!raw) return "";
    return "Non-Critical / Facility";
}

function matchesMtbfCriticalityFilter(record, criticalityFilter, assetLookup) {
    if (!criticalityFilter) return true;
    return getMtbfRecordCriticality(record, assetLookup) === criticalityFilter;
}

function getSelectedMtbfDateRange() {
    const { year, month } = getSelectedMtbfPeriod();
    if (month) {
        const [yearValue, monthValue] = month.split("-").map((value) => Number(value));
        if (yearValue && monthValue) {
            return {
                start: new Date(yearValue, monthValue - 1, 1, 0, 0, 0, 0),
                end: new Date(yearValue, monthValue, 0, 23, 59, 59, 999),
            };
        }
    }
    if (year) {
        const numericYear = Number(year);
        if (numericYear) {
            return {
                start: new Date(numericYear, 0, 1, 0, 0, 0, 0),
                end: new Date(numericYear, 11, 31, 23, 59, 59, 999),
            };
        }
    }

    const meta = downtimePayload?.meta || {};
    const start = parseDateValue(meta.period_start);
    const end = parseDateValue(meta.period_end || meta.reference_end);
    return start && end ? { start, end } : null;
}

function getAllWorkOrdersForMtbf() {
    if (mtbfHistoryPayload?.work_orders?.length) return applyCategoryFilter(mtbfHistoryPayload.work_orders);
    return getWorkOrderRows(getManagement());
}

async function loadMtbfHistory() {
    if (mtbfHistoryPayload) return mtbfHistoryPayload;
    if (mtbfHistoryPromise) return mtbfHistoryPromise;
    const gen = _stageFetchGeneration;
    mtbfHistoryPromise = fetch(buildDowntimeMtbfHistoryUrl(), { cache: "no-store" })
        .then((response) => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.json();
        })
        .then((payload) => {
            if (_stageFetchGeneration !== gen) return null;
            mtbfHistoryPayload = payload;
            return payload;
        })
        .catch((error) => {
            console.warn("MTBF history load failed:", error);
            return null;
        })
        .finally(() => {
            mtbfHistoryPromise = null;
        });
    return mtbfHistoryPromise;
}

function requestMtbfHistoryLoad() {
    if (mtbfHistoryPayload || mtbfHistoryPromise) return;
    loadMtbfHistory()
        .then((payload) => {
            if (!payload) return;
            try { populateMtbfPeriodFilters(); } catch (e) { console.warn("[MTBF] period filters:", e); }
            try { renderMtbfSection(); } catch (e) { console.warn("[MTBF] section render:", e); }
            updateKdiSection();
        })
        .catch((error) => {
            console.warn("MTBF history load failed:", error);
        });
}

function populateMtbfPeriodFilters() {
    const wos = getAllWorkOrdersForMtbf();
    const yearSet = new Set();
    const monthSet = new Set();
    (mtbfHistoryPayload?.years || []).forEach((year) => yearSet.add(String(year)));
    (mtbfHistoryPayload?.months || []).forEach((month) => monthSet.add(String(month)));
    (getManagement().historical_trend || []).forEach((row) => {
        if (row.year) yearSet.add(String(row.year));
    });
    wos.forEach((wo) => {
        const t = wo.start_time || wo.actual_start_time || "";
        if (t.length >= 7) {
            yearSet.add(t.slice(0, 4));
            monthSet.add(t.slice(0, 7));
        }
    });
    const years = [...yearSet].sort((a, b) => b.localeCompare(a));
    const months = [...monthSet].sort((a, b) => b.localeCompare(a));

    const yearSel = document.getElementById("mtbf-year-filter");
    const monthSel = document.getElementById("mtbf-month-filter");
    if (!yearSel || !monthSel) return;

    const prevYear = yearSel.value;
    const prevMonth = monthSel.value;

    yearSel.innerHTML = `<option value="">All Years</option>` +
        years.map((y) => `<option value="${y}">${y}</option>`).join("");
    if (years.includes(prevYear)) yearSel.value = prevYear;

    const selectedYear = yearSel.value;
    const filteredMonths = selectedYear ? months.filter((m) => m.startsWith(selectedYear)) : months;
    monthSel.innerHTML = `<option value="">All Months</option>` +
        filteredMonths.map((m) => {
            const dt = new Date(`${m}-01`);
            const label = dt.toLocaleDateString("en-GB", { month: "long", year: "numeric" });
            return `<option value="${m}">${label}</option>`;
        }).join("");
    if (filteredMonths.includes(prevMonth)) monthSel.value = prevMonth;

    populateMtbfTrendCompareOptions(years, months);
}

function formatMtbfMonthOptionLabel(monthKey) {
    const dt = new Date(`${monthKey}-01`);
    if (Number.isNaN(dt.getTime())) return monthKey;
    return dt.toLocaleDateString("en-GB", { month: "long", year: "numeric" });
}

function populateMtbfTrendCompareSelect(select, values, labelFormatter, fallbackValue) {
    if (!select) return "";
    const previous = select.value;
    select.innerHTML = values.map((value) => (
        `<option value="${escapeHtml(value)}">${escapeHtml(labelFormatter(value))}</option>`
    )).join("");
    if (values.includes(previous)) {
        select.value = previous;
    } else if (fallbackValue && values.includes(fallbackValue)) {
        select.value = fallbackValue;
    } else if (values.length) {
        select.value = values[0];
    }
    return select.value;
}

function populateMtbfTrendCompareOptions(years, months) {
    const mode = getSelectedMtbfTrendCompareMode();
    syncMtbfTrendModeLabels();
    const wrapA = document.getElementById("mtbf-compare-a-wrap");
    const wrapB = document.getElementById("mtbf-compare-b-wrap");
    const selectA = document.getElementById("mtbf-trend-compare-a");
    const selectB = document.getElementById("mtbf-trend-compare-b");
    const compareEnabled = mode === "years" || mode === "months";
    [wrapA, wrapB].forEach((wrap) => wrap?.classList.toggle("hidden", !compareEnabled));
    if (!compareEnabled) return;

    const values = mode === "months" ? months : years;
    const labelFormatter = mode === "months" ? formatMtbfMonthOptionLabel : (value) => value;
    const firstValue = values[0] || "";
    const secondValue = values.find((value) => value !== firstValue) || firstValue;
    const selectedA = populateMtbfTrendCompareSelect(selectA, values, labelFormatter, firstValue);
    const fallbackB = values.find((value) => value !== selectedA) || secondValue;
    populateMtbfTrendCompareSelect(selectB, values, labelFormatter, fallbackB);
    if (selectA && selectB && selectA.value === selectB.value) {
        const nextDifferent = values.find((value) => value !== selectA.value);
        if (nextDifferent) selectB.value = nextDifferent;
    }
}

function syncMtbfTrendModeLabels() {
    const monthlyButton = document.querySelector('#mtbf-trend-mode-toggle [data-mode="monthly"]');
    if (monthlyButton) {
        monthlyButton.textContent = getSelectedMtbfTrendCompareMode() === "months" ? "Daily" : "Monthly";
    }
}

function computeMtbfFromWorkOrders(wos) {
    // Per-asset MTBF — strict end-to-start only (no start-to-start fallback).
    const byAsset = new Map();
    wos.forEach((wo) => {
        const id = String(wo.asset_id || "").trim();
        if (!id || id.toUpperCase() === "WO-ASSET" || id === "--") return;
        if (isMtbfGeneralAreaWo(wo)) return;            // skip area/location placeholders
        if (!byAsset.has(id)) byAsset.set(id, []);
        byAsset.get(id).push(wo);
    });

    const assetLookup = buildAssetListLookup();
    const results = [];

    byAsset.forEach((assetWos, assetId) => {
        // Sort by actual start date
        const sorted = [...assetWos].sort((a, b) => {
            const ta = getMtbfWorkOrderStart(a);
            const tb = getMtbfWorkOrderStart(b);
            return (ta ? ta.getTime() : 0) - (tb ? tb.getTime() : 0);
        });

        const gaps = [];

        for (let i = 1; i < sorted.length; i++) {
            const prev = sorted[i - 1];
            const next = sorted[i];
            const prevEnd = getMtbfWorkOrderEnd(prev);      // strict — null if no actual end
            const nextStart = getMtbfWorkOrderStart(next);
            if (!prevEnd || !nextStart) continue;

            const gapMs    = nextStart.getTime() - prevEnd.getTime();
            const gapHours = gapMs / 3600000;
            if (gapHours <= MTBF_MIN_GAP_HOURS) continue;
            gaps.push(gapHours);
        }

        const avgMtbf = gaps.length > 0 ? gaps.reduce((a, b) => a + b, 0) / gaps.length : null;
        const meta = assetLookup.get(assetId) || {};
        results.push({
            asset_id: assetId,
            machine_group: meta.machine_name || assetWos[0]?.machine_group || assetId,
            criticality: meta.criticality || assetWos[0]?.criticality || "",
            average_mtbf_hours: avgMtbf,
            work_order_count: sorted.length,
            valid_mtbf_gap_count: gaps.length,
            excluded_gap_count: sorted.length > 1 ? (sorted.length - 1 - gaps.length) : 0,
        });
    });

    return results;
}

// MTBF constants
const MTBF_MIN_GAP_HOURS       = 1 / 60; // absolute floor — gaps <= 1 min are excluded (effectively zero)

// General-area pattern: "Production High Risk", "Work Area", etc. are
// area/location records, not specific machines — MTBF between them is meaningless.
const MTBF_GENERAL_AREA_RE = /\b(risk\s*area|work\s*area|high\s+risk|low\s+risk|medium\s+risk|general\s+area|production\s+area|low\s+risk\s+area|high\s+risk\s+area)\b/i;

function isMtbfGeneralAreaWo(wo) {
    const name = [wo.machine_group, wo.asset_display_name, wo.machine_name, wo.raw_functional_location, wo.location]
        .filter(Boolean).join(" ");
    return MTBF_GENERAL_AREA_RE.test(name);
}

function getMtbfWorkOrderStart(wo) {
    return parseDateValue(wo.actual_start_time || wo.maintenance_start_time || wo.start_time);
}

// STRICT: no fallback to start_time — end must be an actual recorded end date.
function getMtbfWorkOrderEnd(wo) {
    return parseDateValue(wo.actual_end_time || wo.maintenance_end_time || wo.end_time);
}

function getMtbfTrendBucketMode(range, pointDates) {
    const start = range?.start || pointDates[0] || null;
    const end = range?.end || pointDates[pointDates.length - 1] || null;
    return start && end ? "week" : "day";
}

function getMtbfTrendBucketStart(date, bucketMode) {
    if (bucketMode === "month") {
        return new Date(date.getFullYear(), date.getMonth(), 1);
    }
    if (bucketMode === "week") {
        const bucket = new Date(date.getFullYear(), date.getMonth(), date.getDate());
        const mondayOffset = (bucket.getDay() + 6) % 7;
        bucket.setDate(bucket.getDate() - mondayOffset);
        return bucket;
    }
    return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function collectMtbfGapPoints(wos, range, criticalityFilter = "") {
    const byAsset = new Map();
    const seenWorkOrderIds = new Set();
    const assetLookup = buildAssetListLookup();

    (wos || []).forEach((wo) => {
        const workOrderId = String(wo.work_order_id || "").trim();
        if (workOrderId) {
            if (seenWorkOrderIds.has(workOrderId)) return;
            seenWorkOrderIds.add(workOrderId);
        }
        if (!matchesMtbfCriticalityFilter(wo, criticalityFilter, assetLookup)) return;
        const assetId = String(wo.asset_id || "").trim();
        if (!assetId || assetId.toUpperCase() === "WO-ASSET") return;
        const start = getMtbfWorkOrderStart(wo);
        const end = getMtbfWorkOrderEnd(wo);
        if (!start || !end || end <= start) return;
        if (!byAsset.has(assetId)) byAsset.set(assetId, []);
        byAsset.get(assetId).push({ ...wo, _start: start, _end: end });
    });

    const points = [];
    byAsset.forEach((assetWos) => {
        const sorted = [...assetWos].sort((a, b) => a._start - b._start);
        for (let i = 1; i < sorted.length; i++) {
            const previous = sorted[i - 1];
            const next = sorted[i];
            const gapHours = (next._start - previous._end) / 3600000;
            if (gapHours <= 0) continue;
            if (range?.start && next._start < range.start) continue;
            if (range?.end && next._start > range.end) continue;
            points.push({ timestamp: next._start, gap_hours: gapHours });
        }
    });

    points.sort((a, b) => a.timestamp - b.timestamp);
    return points;
}

function buildMtbfTrendFromPoints(points, range, bucketMode) {
    if (!points.length) {
        return { labels: [], mtbf_hours: [], pair_counts: [], bucket_keys: [], bucket_mode: "day" };
    }

    const buckets = new Map();
    points.forEach((point) => {
        const bucketStart = getMtbfTrendBucketStart(point.timestamp, bucketMode);
        const bucketKey = bucketMode === "month" ? formatMonthKey(bucketStart) : formatDateKey(bucketStart);
        if (!buckets.has(bucketKey)) {
            buckets.set(bucketKey, { date: bucketStart, hours: 0, count: 0 });
        }
        const bucket = buckets.get(bucketKey);
        bucket.hours += point.gap_hours;
        bucket.count += 1;
    });

    const rows = [...buckets.values()].sort((a, b) => a.date - b.date);
    return {
        labels: rows.map((row) => formatMtbfBucketLabel(row.date, bucketMode)),
        mtbf_hours: rows.map((row) => Math.round((row.hours / row.count) * 1000) / 1000),
        pair_counts: rows.map((row) => row.count),
        bucket_keys: rows.map((row) => (bucketMode === "month" ? formatMonthKey(row.date) : formatDateKey(row.date))),
        bucket_mode: bucketMode,
    };
}

function buildMtbfTrendFromWorkOrders(wos, range, criticalityFilter = "") {
    // MTBF trend points are repeat-failure gaps from one completed WO to the next WO start for the same asset.
    const points = collectMtbfGapPoints(wos, range, criticalityFilter);
    const bucketMode = getMtbfTrendBucketMode(range, points.map((point) => point.timestamp));
    return buildMtbfTrendFromPoints(points, range, bucketMode);
}

function getMtbfAssetRowsForPeriod() {
    const { year, month } = getSelectedMtbfPeriod();
    // No filter → use cached global MTBF (most accurate with full history)
    if (!year && !month) return getMtbfData().asset_rows || [];
    // With filter → compute from work orders for that period
    const allWos = getAllWorkOrdersForMtbf();
    const filtered = allWos.filter((wo) => {
        const t = wo.start_time || wo.actual_start_time || "";
        if (month) return t.startsWith(month);
        if (year) return t.startsWith(year);
        return true;
    });
    return computeMtbfFromWorkOrders(filtered);
}

function filterMtbfTrend(trend) {
    const range = getSelectedMtbfDateRange();
    const criticalityFilter = getSelectedMtbfTrendCriticality();
    const allWos = getAllWorkOrdersForMtbf();
    if (allWos.length) {
        return buildMtbfTrendFromWorkOrders(allWos, range, criticalityFilter);
    }
    if (!range || !trend?.labels?.length || criticalityFilter) {
        return criticalityFilter
            ? { labels: [], mtbf_hours: [], pair_counts: [], bucket_keys: [], bucket_mode: "day" }
            : trend;
    }

    const indices = (trend.bucket_keys || []).reduce((acc, key, i) => {
        const date = parseDateValue(key.length === 7 ? `${key}-01` : key);
        if (!date) return acc;
        if (range.start && date < range.start) return acc;
        if (range.end && date > range.end) return acc;
        acc.push(i);
        return acc;
    }, []);
    if (!indices.length) return { labels: [], mtbf_hours: [], pair_counts: [], bucket_keys: [], bucket_mode: trend.bucket_mode || "day" };
    return {
        labels: indices.map((i) => trend.labels[i]),
        mtbf_hours: indices.map((i) => trend.mtbf_hours[i]),
        pair_counts: indices.map((i) => (trend.pair_counts || [])[i] || 0),
        bucket_keys: indices.map((i) => (trend.bucket_keys || [])[i]).filter(Boolean),
        bucket_mode: trend.bucket_mode,
    };
}

function getMtbfTrendChartMode() {
    return document.getElementById("mtbf-trend-mode-toggle")?.querySelector(".chart-toggle-btn.active")?.dataset.mode || "weekly";
}

function getMtbfTrendCompareSettings() {
    const mode = getSelectedMtbfTrendCompareMode();
    if (mode !== "years" && mode !== "months") return null;
    const valueA = document.getElementById("mtbf-trend-compare-a")?.value || "";
    const valueB = document.getElementById("mtbf-trend-compare-b")?.value || "";
    return {
        mode,
        values: [valueA, valueB].filter(Boolean),
        chartMode: getMtbfTrendChartMode(),
        criticalityFilter: getSelectedMtbfTrendCriticality(),
    };
}

function getMtbfComparisonRange(mode, value) {
    if (mode === "years") {
        const year = Number(value);
        if (!year) return null;
        return {
            start: new Date(year, 0, 1, 0, 0, 0, 0),
            end: new Date(year, 11, 31, 23, 59, 59, 999),
        };
    }
    if (mode === "months") {
        const [year, month] = String(value || "").split("-").map((part) => Number(part));
        if (!year || !month) return null;
        return {
            start: new Date(year, month - 1, 1, 0, 0, 0, 0),
            end: new Date(year, month, 0, 23, 59, 59, 999),
        };
    }
    return null;
}

function getMtbfComparisonSeriesLabel(mode, value) {
    return mode === "months" ? formatMtbfMonthOptionLabel(value) : String(value || "");
}

function getMtbfComparisonBucketDefs(mode, chartMode, values) {
    if (mode === "years" && chartMode === "monthly") {
        return Array.from({ length: 12 }, (_, i) => ({
            key: String(i + 1),
            label: new Date(2026, i, 1).toLocaleDateString("en-GB", { month: "short" }),
        }));
    }
    if (mode === "years") {
        return Array.from({ length: 53 }, (_, i) => ({ key: String(i + 1), label: `Week ${i + 1}` }));
    }
    if (mode === "months" && chartMode === "monthly") {
        const maxDays = Math.max(...values.map((value) => {
            const [year, month] = String(value || "").split("-").map((part) => Number(part));
            return year && month ? new Date(year, month, 0).getDate() : 0;
        }), 0);
        return Array.from({ length: maxDays }, (_, i) => ({ key: String(i + 1), label: `Day ${i + 1}` }));
    }
    return Array.from({ length: 6 }, (_, i) => ({ key: String(i + 1), label: `Week ${i + 1}` }));
}

function getMtbfComparisonBucketKey(date, range, mode, chartMode) {
    if (!date || !range) return "";
    if (mode === "years" && chartMode === "monthly") return String(date.getMonth() + 1);
    if (mode === "months" && chartMode === "monthly") return String(date.getDate());
    if (mode === "months") return String(Math.floor((date.getDate() - 1) / 7) + 1);
    const weekIndex = Math.floor((date.getTime() - range.start.getTime()) / 604800000) + 1;
    return weekIndex > 0 ? String(weekIndex) : "";
}

function buildMtbfTrendComparisonData() {
    const settings = getMtbfTrendCompareSettings();
    if (!settings || settings.values.length < 2 || settings.values[0] === settings.values[1]) return null;
    const allWos = getAllWorkOrdersForMtbf();
    if (!allWos.length) return null;

    const bucketDefs = getMtbfComparisonBucketDefs(settings.mode, settings.chartMode, settings.values);
    const colors = [
        { border: "#0f766e", fill: "rgba(15, 118, 110, 0.18)" },
        { border: "#2563eb", fill: "rgba(37, 99, 235, 0.14)" },
    ];
    const datasets = settings.values.map((value, index) => {
        const range = getMtbfComparisonRange(settings.mode, value);
        const points = range ? collectMtbfGapPoints(allWos, range, settings.criticalityFilter) : [];
        const buckets = new Map();
        points.forEach((point) => {
            const key = getMtbfComparisonBucketKey(point.timestamp, range, settings.mode, settings.chartMode);
            if (!key) return;
            if (!buckets.has(key)) buckets.set(key, { hours: 0, count: 0 });
            const bucket = buckets.get(key);
            bucket.hours += point.gap_hours;
            bucket.count += 1;
        });
        const color = colors[index % colors.length];
        return {
            label: getMtbfComparisonSeriesLabel(settings.mode, value),
            data: bucketDefs.map((bucket) => {
                const row = buckets.get(bucket.key);
                // Use 0 instead of null so the line stays connected through empty days
                return row?.count ? Math.round((row.hours / row.count) * 1000) / 1000 : 0;
            }),
            _counts: bucketDefs.map((bucket) => buckets.get(bucket.key)?.count || 0),
            borderColor: color.border,
            backgroundColor: color.fill,
            fill: "origin",
            tension: 0.28,
            borderWidth: 2.5,
            pointRadius: (ctx) => (ctx.dataset.data[ctx.dataIndex] === 0 ? 2 : 4),
            pointBackgroundColor: color.border,
        };
    });

    return {
        labels: bucketDefs.map((bucket) => bucket.label),
        datasets,
        hasData: datasets.some((dataset) => dataset._counts.some((count) => count > 0)),
    };
}

function renderMtbfSection() {
    const mtbf = getMtbfData();
    renderMtbfSummary(mtbf.summary || {});
    renderMtbfCriticalityCards(mtbf.criticality_rows || []);
    renderMtbfCriticalMachinesChart(getMtbfAssetRowsForPeriod());
    renderMtbfTrendChart(filterMtbfTrend(mtbf.trend || {}));
}

function toggleCustomDateFilter(period) {
    const wrap = document.getElementById("custom-date-wrap");
    if (!wrap) return;
    wrap.style.display = period === "custom" ? "flex" : "none";
}

function renderAlerts(alerts) {
    const banner = document.getElementById("alert-banner");
    const items = document.getElementById("alert-items");
    if (!banner || !items) return;

    if (!alerts || !alerts.length) {
        banner.classList.add("hidden");
        items.innerHTML = "";
        return;
    }

    items.innerHTML = alerts.map((alert) => (
        `<div class="alert-item ${escapeHtml(alert.level || "warning")}">${escapeHtml(alert.message)}</div>`
    )).join("");
    banner.classList.remove("hidden");
}

function renderSummary(summary, downtimeSummary = {}, management = getManagement(), meta = {}) {
    const metrics = buildMaintenanceImpactMetrics(management, summary, downtimeSummary, meta);
    const validText = `${fmtNumber(metrics.validTtrCount)} valid TTR record${metrics.validTtrCount === 1 ? "" : "s"}`;
    renderCurrentDowntimeKpi();

    setText("kpi-average-ttr", metrics.averageTtrHours === null ? "No valid records" : fmtHours(metrics.averageTtrHours));
    setText("kpi-average-ttr-sub", validText);

    setText("kpi-median-ttr", metrics.medianTtrHours === null ? "No valid records" : fmtHours(metrics.medianTtrHours));
    setText("kpi-median-ttr-sub", validText);

    if (metrics.unclassifiedCount > 0) {
        setText("kpi-estimated-downtime", "Classification required");
        setText("kpi-estimated-downtime-sub", `${fmtNumber(metrics.unclassifiedCount)} work order(s) need an Asset Master mapping before operating-hour overlap can be trusted`);
        setText("kpi-production-impact", "Classification required");
        setText("kpi-production-impact-sub", "Estimated production impact is held until missing Asset Master matches are resolved");
    } else if (!metrics.operatingHoursConfigured) {
        setText("kpi-estimated-downtime", "Insufficient data");
        setText("kpi-estimated-downtime-sub", "Operating hours config required before overlap can be calculated");
        setText("kpi-production-impact", "Insufficient data");
        setText("kpi-production-impact-sub", "Estimated until actual production stop data or operating hours are connected");
    } else if (metrics.productionCriticalCount > 0 && metrics.estimatedRecordCount === 0) {
        setText("kpi-estimated-downtime", "Insufficient data");
        setText("kpi-estimated-downtime-sub", "Start and end times are required for production-critical work orders");
        setText("kpi-production-impact", "Insufficient data");
        setText("kpi-production-impact-sub", "No production-critical records had usable time windows");
    } else {
        const estimateText = fmtHoursIfAvailable(metrics.estimatedDowntimeHours || 0, true);
        setText("kpi-estimated-downtime", estimateText);
        setText(
            "kpi-estimated-downtime-sub",
            `${fmtNumber(metrics.estimatedRecordCount)} critical work order(s) overlapped with operating hours${metrics.missingTimeWindowCount ? `; ${fmtNumber(metrics.missingTimeWindowCount)} missing time windows` : ""}`
        );
        // Production impact is estimated until actual downtime or production-stop data is available.
        setText("kpi-production-impact", estimateText);
        setText("kpi-production-impact-sub", "Estimated from production-critical classification and operating-hour overlap");
    }

    setText("kpi-non-production-time", metrics.nonProductionCount ? fmtHours(metrics.nonProductionHours) : "No matching records");
    setText("kpi-non-production-time-sub", `${fmtNumber(metrics.nonProductionCount)} non-critical/facility work order(s) with valid TTR`);

    setText("kpi-data-reliability", fmtPercent(metrics.dataReliabilityPercent));
    setText("kpi-invalid-work-orders", fmtNumber(metrics.invalidWorkOrderCount));
    setText(
        "kpi-data-reliability-sub",
        metrics.workOrderRecordCount
            ? `${fmtNumber(metrics.invalidWorkOrderCount)} of ${fmtNumber(metrics.workOrderRecordCount)} selected-period work order(s) require review.`
            : "No selected-period work orders available for reliability scoring."
    );
    renderDowntimeOverviewFromRows(getOverviewSourceRows(management));

}

function renderCriticalityCards(rows) {
    const container = document.getElementById("criticality-cards");
    if (!container) return;

    if (!rows || !rows.length) {
        selectedMttrCriticality = "";
        container.innerHTML = `<div class="empty-state compact">No criticality data available</div>`;
        return;
    }

    if (selectedMttrCriticality && !rows.some((row) => row.criticality === selectedMttrCriticality)) {
        selectedMttrCriticality = "";
    }

    container.innerHTML = rows.map((row) => {
        const color = CRITICALITY_COLORS[row.criticality] || "#64748b";
        const criticality = row.criticality || "";
        const isActive = selectedMttrCriticality === criticality;
        return `
            <button type="button" class="criticality-card criticality-filter-card${isActive ? " active" : ""}" data-criticality="${escapeHtml(criticality)}" aria-pressed="${isActive ? "true" : "false"}" style="border-top-color:${escapeHtml(color)};">
                <div class="criticality-header">
                    <span class="criticality-name">${escapeHtml(row.criticality)}</span>
                    <span class="criticality-share">${escapeHtml((row.share_of_total_pct || 0).toFixed(1))}% of total</span>
                </div>
                <div class="criticality-metric">${escapeHtml(fmtHours(row.average_mttr_hours))}</div>
                <div class="criticality-meta">${escapeHtml(fmtNumber(row.work_order_count))} work orders</div>
                <div class="criticality-meta criticality-meta-mttr"><strong>TTR Logged</strong><span>${escapeHtml(fmtHours(row.total_downtime_hours))}</span></div>
            </button>
        `;
    }).join("");

    container.querySelectorAll(".criticality-filter-card").forEach((card) => {
        card.addEventListener("click", () => {
            const nextCriticality = card.dataset.criticality || "";
            selectedMttrCriticality = selectedMttrCriticality === nextCriticality ? "" : nextCriticality;
            const management = getManagement();
            renderCriticalityCards(management.criticality_rows || []);
            renderCharts(management);
        });
    });
}

function renderMtbfSummary(summary) {
    const mtbfLabel = summary.selected_view === "rolling_12_month"
        ? "Rolling 12-month MTBF"
        : (summary.selected_view === "historical" ? "Historical MTBF" : "Selected period MTBF");
    const mtbfText = summary.overall_average_mtbf_hours ? fmtMtbfDays(summary.overall_average_mtbf_hours) : "Insufficient history";

    setText("kpi-mtbf", mtbfText);
    setText("kpi-mtbf-sub", `${mtbfLabel} | ${summary.assets_with_valid_mtbf ? `${fmtNumber(summary.assets_with_valid_mtbf)} asset(s)` : "fewer than 2 valid work orders per asset"}`);
    setText("kpi-repeated-work-orders", fmtNumber(summary.repeated_failure_assets || 0));
    setText("kpi-repeated-work-orders-sub", summary.repeated_failure_assets ? `${fmtNumber(summary.repeated_failure_assets)} asset${summary.repeated_failure_assets === 1 ? "" : "s"} with 3+ work orders — possible repeat failure pattern` : "No repeat failure pattern detected");
    setText("kpi-low-mtbf-assets", summary.lowest_mtbf_hours ? fmtMtbfDays(summary.lowest_mtbf_hours) : "Insufficient history");
    setText("kpi-low-mtbf-assets-sub", summary.lowest_mtbf_asset_name ? `${summary.lowest_mtbf_asset_name}${summary.lowest_mtbf_asset_id ? ` | ${summary.lowest_mtbf_asset_id}` : ""}` : "No low-MTBF asset available");

    setText("mtbf-overall-average", mtbfText);
    setText(
        "mtbf-overall-average-sub",
        summary.assets_with_valid_mtbf
            ? `${mtbfLabel} across ${fmtNumber(summary.assets_with_valid_mtbf)} asset(s) with valid repeat failure gaps`
            : ""
    );

    const lowestAsset = summary.lowest_mtbf_asset_name || summary.lowest_mtbf_asset_id || "No data";
    setText("mtbf-lowest-asset", lowestAsset);
    setText(
        "mtbf-lowest-asset-sub",
        summary.lowest_mtbf_hours
            ? `${fmtDaysHours(summary.lowest_mtbf_hours)} average run time${summary.lowest_mtbf_asset_id ? ` | ${summary.lowest_mtbf_asset_id}` : ""}`
            : ""
    );

    setText("mtbf-repeated-assets", fmtNumber(summary.repeated_failure_assets || 0));
    setText(
        "mtbf-repeated-assets-sub",
        summary.repeated_failure_assets
            ? "Assets showing repeated repair cycles"
            : "No repeated failure pattern detected"
    );

    setText("mtbf-valid-assets", fmtNumber(summary.assets_with_valid_mtbf || 0));
    setText(
        "mtbf-valid-assets-sub",
        summary.assets_with_valid_mtbf
            ? "Assets with at least one valid failure gap"
            : ""
    );
}

function renderMtbfCriticalityCards(rows) {
    const container = document.getElementById("mtbf-criticality-cards");
    if (!container) return;

    const normalizedRows = CRITICALITY_ORDER.map((criticality) => (
        rows.find((row) => row.criticality === criticality) || {
            criticality,
            asset_count: 0,
            work_order_count: 0,
            average_mtbf_hours: null,
            valid_mtbf_asset_count: 0,
        }
    ));

    if (!normalizedRows.length) {
        container.innerHTML = `<div class="empty-state compact">No MTBF data available for the selected period</div>`;
        return;
    }

    container.innerHTML = normalizedRows.map((row) => {
        const color = CRITICALITY_COLORS[row.criticality] || "#64748b";
        const isActive = getSelectedMtbfTrendCriticality() === row.criticality;
        return `
            <button type="button" class="criticality-card criticality-filter-card mtbf-criticality-filter-card${isActive ? " active" : ""}" data-mtbf-criticality="${escapeHtml(row.criticality)}" aria-pressed="${isActive ? "true" : "false"}" style="border-top-color:${escapeHtml(color)};">
                <div class="criticality-header">
                    <span class="criticality-name">${escapeHtml(row.criticality)}</span>
                    <span class="criticality-share">${escapeHtml(fmtNumber(row.asset_count || 0))} assets</span>
                </div>
                <div class="criticality-metric">${escapeHtml(fmtMtbfDays(row.average_mtbf_hours))}</div>
                <div class="criticality-meta">${escapeHtml(fmtNumber(row.work_order_count || 0))} work orders</div>
            </button>
        `;
    }).join("");

    container.querySelectorAll(".mtbf-criticality-filter-card").forEach((card) => {
        card.addEventListener("click", () => {
            const select = document.getElementById("mtbf-trend-criticality-filter");
            const nextCriticality = card.dataset.mtbfCriticality || "";
            if (select) select.value = select.value === nextCriticality ? "" : nextCriticality;
            renderMtbfSection();
        });
    });
}

function renderLowestMtbfList(rows) {
    const container = document.getElementById("mtbf-lowest-list");
    if (!container) return;

    if (!rows.length) {
        container.innerHTML = `<div class="empty-state">No MTBF data available</div>`;
        return;
    }

    destroyChart("mtbfLowestChart");

    container.innerHTML = rows.map((row) => `
        <div class="mtbf-mini-item">
            <div>
                <div class="mtbf-mini-name">${escapeHtml(row.asset_name || row.asset_display_name || row.asset_id || "--")}</div>
                <div class="mtbf-mini-sub">${escapeHtml(row.asset_id || "--")}${row.machine_group ? ` | ${escapeHtml(row.machine_group)}` : ""}</div>
            </div>
            <div class="mtbf-mini-value">${escapeHtml(fmtMtbfDays(row.average_mtbf_hours))}</div>
        </div>
    `).join("");
}

function renderBarChart(id, labels, data, color, axisTitle) {
    const canvas = ensureCanvas(id);
    if (!canvas) return;
    destroyChart(id);
    if (!labels.length) {
        renderEmptyChart(id, "No data available");
        return;
    }
    chartRefs[id] = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
            labels,
            datasets: [{
                data,
                backgroundColor: color,
                borderRadius: 8,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                y: {
                    beginAtZero: true,
                    grid: { color: "#e2e8f0" },
                    title: { display: true, text: axisTitle },
                    ticks: { callback: (value) => fmtAxisHours(value) },
                },
                x: {
                    grid: { display: false },
                    ticks: { font: { size: 11, weight: "600" } },
                },
            },
        },
    });
}

function renderHorizontalBarChart(id, labels, data, color, axisTitle) {
    const canvas = ensureCanvas(id);
    if (!canvas) return;
    destroyChart(id);
    if (!labels.length) {
        renderEmptyChart(id, "No data available");
        return;
    }
    chartRefs[id] = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
            labels,
            datasets: [{
                data,
                backgroundColor: color,
                borderRadius: 8,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            indexAxis: "y",
            plugins: { legend: { display: false } },
            scales: {
                x: {
                    beginAtZero: true,
                    grid: { color: "#e2e8f0" },
                    title: { display: true, text: axisTitle },
                    ticks: { callback: (value) => fmtAxisHours(value) },
                },
                y: {
                    grid: { display: false },
                    ticks: { font: { size: 11, weight: "600" } },
                },
            },
        },
    });
}

function renderTrendChart(trend) {
    const canvas = ensureCanvas("trendChart");
    if (!canvas) return;
    destroyChart("trendChart");
    if (!trend?.labels?.length) {
        renderEmptyChart("trendChart", "No dated work order TTR history available");
        return;
    }
    chartRefs.trendChart = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: {
            labels: trend.labels,
            datasets: [{
                label: "TTR Logged",
                data: trend.downtime_hours,
                borderColor: "#ef4444",
                backgroundColor: "rgba(239, 68, 68, 0.14)",
                fill: true,
                tension: 0.28,
                borderWidth: 3,
                pointRadius: 3,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (context) => `TTR logged: ${fmtHours(context.raw)}`,
                        afterLabel: (context) => {
                            const count = trend.work_order_counts?.[context.dataIndex] || 0;
                            return `${fmtNumber(count)} work orders`;
                        },
                    },
                },
            },
            scales: {
                y: {
                    beginAtZero: true,
                    grid: { color: "#e2e8f0" },
                    ticks: { callback: (value) => fmtAxisHours(value) },
                },
                x: {
                    grid: { display: false },
                },
            },
        },
    });
}

function parseMonthKeyFromLabel(label) {
    const parsed = parseDateValue(label);
    if (parsed) return formatMonthKey(parsed);
    const match = String(label || "").match(/\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b/i);
    if (!match) return "";
    const monthLookup = { jan: "01", feb: "02", mar: "03", apr: "04", may: "05", jun: "06", jul: "07", aug: "08", sep: "09", oct: "10", nov: "11", dec: "12" };
    return `${match[2]}-${monthLookup[match[1].slice(0, 3).toLowerCase()] || ""}`;
}

function formatMonthLabelFromKey(key) {
    const [year, month] = String(key || "").split("-");
    if (!year || !month) return key || "";
    return new Date(Number(year), Number(month) - 1, 1).toLocaleDateString("en-GB", { month: "short", year: "numeric" });
}

function aggregateMtbfByMonth(trend) {
    const monthMap = new Map();
    const monthOrder = [];
    (trend.labels || []).forEach((label, i) => {
        const key = (trend.bucket_keys || [])[i]?.slice(0, 7) || parseMonthKeyFromLabel(label);
        if (!key) return;
        if (!monthMap.has(key)) { monthMap.set(key, { sum: 0, count: 0 }); monthOrder.push(key); }
        const v = Number(trend.mtbf_hours?.[i] || 0);
        const pairCount = Number((trend.pair_counts || [])[i] || 1);
        if (v > 0) {
            monthMap.get(key).sum += v * pairCount;
            monthMap.get(key).count += pairCount;
        }
    });
    return {
        labels: monthOrder.map(formatMonthLabelFromKey),
        values: monthOrder.map((m) => { const d = monthMap.get(m); return d.count > 0 ? Math.round(d.sum / d.count) : 0; }),
        counts: monthOrder.map((m) => monthMap.get(m).count),
    };
}

function buildAssetListLookup() {
    // asset_id → { asset_name, machine_name, criticality, location } from
    // Asset_Master.xlsx (source of truth). asset_name is the per-asset
    // friendly label entered in Excel; machine_name is the group it belongs to.
    const lookup = new Map();
    (assetListData || []).forEach((machine) => {
        (machine.assets || []).forEach((asset) => {
            const key = String(asset.asset_id || "").trim();
            if (!key) return;
            const meta = {
                asset_name: String(asset.label || "").trim(),
                machine_name: machine.machine_name,
                asset_machine_group: String(asset.mappedMachineGroup || "").trim(),
                criticality: machine.criticality,
                location: machine.location,
            };
            lookup.set(key, meta);
            lookup.set(key.toUpperCase(), meta);
        });
    });
    return lookup;
}

function buildMachineGroupCategoryMap() {
    const mgToCategory = new Map();
    (assetListData || []).forEach((machine) => {
        const cat = String(machine.machine_name || "").trim();
        if (!cat) return;
        (machine.assets || []).forEach((asset) => {
            const mg = String(asset.mappedMachineGroup || "").trim();
            if (mg) mgToCategory.set(mg, cat);
        });
    });
    return mgToCategory;
}

// Returns {assetId, category} for each critical asset, filtered by the
// current stage and category filters. category is "Production"|"Utilities"|"Other".
function getFilteredCriticalAssetDetails() {
    if (!assetListLoaded || assetListLoadFailed) return [];
    const selectedStage = getSelectedDowntimeStage();
    const isAllStages = !selectedStage || selectedStage === DOWNTIME_STAGE_ALL;
    const stageFilter = isAllStages ? "" : String(selectedStage).toLowerCase().trim();
    const result = [];
    (assetListData || []).forEach((machine) => {
        if (machine.criticality !== "Critical") return;
        const groupName = String(machine.machine_name || machine.mappedMainAssetGroup || "").trim();
        // Apply page-level category filter
        if (selectedEquipmentCategory !== "all") {
            const groupCat = EQUIP_GROUP_TO_CATEGORY[groupName] || "Unclassified";
            if (groupCat !== selectedEquipmentCategory) return;
        }
        // Map group to display category
        const rawCat = EQUIP_GROUP_TO_CATEGORY[groupName];
        const category = rawCat === "Production Equipment" ? "Production"
            : rawCat === "Utilities" ? "Utilities"
            : "Other";
        (machine.assets || []).forEach((asset) => {
            const assetId = String(asset.asset_id || "").trim().toUpperCase();
            if (!assetId) return;
            if (!isAllStages) {
                const assetStage = String(asset.mappedStage || "").toLowerCase().trim();
                if (assetStage !== stageFilter) return;
            }
            result.push({ assetId, category });
        });
    });
    return result;
}

function getCriticalAssetIdSet() {
    const criticalAssetIds = new Set();
    (assetListData || []).forEach((machine) => {
        if (machine.criticality !== "Critical") return;
        (machine.assets || []).forEach((asset) => {
            const assetId = String(asset.asset_id || "").trim().toUpperCase();
            if (assetId) criticalAssetIds.add(assetId);
        });
    });
    return criticalAssetIds;
}

function renderMtbfCriticalMachinesChart(assetRows) {
    const id = "mtbfCriticalMachinesChart";
    const canvas = ensureCanvas(id);
    if (!canvas) return;
    destroyChart(id);

    const assetLookup = buildAssetListLookup();
    const selectedCriticality = getSelectedMtbfTrendCriticality() || "Critical";
    const criticalityLabel = selectedCriticality === "Critical" ? "Critical" : "Non-Critical / Facility";
    const chartTitle = document.getElementById("mtbf-machine-group-chart-title");
    const chartSubtitle = document.getElementById("mtbf-machine-group-chart-subtitle");
    if (chartTitle) chartTitle.textContent = `MTBF by ${criticalityLabel} Machine Group`;
    if (chartSubtitle) {
        chartSubtitle.textContent = `Grouped by machine name, sorted by lowest MTBF first - ${criticalityLabel.toLowerCase()} assets only`;
    }

    const groupedRows = new Map();
    (assetRows || []).forEach((r) => {
        const mtbfHours = Number(r.average_mtbf_hours || 0);
        if (mtbfHours <= 0) return;
        // Use Asset Master criticality if available, fall back to cached value.
        const meta = getAssetMetaFromLookup(assetLookup, r.asset_id);
        const criticality = getMtbfRecordCriticality(r, assetLookup);
        if (criticality !== selectedCriticality) return;

        const machineName = String(meta?.machine_name || r.machine_group || r.asset_id || "Unmapped Machine").trim();
        const key = machineName.toLowerCase();
        if (!groupedRows.has(key)) {
            groupedRows.set(key, {
                machine_name: machineName,
                weighted_hours: 0,
                weight: 0,
                work_order_count: 0,
                valid_mtbf_gap_count: 0,
                asset_ids: new Set(),
            });
        }

        const group = groupedRows.get(key);
        const gapCount = Math.max(Number(r.valid_mtbf_gap_count || r.gap_count || 0), 0);
        const weight = gapCount || 1;
        group.weighted_hours += mtbfHours * weight;
        group.weight += weight;
        group.work_order_count += Number(r.work_order_count || 0);
        group.valid_mtbf_gap_count += gapCount;
        if (r.asset_id) group.asset_ids.add(String(r.asset_id));
    });

    const rows = [...groupedRows.values()]
        .map((group) => ({
            machine_name: group.machine_name,
            average_mtbf_hours: group.weight ? group.weighted_hours / group.weight : null,
            work_order_count: group.work_order_count,
            valid_mtbf_gap_count: group.valid_mtbf_gap_count,
            asset_count: group.asset_ids.size,
            asset_id_list: [...group.asset_ids].sort(),
        }))
        .filter((r) => Number(r.average_mtbf_hours || 0) > 0)
        .sort((a, b) => Number(a.average_mtbf_hours || 0) - Number(b.average_mtbf_hours || 0))
        .slice(0, 14);

    if (!rows.length) { renderEmptyChart(id, `No ${criticalityLabel.toLowerCase()} machine MTBF data`); return; }
    const labels = rows.map((r) => r.machine_name);
    // Convert to days for the chart scale
    const dataHours = rows.map((r) => Number(r.average_mtbf_hours || 0));
    const dataDays = dataHours.map((h) => Math.round((h / 24) * 10) / 10);
    const colors = dataHours.map((v) => v < 168 ? "#ef4444" : v < 720 ? "#f59e0b" : "#16a34a");
    chartRefs[id] = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: { labels, datasets: [{ data: dataDays, backgroundColor: colors, borderRadius: 6 }] },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            indexAxis: "y",
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => `MTBF: ${fmtDaysHours(dataHours[ctx.dataIndex])}`,
                        afterLabel: (ctx) => {
                            const r = rows[ctx.dataIndex];
                            const assetIds = r.asset_id_list || [];
                            const shownIds = assetIds.slice(0, 6).join(", ");
                            const extra = assetIds.length > 6 ? ` +${assetIds.length - 6} more` : "";
                            return [
                                `${r.asset_count} ${criticalityLabel.toLowerCase()} asset(s)`,
                                `${r.work_order_count} work order(s)`,
                                `${r.valid_mtbf_gap_count || r.asset_count} valid MTBF gap(s)`,
                                shownIds ? `Asset IDs: ${shownIds}${extra}` : "",
                            ].filter(Boolean);
                        },
                    },
                },
            },
            scales: {
                x: {
                    beginAtZero: true,
                    grid: { color: "#e2e8f0" },
                    ticks: { callback: (v) => `${v}d` },
                    title: { display: true, text: "Avg MTBF (days)" },
                },
                y: { grid: { display: false }, ticks: { font: { size: 11, weight: "600" } } },
            },
        },
    });
}

function renderMtbfTrendChart(trend) {
    const id = "mtbfTrendChart";
    const canvas = ensureCanvas(id);
    if (!canvas) return;
    destroyChart(id);
    if (getSelectedMtbfTrendCompareMode()) {
        const comparison = buildMtbfTrendComparisonData();
        if (!comparison) { renderEmptyChart(id, "Choose two different MTBF periods to compare"); return; }
        if (!comparison.hasData) { renderEmptyChart(id, "No repeat failure history for the comparison"); return; }
        chartRefs[id] = new Chart(canvas.getContext("2d"), {
            type: "line",
            data: { labels: comparison.labels, datasets: comparison.datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                spanGaps: true,
                plugins: {
                    legend: { display: true, position: "bottom", labels: { boxWidth: 12, usePointStyle: true, padding: 16 } },
                    tooltip: {
                        callbacks: {
                            label: (ctx) => {
                                const count = ctx.dataset._counts?.[ctx.dataIndex] || 0;
                                return count === 0
                                    ? `${ctx.dataset.label}: No data`
                                    : `${ctx.dataset.label}: ${fmtDaysHours(ctx.raw)}`;
                            },
                            afterLabel: (ctx) => {
                                const count = ctx.dataset._counts?.[ctx.dataIndex] || 0;
                                return count > 0 ? `${count} MTBF gap(s)` : "";
                            },
                        },
                    },
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: { color: "#e2e8f0" },
                        ticks: { callback: (v) => `${(Number(v) / 24).toLocaleString(undefined, { maximumFractionDigits: 0 })} d` },
                        title: { display: true, text: "MTBF (days)" },
                    },
                    x: { grid: { color: "rgba(226,232,240,0.5)" } },
                },
            },
        });
        return;
    }
    if (!trend?.labels?.length) { renderEmptyChart(id, "No repeat failure history available"); return; }
    const mode = getMtbfTrendChartMode();
    if (mode === "monthly") {
        const agg = aggregateMtbfByMonth(trend);
        chartRefs[id] = new Chart(canvas.getContext("2d"), {
            type: "bar",
            data: { labels: agg.labels, datasets: [{ label: "Avg Monthly MTBF", data: agg.values, backgroundColor: "#0f766e", borderRadius: 8 }] },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: (ctx) => `Avg MTBF: ${fmtDaysHours(ctx.raw)}`,
                            afterLabel: (ctx) => `${agg.counts[ctx.dataIndex]} week(s) of data`,
                        },
                    },
                },
                scales: {
                    y: { beginAtZero: true, grid: { color: "#e2e8f0" }, ticks: { callback: (v) => fmtAxisHours(v) } },
                    x: { grid: { display: false } },
                },
            },
        });
    } else {
        chartRefs[id] = new Chart(canvas.getContext("2d"), {
            type: "line",
            data: {
                labels: trend.labels,
                datasets: [{
                    label: "Average MTBF",
                    data: trend.mtbf_hours,
                    borderColor: "#0f766e",
                    backgroundColor: "rgba(15, 118, 110, 0.12)",
                    fill: true,
                    tension: 0.28,
                    borderWidth: 3,
                    pointRadius: 3,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: (ctx) => `Average MTBF: ${fmtDaysHours(ctx.raw)}`,
                            afterLabel: (ctx) => `${trend.pair_counts?.[ctx.dataIndex] || 0} failure gap pair(s)`,
                        },
                    },
                },
                scales: {
                    y: { beginAtZero: true, grid: { color: "#e2e8f0" }, ticks: { callback: (v) => fmtAxisHours(v) } },
                    x: { grid: { display: false } },
                },
            },
        });
    }
}

function renderCharts(management) {
    const criticalityRows = (management.criticality_rows || []).filter((row) => Number(row.work_order_count || 0) > 0);
    renderBarChart(
        "criticalityChart",
        criticalityRows.map((row) => row.criticality),
        criticalityRows.map((row) => Number(row.total_downtime_hours || 0)),
        criticalityRows.map((row) => CRITICALITY_COLORS[row.criticality] || "#64748b"),
        "TTR Logged (hrs)"
    );

    setText(
        "mttr-chart-title",
        selectedMttrCriticality ? `MTTR by Machine Group - ${selectedMttrCriticality}` : "MTTR by Machine Group"
    );
    const mttrRows = [...(management.machine_group_rows || [])]
        .filter((row) => !selectedMttrCriticality || row.criticality === selectedMttrCriticality)
        .filter((row) => Number(row.mttr_hours || 0) > 0)
        .sort((a, b) => Number(b.mttr_hours || 0) - Number(a.mttr_hours || 0))
        .slice(0, 12);
    renderHorizontalBarChart(
        "mttrChart",
        mttrRows.map((row) => row.machine_group),
        mttrRows.map((row) => Number(row.mttr_hours || 0)),
        selectedMttrCriticality ? (CRITICALITY_COLORS[selectedMttrCriticality] || "#8b5cf6") : "#8b5cf6",
        "MTTR (hrs)"
    );

    const locationRows = [...(management.location_rows || [])].slice(0, 12);
    renderHorizontalBarChart(
        "locationChart",
        locationRows.map((row) => row.location),
        locationRows.map((row) => Number(row.total_downtime_hours || 0)),
        "#0f766e",
        "TTR Logged (hrs)"
    );

    renderTrendChart(management.trend || {});
    // MTBF charts rendered by renderMtbfSection() (period-aware)
}

function getPriorityLabel(p) { return `Priority ${p}`; }

function buildWoFilterIndex() {
    const rows = getWorkOrderRows(getManagement());
    // key: "machine_group||location" -> { years, months, priorities }
    const index = new Map();
    rows.forEach((row) => {
        const key = `${row.machine_group || ""}||${row.location || row.building || ""}`;
        if (!index.has(key)) index.set(key, { years: new Set(), months: new Set(), priorities: new Set() });
        const entry = index.get(key);
        const dateStr = String(row.actual_start_time || row.start_time || row.maintenance_start_time || "");
        const m = dateStr.match(/^(\d{4})-(\d{2})/);
        if (m) {
            entry.years.add(m[1]);
            entry.months.add(`${m[1]}-${m[2]}`);
        }
        if (row.priority != null) entry.priorities.add(String(row.priority));
    });
    return index;
}

function getFilteredMachineGroups() {
    const criticality = document.getElementById("group-criticality-filter")?.value || "";
    const location = document.getElementById("group-location-filter")?.value || "";
    const priority = document.getElementById("group-priority-filter")?.value || "";
    const year = document.getElementById("group-year-filter")?.value || "";
    const month = document.getElementById("group-month-filter")?.value || "";
    const search = (document.getElementById("group-search")?.value || "").trim().toLowerCase();

    return (applyCategoryFilter(allWorkOrderRowsCache || getWorkOrderRows(getManagement()))).filter((row) => {
        const raised = getMrRaisedDate(row).date;
        if (criticality && String(row.criticality || row.normalized_criticality || "") !== criticality) return false;
        if (location && String(row.location || row.building || "") !== location) return false;
        if (priority && String(row.priority ?? row.service_level ?? "") !== priority) return false;
        if (year && (!raised || String(raised.getFullYear()) !== year)) return false;
        if (month && (!raised || formatMonthKey(raised) !== month)) return false;
        if (search) {
            const haystack = [row.machine_group, row.location, row.asset_id, getMachineEquipmentName(row), row.description].join(" ").toLowerCase();
            if (!haystack.includes(search)) return false;
        }
        return true;
    });
}

function getPerformanceMachineGroup(row) {
    const rawMachineGroup = String(row?.machine_group || "").trim();
    const haystack = [
        row.machine_group,
        getMachineEquipmentName(row),
        row.description,
        row.job_trade,
        row.maintenance_job_type,
        row.raw_functional_location,
        row.location,
        row.asset_id,
    ].join(" ").toLowerCase().replace("producton", "production").replace("condencer", "condenser");
    if (rawMachineGroup === "Refrigeration" || row.refrigeration_group_match) return "Refrigeration";
    if (rawMachineGroup === "Utilities / Support" || rawMachineGroup === "Utilities") return "Utilities";
    if (rawMachineGroup === "Facility / Building" || rawMachineGroup === "Non-Critical / Facility") return "Non-Critical / Facility";
    if (rawMachineGroup === "Unknown / Review") return "Unknown / Review";
    if (/building|facility|door|room|light|lamp|toilet|cctv|air condition|electrical/.test(haystack)) return "Non-Critical / Facility";
    if (/utility|water|boiler|pump|tank|mdb|support|compressor/.test(haystack)) return "Utilities";
    if (/production|bratt|oven|conveyor|fryer|x-ray|xray|check weight|vacuum|steambox|bowl cutter|sealer|low risk/.test(haystack)) return "Production Equipment";
    if (row.criticality === "Critical") return "Production Equipment";
    if (/general|office|canteen/.test(haystack)) return "Non-Critical / Facility";
    return "Unknown / Review";
}

function buildMachineGroupPerformanceRows(rows = []) {
    const groups = new Map();
    rows.forEach((row) => {
        const group = getPerformanceMachineGroup(row);
        const bucket = groups.get(group) || {
            group,
            rows: [],
            total: 0,
            open: 0,
            finished: 0,
            ttr: [],
            oldestOpenAge: null,
            critical: 0,
            nonCritical: 0,
            assetCounts: new Map(),
        };
        bucket.rows.push(row);
        bucket.total += 1;
        if (isNormalOpenMrStatus(getMrStatus(row))) {
            bucket.open += 1;
            const age = getAgeDaysFrom(getMrRaisedDate(row).date);
            if (age !== null) bucket.oldestOpenAge = Math.max(bucket.oldestOpenAge ?? 0, age);
        }
        if (isMrFinishedStatus(getMrStatus(row))) bucket.finished += 1;
        const ttr = getTtrHours(row);
        if (ttr !== null) bucket.ttr.push(ttr);
        if (row.criticality === "Critical") bucket.critical += 1;
        else bucket.nonCritical += 1;
        const assetId = String(row.asset_id || row.machine_code || "").trim();
        if (assetId) bucket.assetCounts.set(assetId, (bucket.assetCounts.get(assetId) || 0) + 1);
        groups.set(group, bucket);
    });
    const groupOrder = [
        "Production Equipment",
        "Utilities",
        "Refrigeration",
        "Non-Critical / Facility",
        "Unknown / Review",
    ];
    const getGroupSortRank = (group) => {
        const index = groupOrder.indexOf(group);
        return index >= 0 ? index : groupOrder.length;
    };
    return [...groups.values()].map((row) => ({
        ...row,
        closureRate: row.total ? (row.finished / row.total) * 100 : null,
        averageTtr: row.ttr.length ? row.ttr.reduce((sum, value) => sum + value, 0) / row.ttr.length : null,
        repeatedMrCount: [...row.assetCounts.values()].reduce((sum, count) => sum + Math.max(0, count - 1), 0),
    })).sort((a, b) => getGroupSortRank(a.group) - getGroupSortRank(b.group) || a.group.localeCompare(b.group));
}

function renderPriorityBadges(priorityValues) {
    if (!priorityValues || !priorityValues.length) return `<span class="cell-sub">—</span>`;
    return `<div class="priority-cell">${priorityValues.map((p) => {
        return `<span class="priority-badge p${p}" title="Priority ${escapeHtml(String(p))}">${escapeHtml(String(p))}</span>`;
    }).join("")}</div>`;
}

function renderWorkOrderPriorityBadge(priority) {
    if (priority === null || priority === undefined || String(priority).trim() === "") return "";
    const value = String(priority).trim();
    const classSuffix = value.replace(/[^A-Za-z0-9_-]/g, "");
    return `<span class="priority-badge asset-wo-priority p${escapeHtml(classSuffix)}" title="${escapeHtml(getPriorityLabel(value))}">P${escapeHtml(value)}</span>`;
}

function getMtbfMachineGroupLookup() {
    const rows = getMtbfData().machine_group_rows || [];
    const lookup = new Map();
    rows.forEach((row) => {
        const key = [
            row.machine_group || "",
            row.location || row.building || "",
            row.criticality || "",
        ].join("||");
        lookup.set(key, row);
    });
    return lookup;
}

function renderMachineGroupTable() {
    const tbody = document.getElementById("machine-group-tbody");
    if (!tbody) return;
    const rows = buildMachineGroupPerformanceRows(getFilteredMachineGroups());
    if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="9" class="empty-cell">No machine group rows match the selected filters.</td></tr>`;
        return;
    }

    tbody.innerHTML = rows.map((row) => {
        return `
        <tr>
            <td>
                <div class="cell-title">${escapeHtml(row.group)}</div>
                <div class="cell-sub">${fmtNumber(row.assetCounts.size)} asset(s)</div>
            </td>
            <td>${escapeHtml(fmtNumber(row.total))}</td>
            <td>${escapeHtml(fmtNumber(row.open))}</td>
            <td>${escapeHtml(fmtNumber(row.finished))}</td>
            <td>${escapeHtml(row.closureRate === null ? "--" : fmtPercent(row.closureRate))}</td>
            <td>${escapeHtml(row.averageTtr === null ? "--" : fmtHours(row.averageTtr))}</td>
            <td>${escapeHtml(fmtNumber(row.repeatedMrCount))}</td>
            <td>${escapeHtml(formatDays(row.oldestOpenAge))}</td>
            <td>${escapeHtml(`Critical ${fmtNumber(row.critical)} | Non-Critical ${fmtNumber(row.nonCritical)}`)}</td>
        </tr>
    `;
    }).join("");
}

function getUtilitiesRows() {
    return (downtimePayload?.events || [])
        .filter((row) => row?.source === "Status-derived")
        .sort((a, b) => String(b?.start_time || "").localeCompare(String(a?.start_time || "")));
}

function renderUtilitiesTable() {
    const tbody = document.getElementById("utilities-tbody");
    if (!tbody) return;
    const rows = getUtilitiesRows();
    if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="8" class="empty-cell">No status-derived downtime rows are available for the selected period.</td></tr>`;
        return;
    }
    tbody.innerHTML = rows.map((row) => `
        <tr>
            <td>${escapeHtml(row.system || "--")}</td>
            <td>${escapeHtml(row.machine_name || "--")}</td>
            <td>${escapeHtml(row.machine_code || "--")}</td>
            <td>${escapeHtml(row.area || row.location || "--")}</td>
            <td>${escapeHtml(fmtHours(row.duration_hours))}</td>
            <td>${escapeHtml(fmtDateTime(row.start_time))}</td>
            <td>${escapeHtml(fmtDateTime(row.end_time))}</td>
            <td>${escapeHtml(row.detection_type || row.source || "--")}</td>
        </tr>
    `).join("");
}

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function populateFilters(management) {
    const woRows = applyCategoryFilter(allWorkOrderRowsCache || getWorkOrderRows(management));
    populateSelect("group-criticality-filter", [...new Set(woRows.map((row) => row.criticality || row.normalized_criticality).filter(Boolean))], "All Criticalities");
    populateSelect("group-location-filter", [...new Set(woRows.map((row) => row.location || row.building).filter(Boolean))], "All Locations");

    // Year, Month, Priority derived from Dynamics work-order Created date and Service level.
    const years = new Set();
    const months = new Set();
    const priorities = new Set();
    woRows.forEach((row) => {
        const raised = getMrRaisedDate(row).date;
        if (raised) {
            years.add(String(raised.getFullYear()));
            months.add(formatMonthKey(raised));
        }
        if (row.priority != null || row.service_level) priorities.add(String(row.priority ?? row.service_level));
    });

    populateSelect("group-year-filter", [...years].sort().reverse(), "All Years");

    const sortedMonths = [...months].sort().reverse();
    const monthSelect = document.getElementById("group-month-filter");
    if (monthSelect) {
        const currentMonth = monthSelect.value;
        monthSelect.innerHTML = `<option value="">All Months</option>` + sortedMonths.map((ym) => {
            const [y, mo] = ym.split("-");
            const label = `${MONTH_NAMES[parseInt(mo, 10) - 1]} ${y}`;
            return `<option value="${escapeHtml(ym)}">${escapeHtml(label)}</option>`;
        }).join("");
        if (sortedMonths.includes(currentMonth)) monthSelect.value = currentMonth;
    }

    const sortedPriorities = [...priorities].sort((a, b) => Number(a) - Number(b));
    const prioritySelect = document.getElementById("group-priority-filter");
    if (prioritySelect) {
        const currentPriority = prioritySelect.value;
        prioritySelect.innerHTML = `<option value="">All Priorities</option>` + sortedPriorities.map((p) => {
            const label = getPriorityLabel(p);
            return `<option value="${escapeHtml(String(p))}">${escapeHtml(label)}</option>`;
        }).join("");
        if (sortedPriorities.map(String).includes(currentPriority)) prioritySelect.value = currentPriority;
    }
}

function renderHistoricalTrend(rows = []) {
    const tbody = document.getElementById("historical-trend-body");
    if (!tbody) return;
    if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="6" class="empty-cell">No historical reliability data available.</td></tr>`;
        return;
    }
    tbody.innerHTML = rows.map((row) => `
        <tr>
            <td>${escapeHtml(row.year || "--")}</td>
            <td>${escapeHtml(fmtHours(row.ttr_logged_hours))}</td>
            <td>${escapeHtml(fmtNumber(row.work_order_count))}</td>
            <td>${escapeHtml(row.average_ttr_hours === null || row.average_ttr_hours === undefined ? "Insufficient data" : fmtHours(row.average_ttr_hours))}</td>
            <td>${escapeHtml(fmtNumber(row.repeated_work_order_assets || 0))}</td>
            <td>${escapeHtml(fmtNumber(row.critical_work_order_count || 0))}</td>
        </tr>
    `).join("");
}

function renderPeriodHelper(meta = {}) {
    const banner = document.getElementById("period-helper-banner");
    const text = document.getElementById("period-helper-text");
    if (!banner || !text) return;
    const message = meta.all_years_warning || "";
    if (!message) {
        banner.classList.add("hidden");
        text.textContent = "";
        return;
    }
    text.textContent = message;
    banner.classList.remove("hidden");
}

function renderDowntimePage() {
    const management = getManagement();
    const meta = downtimePayload?.meta || {};
    const downtimeSummary = downtimePayload?.summary || {};

    setText("last-synced", meta.last_synced ? `Last synced ${fmtDateTime(meta.last_synced)}` : "Last synced unavailable");
    renderPeriodHelper(meta);
    renderAlerts(management.alerts || []);
    renderSummary(management.summary || {}, downtimeSummary, management, meta);
    renderCriticalityCards(management.criticality_rows || []);
    renderCharts(management);
    populateFilters(management);
    renderWorkOrderResponseSection(getWorkOrderRows(management));
    populateMtbfPeriodFilters();
    renderMtbfSection();
    requestMtbfHistoryLoad();
    renderMachineGroupTable();
    renderUtilitiesTable();
    renderHistoricalTrend(management.historical_trend || []);
    renderActivityStatusCharts();
    updateKdiSection();
    scheduleMrMovementLoad();

    // Refresh machine explorer search results and WO counts whenever the selected period data changes.
    if (document.getElementById("machine-name-list") && assetListData.length) {
        renderMachineNameList();
    }
    if (document.getElementById("machine-name-list") && selectedMachineName && assetListData.length) {
        const machine = assetListData.find((m) => m.machine_name === selectedMachineName);
        if (machine) renderMachineDetail(machine);
    }

    scheduleEmbeddedHeightPost();
}

function handlePeriodChange() {
    const period = document.getElementById("period-select")?.value || "ytd";
    mrMovementUserSelectedYear = false;
    mrTrackingSelectedYear = "";
    mrTrackingSelectedMonth = "";
    toggleCustomDateFilter(period);
    const start = period === "custom" ? (document.getElementById("custom-start")?.value || "") : "";
    const end = period === "custom" ? (document.getElementById("custom-end")?.value || "") : "";
    if (period === "custom" && (!start || !end)) return;
    loadDowntimeData(period, "", start, end).catch((error) => {
        console.error("Downtime period change failed:", error);
    });
}

function handleEquipmentCategoryChange(event) {
    // Frontend-only filter — no re-fetch needed; the page re-renders from the
    // already-loaded rows with the category filter applied in getWorkOrderRows().
    selectedEquipmentCategory = event?.target?.value || "all";
    try {
        renderDowntimePage();
        if (document.getElementById("utilities-panel")?.classList.contains("active")) {
            renderMachineExplorer(getCategoryScopedAllRows());
        }
    } catch (error) {
        console.error("Category filter re-render failed:", error);
    }
}

function handleDowntimeStageChange(event) {
    downtimeStageFilter = event?.target?.value || DOWNTIME_STAGE_ALL;
    resetStageScopedCaches();
    mrMovementUserSelectedYear = false;
    mrTrackingSelectedYear = "";
    mrTrackingSelectedMonth = "";
    reloadCurrentDowntimeData().catch((error) => {
        console.error("Downtime stage change failed:", error);
    });
}

async function handleAssetMappingRefresh() {
    resetStageScopedCaches();
    downtimeCachePayload = null;
    setImportStatus("Refreshing Asset Master mapping...", "");
    try {
        await Promise.all([
            loadAssetList(),
            reloadCurrentDowntimeData(),
        ]);
        setImportStatus("Asset Master mapping refreshed.", "ok");
    } catch (error) {
        console.error("Asset Master mapping refresh failed:", error);
        setImportStatus(`Asset Master refresh failed: ${error.message}`, "error");
    }
}

async function handleWorkOrderImport(event) {
    event.preventDefault();
    const fileInput = document.getElementById("work-order-import-file");
    const file = fileInput?.files?.[0];
    if (!file) {
        setImportStatus("Choose a CSV, XLSX, or XLS work order file first.", "error");
        return;
    }

    const formData = new FormData();
    formData.append("file", file);
    formData.append("replace", document.getElementById("work-order-import-replace")?.checked ? "true" : "false");
    setImportStatus("Importing work order file...", "");

    try {
        const response = await fetch("/api/downtime/import-work-orders", {
            method: "POST",
            body: formData,
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || result.ok === false) {
            throw new Error(result.message || `HTTP ${response.status}`);
        }
        downtimeCachePayload = null;
        resetStageScopedCaches();
        mrMovementUserSelectedYear = false;
        mrTrackingSelectedYear = "";
        mrTrackingSelectedMonth = "";
        mrMachineFilter = "all";
        machineExplorerSelectedAssetId = "";
        machineExplorerRefrigSubgroup = "";
        machineExplorerRefrigCondenserGroupId = "";
        machineExplorerRefrigExpandedCondensers = new Set();
        setImportStatus(`${result.message || "Work order file imported."} Refreshing page data...`, "ok");
        fileInput.value = "";
        await loadDowntimeData(
            document.getElementById("period-select")?.value || "ytd",
            "",
            document.getElementById("custom-start")?.value || "",
            document.getElementById("custom-end")?.value || ""
        );
        await loadWorkOrderImportStatus();
        setImportStatus(`${result.message || "Work order file imported."} Page data refreshed.`, "ok");
    } catch (error) {
        console.error("Work order import failed:", error);
        setImportStatus(`Import failed: ${error.message}`, "error");
    }
}

function setSummaryView(view) {
    document.querySelectorAll("[data-summary-view]").forEach((button) => {
        button.classList.toggle("active", button.dataset.summaryView === view);
    });
    document.getElementById("criticality-summary-panel")?.classList.toggle("active", view === "criticality");
    document.getElementById("mtbf-summary-panel")?.classList.toggle("active", view === "mtbf");
}

function setPerformanceView(view) {
    document.querySelectorAll("[data-performance-view]").forEach((button) => {
        button.classList.toggle("active", button.dataset.performanceView === view);
    });
    document.getElementById("machine-groups-panel")?.classList.toggle("active", view === "machine-groups");
    document.getElementById("utilities-panel")?.classList.toggle("active", view === "utilities");
    if (view === "utilities") renderMachineExplorer(getCategoryScopedAllRows());
}

function wireFilters() {
    [
        "group-criticality-filter",
        "group-location-filter",
        "group-priority-filter",
        "group-year-filter",
        "group-month-filter",
        "group-search",
    ].forEach((id) => {
        const element = document.getElementById(id);
        if (element) {
            element.addEventListener("input", () => {
                renderMachineGroupTable();
                if (id === "group-criticality-filter") renderWorkOrderResponseSection(getWorkOrderRows(getManagement()));
            });
        }
        if (element && element.tagName === "SELECT") {
            element.addEventListener("change", () => {
                renderMachineGroupTable();
                if (id === "group-criticality-filter") renderWorkOrderResponseSection(getWorkOrderRows(getManagement()));
            });
        }
    });

    document.getElementById("period-select")?.addEventListener("change", handlePeriodChange);
    document.getElementById("custom-start")?.addEventListener("change", handlePeriodChange);
    document.getElementById("custom-end")?.addEventListener("change", handlePeriodChange);
    document.getElementById("downtime-stage-filter")?.addEventListener("change", handleDowntimeStageChange);
    document.getElementById("downtime-category-filter")?.addEventListener("change", handleEquipmentCategoryChange);
    document.getElementById("refresh-asset-mapping-btn")?.addEventListener("click", handleAssetMappingRefresh);

    // Topic-panel scoped filters — only the dedicated panels respect these.
    document.getElementById("topic-reliability-year-filter")?.addEventListener("change", (event) => {
        topicReliabilityYearFilter = event.target.value || "";
        renderTopicDataReliabilityPanel();
    });
    document.getElementById("topic-reliability-month-filter")?.addEventListener("change", (event) => {
        topicReliabilityMonthFilter = event.target.value || "";
        renderTopicDataReliabilityPanel();
    });
    document.getElementById("topic-preventive-year-filter")?.addEventListener("change", (event) => {
        topicPreventiveYearFilter = event.target.value || "";
        renderPreventiveCorrectiveSection(preventiveCorrectiveSourceRows);
    });
    document.getElementById("topic-preventive-month-filter")?.addEventListener("change", (event) => {
        topicPreventiveMonthFilter = event.target.value || "";
        renderPreventiveCorrectiveSection(preventiveCorrectiveSourceRows);
    });
    document.getElementById("topic-preventive-analysis-type-filter")?.addEventListener("change", (event) => {
        preventiveCorrectiveAnalysisTypeFilter = event.target.value || "preventive";
        renderPreventiveCorrectiveAnalysis(preventiveCorrectiveSourceRows);
    });
    document.getElementById("topic-preventive-analysis-period-filter")?.addEventListener("change", (event) => {
        preventiveCorrectiveAnalysisPeriodFilter = event.target.value || "full";
        renderPreventiveCorrectiveAnalysis(preventiveCorrectiveSourceRows);
    });
    document.getElementById("topic-preventive-analysis-year-mode-filter")?.addEventListener("change", (event) => {
        preventiveCorrectiveAnalysisYearModeFilter = event.target.value || "financial";
        renderPreventiveCorrectiveAnalysis(preventiveCorrectiveSourceRows);
    });
    document.getElementById("topic-preventive-financial-year-filter")?.addEventListener("change", (event) => {
        preventiveCorrectiveFinancialYearFilter = event.target.value || "";
        renderPreventiveCorrectiveAnalysis(preventiveCorrectiveSourceRows);
    });
    document.getElementById("work-order-import-form")?.addEventListener("submit", handleWorkOrderImport);
    document.getElementById("mr-movement-year")?.addEventListener("change", (event) => {
        mrMovementSelectedYear = event.target.value || mrMovementSelectedYear;
        mrMovementUserSelectedYear = true;
        renderMrMovementSection(getCategoryScopedAllRows());
        renderDynamicsWorkOrderSections(getCategoryScopedAllRows());
    });
    document.getElementById("mr-carryover-filter")?.addEventListener("change", (event) => {
        mrCarryoverFilter = event.target.value || "all";
        renderMrMovementSection(getCategoryScopedAllRows());
    });
    document.getElementById("mr-carryover-sort")?.addEventListener("change", (event) => {
        mrCarryoverSort = event.target.value || "duration_desc";
        renderMrMovementSection(getCategoryScopedAllRows());
    });
    document.getElementById("mr-tracking-equipment")?.addEventListener("change", (event) => {
        mrTrackingEquipmentFilter = event.target.value || "all";
        renderMrTrackingSection(getCategoryScopedAllRows());
        renderWorkOrderResponseSection(getWorkOrderRows(getManagement()));
    });
    document.getElementById("mr-tracking-year")?.addEventListener("change", (event) => {
        mrTrackingSelectedYear = event.target.value || mrTrackingSelectedYear || "all";
        if (!mrTrackingSelectedMonth) mrTrackingSelectedMonth = "all";
        renderMrTrackingSection(getCategoryScopedAllRows());
    });
    document.getElementById("mr-tracking-month")?.addEventListener("change", (event) => {
        mrTrackingSelectedMonth = event.target.value || mrTrackingSelectedMonth || "all";
        renderMrTrackingSection(getCategoryScopedAllRows());
    });
    document.getElementById("mr-machine-filter")?.addEventListener("change", (event) => {
        mrMachineFilter = event.target.value || "all";
        renderMachineMrSection(getCategoryScopedAllRows());
    });
    document.getElementById("wo-sla-year-filter")?.addEventListener("change", (event) => {
        woSlaYearFilter = event.target.value || "all";
        woSlaMonthFilter = "all";
        renderWorkOrderResponseSection(applyCategoryFilter(allWorkOrderRowsCache || getWorkOrderRows(getManagement())));
    });
    document.getElementById("wo-sla-month-filter")?.addEventListener("change", (event) => {
        woSlaMonthFilter = event.target.value || "all";
        renderWorkOrderResponseSection(applyCategoryFilter(allWorkOrderRowsCache || getWorkOrderRows(getManagement())));
    });
    // ── Missing Data Drill-down (data cleansing review) ──
    document.getElementById("missing-data-severity-filter")?.addEventListener("change", (e) => { missingDataFilters.severity = e.target.value || "all"; renderMissingDataDrilldown(); });
    document.getElementById("missing-data-fieldtype-filter")?.addEventListener("change", (e) => { missingDataFilters.fieldType = e.target.value || "all"; renderMissingDataDrilldown(); });
    document.getElementById("missing-data-status-filter")?.addEventListener("change", (e) => { missingDataFilters.slaStatus = e.target.value || "all"; renderMissingDataDrilldown(); });
    document.getElementById("missing-data-group-filter")?.addEventListener("change", (e) => { missingDataFilters.machineGroup = e.target.value || "all"; renderMissingDataDrilldown(); });
    document.getElementById("missing-data-cleared-filter")?.addEventListener("change", (e) => { missingDataFilters.cleared = e.target.value || "all"; renderMissingDataDrilldown(); });
    document.getElementById("missing-data-search")?.addEventListener("input", (e) => { missingDataFilters.search = e.target.value || ""; renderMissingDataDrilldown(); });
    document.getElementById("missing-data-export-btn")?.addEventListener("click", exportDataCleansingTracker);
    document.getElementById("missing-data-followup-export-btn")?.addEventListener("click", exportDataCleansingTracker);
    document.getElementById("cleansing-tracker-export-btn")?.addEventListener("click", exportDataCleansingTracker);
    const missingDataCard = document.getElementById("missing-data-drilldown");
    if (missingDataCard) {
        missingDataCard.addEventListener("change", (e) => {
            const el = e.target.closest(".cleansing-input");
            if (!el) return;
            const prevPage = window.missingDataPage;
            setCleansingField(el.dataset.cleansingKey, el.dataset.cleansingField, el.value);
            if (el.dataset.cleansingField === "cleansingStatus") { window.missingDataPage = prevPage; renderMissingDataDrilldown(); }
        });
        // Pagination clicks (delegated)
        missingDataCard.addEventListener("click", (e) => {
            if (e.target.id === "md-next") { window.missingDataPage = (window.missingDataPage || 0) + 1; renderMissingDataDrilldown(); }
            if (e.target.id === "md-prev") { window.missingDataPage = Math.max(0, (window.missingDataPage || 1) - 1); renderMissingDataDrilldown(); }
            // Edit History tab toggle
            if (e.target.dataset.mdTab) {
                const tab = e.target.dataset.mdTab;
                document.querySelectorAll(".md-tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.mdTab === tab));
                document.getElementById("missing-data-tab-records")?.classList.toggle("hidden", tab !== "records");
                document.getElementById("missing-data-tab-history")?.classList.toggle("hidden", tab !== "history");
                if (tab === "history") { renderSectionEditHistory("missing-data-history-body", "missing-data"); }
            }
        });
    }
    document.getElementById("wo-sla-severity-body")?.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-missing-severity]");
        if (!btn) return;
        openMissingDataDrilldownForSeverity(btn.dataset.missingSeverity);
    });
    // Work Type Classification table
    document.getElementById("wt-classification-card")?.addEventListener("change", (e) => {
        const el = e.target.closest(".wt-input");
        if (!el) return;
        const ctx = { woId: el.dataset.wtWo, mrId: el.dataset.wtMr, equipment: el.dataset.wtEquip, machineGroup: el.dataset.wtGroup, severity: el.dataset.wtSev };
        setWorkTypeReviewField(el.dataset.wtKey, el.dataset.wtField, el.value, ctx);
    });
    document.getElementById("wt-classification-card")?.addEventListener("click", (e) => {
        if (e.target.id === "wt-next") { workTypeReviewPage++; renderWorkTypeClassificationTable(); }
        if (e.target.id === "wt-prev") { workTypeReviewPage = Math.max(0, workTypeReviewPage - 1); renderWorkTypeClassificationTable(); }
        if (e.target.dataset.wtTab) {
            const tab = e.target.dataset.wtTab;
            document.querySelectorAll(".wt-tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.wtTab === tab));
            document.getElementById("wt-tab-records")?.classList.toggle("hidden", tab !== "records");
            document.getElementById("wt-tab-history")?.classList.toggle("hidden", tab !== "history");
            if (tab === "history") renderSectionEditHistory("wt-review-history-body", "work-type");
        }
    });
    document.getElementById("wt-filter-status")?.addEventListener("change", (e) => { workTypeReviewFilter = e.target.value || "open"; workTypeReviewPage = 0; renderWorkTypeClassificationTable(); });
    document.getElementById("wt-search")?.addEventListener("input", (e) => { workTypeReviewSearch = e.target.value || ""; workTypeReviewPage = 0; renderWorkTypeClassificationTable(); });
    // Global Edit History
    document.getElementById("gh-reviewtype-filter")?.addEventListener("change", (e) => { editHistoryFilters.reviewType = e.target.value || "all"; editHistoryPage = 0; renderGlobalEditHistory(); });
    document.getElementById("gh-group-filter")?.addEventListener("change", (e) => { editHistoryFilters.machineGroup = e.target.value || "all"; editHistoryPage = 0; renderGlobalEditHistory(); });
    document.getElementById("gh-search")?.addEventListener("input", (e) => { editHistoryFilters.search = e.target.value || ""; editHistoryPage = 0; renderGlobalEditHistory(); });
    document.getElementById("global-edit-history-pagination")?.addEventListener("click", (e) => {
        if (e.target.id === "gh-next") { editHistoryPage++; renderGlobalEditHistory(); }
        if (e.target.id === "gh-prev") { editHistoryPage = Math.max(0, editHistoryPage - 1); renderGlobalEditHistory(); }
    });
    document.getElementById("pm-cm-list-filter")?.addEventListener("change", (event) => {
        preventiveCorrectiveListFilter = event.target.value || "review";
        renderWorkOrderResponseSection(applyCategoryFilter(allWorkOrderRowsCache || getWorkOrderRows(getManagement())));
    });
    document.getElementById("pm-cm-list-body")?.addEventListener("click", handlePreventiveCorrectiveReviewAction);
    // MR Comparison & Trend Analysis controls
    const cmcRefresh = () => renderCriticalMrComparison(getCategoryScopedAllRows());
    document.getElementById("cmc-scope")?.addEventListener("change", (e) => { cmcScope = e.target.value || "all"; cmcRefresh(); });
    document.getElementById("cmc-mode")?.addEventListener("change", (e) => { cmcMode = e.target.value || "month"; updateCmcControlVisibility(); cmcRefresh(); });
    document.getElementById("cmc-year-view")?.addEventListener("change", (e) => { cmcYearView = e.target.value || "compare"; updateCmcControlVisibility(); cmcRefresh(); });
    document.getElementById("cmc-month-a")?.addEventListener("change", (e) => { cmcMonthA = e.target.value || ""; cmcRefresh(); });
    document.getElementById("cmc-month-b")?.addEventListener("change", (e) => { cmcMonthB = e.target.value || ""; cmcRefresh(); });
    document.getElementById("cmc-year-a")?.addEventListener("change", (e) => { cmcYearA = e.target.value || ""; cmcRefresh(); });
    document.getElementById("cmc-year-b")?.addEventListener("change", (e) => { cmcYearB = e.target.value || ""; cmcRefresh(); });
    [["cmc-custom-a-start", () => cmcCustomAStart], ["cmc-custom-a-end", () => cmcCustomAEnd], ["cmc-custom-b-start", () => cmcCustomBStart], ["cmc-custom-b-end", () => cmcCustomBEnd]].forEach(([id]) => {
        document.getElementById(id)?.addEventListener("change", (e) => {
            if (id === "cmc-custom-a-start") cmcCustomAStart = e.target.value;
            else if (id === "cmc-custom-a-end") cmcCustomAEnd = e.target.value;
            else if (id === "cmc-custom-b-start") cmcCustomBStart = e.target.value;
            else if (id === "cmc-custom-b-end") cmcCustomBEnd = e.target.value;
            cmcRefresh();
        });
    });

    document.getElementById("history-view-toggle")?.addEventListener("click", (event) => {
        const btn = event.target.closest("[data-history-mode]");
        if (!btn) return;
        machineHistoryViewMode = btn.dataset.historyMode || "selected";
        renderMachineExplorer(getCategoryScopedAllRows());
    });
    document.getElementById("machine-history-sort")?.addEventListener("change", (event) => {
        machineHistorySort = event.target.value || "latest_wo_mr";
        renderMachineExplorer(getCategoryScopedAllRows());
    });
    document.getElementById("machine-history-year")?.addEventListener("change", (event) => {
        machineHistoryYearFilter = event.target.value || "";
        machineHistoryDateFrom = "";
        machineHistoryDateTo = "";
        syncMachineHistoryPeriodInputs();
        renderMachineExplorer(getCategoryScopedAllRows());
    });
    document.getElementById("machine-history-month")?.addEventListener("change", (event) => {
        machineHistoryMonthFilter = event.target.value || "";
        machineHistoryDateFrom = "";
        machineHistoryDateTo = "";
        syncMachineHistoryPeriodInputs();
        renderMachineExplorer(getCategoryScopedAllRows());
    });
    document.getElementById("machine-history-date-from")?.addEventListener("change", (event) => {
        machineHistoryDateFrom = event.target.value || "";
        machineHistoryYearFilter = "";
        machineHistoryMonthFilter = "";
        normalizeMachineHistoryDateRange();
        syncMachineHistoryPeriodInputs();
        renderMachineExplorer(getCategoryScopedAllRows());
    });
    document.getElementById("machine-history-date-to")?.addEventListener("change", (event) => {
        machineHistoryDateTo = event.target.value || "";
        machineHistoryYearFilter = "";
        machineHistoryMonthFilter = "";
        normalizeMachineHistoryDateRange();
        syncMachineHistoryPeriodInputs();
        renderMachineExplorer(getCategoryScopedAllRows());
    });
    document.getElementById("machine-history-search")?.addEventListener("input", (event) => {
        machineHistorySearch = event.target.value || "";
        renderMachineExplorer(getCategoryScopedAllRows());
    });
    document.getElementById("include-related-matches")?.addEventListener("change", (event) => {
        includeRelatedMatches = !!event.target.checked;
        renderMachineExplorer(getCategoryScopedAllRows());
    });
    document.getElementById("machine-history-export-btn")?.addEventListener("click", () => {
        exportMachineExplorerData();
    });
    document.getElementById("machine-explorer-machine")?.addEventListener("change", (event) => {
        machineExplorerSelectedAssetId = event.target.value || "";
        const assetSelect = document.getElementById("machine-explorer-asset");
        if (assetSelect) assetSelect.value = machineExplorerSelectedAssetId;
        renderMachineExplorer(getCategoryScopedAllRows());
    });
    document.getElementById("machine-explorer-asset")?.addEventListener("change", (event) => {
        machineExplorerSelectedAssetId = event.target.value || "";
        const machineSelect = document.getElementById("machine-explorer-machine");
        if (machineSelect) machineSelect.value = machineExplorerSelectedAssetId;
        renderMachineExplorer(getCategoryScopedAllRows());
    });
    document.getElementById("machine-explorer-group")?.addEventListener("change", (event) => {
        machineExplorerSelectedGroup = normalizeMachineExplorerGroupLabel(event.target.value || MACHINE_EXPLORER_ALL_GROUP);
        machineExplorerSelectedAssetId = "";
        machineExplorerRefrigSubgroup = "";
        machineExplorerRefrigCondenserGroupId = "";
        machineExplorerRefrigExpandedCondensers = new Set();
        renderMachineExplorer(getCategoryScopedAllRows());
    });
    document.getElementById("machine-explorer-sort")?.addEventListener("change", (event) => {
        machineExplorerSort = event.target.value || "most_wo_mr";
        renderMachineExplorer(getCategoryScopedAllRows());
    });
    document.getElementById("machine-explorer-asset-criticality")?.addEventListener("change", (event) => {
        machineExplorerAssetCriticalityFilter = event.target.value || "";
        machineExplorerSelectedAssetId = "";
        renderMachineExplorer(getCategoryScopedAllRows());
    });
    document.getElementById("machine-explorer-ack-filter")?.addEventListener("change", (event) => {
        machineExplorerAckFilter = event.target.value || "";
        machineExplorerSelectedAssetId = "";
        renderMachineExplorer(getCategoryScopedAllRows());
    });
    document.getElementById("refrig-tree-search")?.addEventListener("input", () => {
        renderRefrigCdeTree(getCategoryScopedAllRows());
    });
    [
        "machine-explorer-criticality",
        "machine-explorer-status",
        "machine-explorer-service-level",
        "machine-explorer-year",
        "machine-explorer-month",
        "machine-explorer-created-by",
        "machine-explorer-started-by",
        "machine-explorer-quality",
    ].forEach((id) => {
        document.getElementById(id)?.addEventListener("change", () => {
            renderMachineExplorer(getCategoryScopedAllRows());
        });
    });
    document.getElementById("mtbf-year-filter")?.addEventListener("change", () => {
        // Reset month when year changes, then repopulate months for that year
        document.getElementById("mtbf-month-filter").value = "";
        populateMtbfPeriodFilters();
        renderMtbfSection();
    });
    document.getElementById("mtbf-month-filter")?.addEventListener("change", () => {
        renderMtbfSection();
    });
    document.getElementById("mtbf-trend-criticality-filter")?.addEventListener("change", () => {
        renderMtbfSection();
    });
    document.getElementById("mtbf-trend-compare-mode")?.addEventListener("change", () => {
        populateMtbfPeriodFilters();
        renderMtbfSection();
    });
    ["mtbf-trend-compare-a", "mtbf-trend-compare-b"].forEach((id) => {
        document.getElementById(id)?.addEventListener("change", renderMtbfSection);
    });
    document.getElementById("mtbf-trend-mode-toggle")?.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-mode]");
        if (!btn) return;
        document.querySelectorAll("#mtbf-trend-mode-toggle .chart-toggle-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        renderMtbfTrendChart(filterMtbfTrend(getMtbfData().trend || {}));
    });
    document.querySelectorAll("[data-summary-view]").forEach((button) => {
        button.addEventListener("click", () => setSummaryView(button.dataset.summaryView || "criticality"));
    });
    document.querySelectorAll("[data-performance-view]").forEach((button) => {
        button.addEventListener("click", () => setPerformanceView(button.dataset.performanceView || "machine-groups"));
    });
    kdiWireStaticControls();
}

// ─── Machine Activity Status ─────────────────────────────────────────

function getLatestWoPerAsset() {
    if (!assetListLoaded || assetListLoadFailed) return new Map();
    const rows = getWorkOrderRows(getManagement());
    const criticalAssetIds = getCriticalAssetIdSet();
    if (!criticalAssetIds.size) return new Map();
    const latestByAsset = new Map();
    rows.forEach((row) => {
        const id = String(row.asset_id || "").trim().toUpperCase();
        if (!id || id === "WO-ASSET" || !criticalAssetIds.has(id)) return;
        const rowStamp = String(row?.actual_start_time || row?.start_time || row?.maintenance_start_time || "").trim();
        const rowDate = parseDateValue(rowStamp);
        const existing = latestByAsset.get(id);
        const existingStamp = String(existing?.actual_start_time || existing?.start_time || existing?.maintenance_start_time || "").trim();
        const existingDate = parseDateValue(existingStamp);
        if (
            !existing
            || (rowDate && !existingDate)
            || (rowDate && existingDate && rowDate > existingDate)
            || (!rowDate && !existingDate && rowStamp > existingStamp)
        ) {
            latestByAsset.set(id, row);
        }
    });
    return latestByAsset;
}

function getCriticalMachineActivityStatus(row) {
    const status = getMrStatus(row);
    const normalized = normalizeClassification(status);
    if (["new", "confirm", "in progress", "inprogress", "rework", "re work"].includes(normalized)) return "maintenance";
    if (["rejected", "reject"].includes(normalized) || isWorkOrderSlaFinished(status, row)) return "active";
    if (row?.is_open === true) return "maintenance";
    if (row?.is_open === false) return "active";
    return "active";
}

function renderActivityStatusCharts() {
    destroyChart("activityDonutChart");
    const setCounts = (activeText, maintenanceText, totalText) => {
        setText("act-count-active", activeText);
        setText("act-count-maintenance", maintenanceText);
        setText("act-count-total", totalText);
    };
    const setDetails = (activeDetail, inactiveDetail, totalDetail) => {
        setText("cma-active-detail", activeDetail);
        setText("cma-inactive-detail", inactiveDetail);
        setText("cma-total-detail", totalDetail);
    };
    const setNote = (message) => setText("critical-status-card-note", message);

    if (assetListLoadFailed) {
        setCounts("--", "--", "--");
        setDetails("", "", "");
        setNote("Critical machine status is unavailable because Asset Master could not be loaded.");
        return;
    }
    if (!assetListLoaded) {
        setCounts("--", "--", "--");
        setDetails("", "", "");
        setNote("Loading Asset Master to identify critical machines.");
        return;
    }

    const criticalAssets = getFilteredCriticalAssetDetails();
    const total = criticalAssets.length;
    if (!total) {
        setCounts("0", "0", "0");
        setDetails("", "", "No critical machines in scope");
        setNote("No critical machines found for the selected scope.");
        return;
    }

    // Build assetId → category lookup
    const assetCategoryMap = new Map(criticalAssets.map((a) => [a.assetId, a.category]));

    // Find latest WO per critical asset within the current filtered rows
    const rows = getWorkOrderRows(getManagement());
    const latestByAsset = new Map();
    rows.forEach((row) => {
        const id = String(row.asset_id || "").trim().toUpperCase();
        if (!id || !assetCategoryMap.has(id)) return;
        const rowStamp = String(row?.actual_start_time || row?.start_time || row?.maintenance_start_time || "").trim();
        const rowDate = parseDateValue(rowStamp);
        const existing = latestByAsset.get(id);
        const existingStamp = String(existing?.actual_start_time || existing?.start_time || existing?.maintenance_start_time || "").trim();
        const existingDate = parseDateValue(existingStamp);
        if (!existing || (rowDate && !existingDate) || (rowDate && existingDate && rowDate > existingDate) || (!rowDate && !existingDate && rowStamp > existingStamp)) {
            latestByAsset.set(id, row);
        }
    });

    // Count by status × category
    let activeProd = 0, activeUtil = 0;
    let inactiveProd = 0, inactiveUtil = 0;
    criticalAssets.forEach(({ assetId, category }) => {
        const woRow = latestByAsset.get(assetId);
        const inactive = woRow && getCriticalMachineActivityStatus(woRow) === "maintenance";
        if (inactive) {
            if (category === "Production") inactiveProd++;
            else if (category === "Utilities") inactiveUtil++;
        } else {
            if (category === "Production") activeProd++;
            else if (category === "Utilities") activeUtil++;
        }
    });

    const activeTotal = activeProd + activeUtil;
    const inactiveTotal = inactiveProd + inactiveUtil;
    const prodTotal = activeProd + inactiveProd;
    const utilTotal = activeUtil + inactiveUtil;
    const otherTotal = total - prodTotal - utilTotal;

    const catStr = (p, u) => {
        const parts = [];
        if (p) parts.push(`Production ${fmtNumber(p)}`);
        if (u) parts.push(`Utilities ${fmtNumber(u)}`);
        return parts.join(" · ") || "—";
    };
    const totalDetail = `Production ${fmtNumber(prodTotal)} · Utilities ${fmtNumber(utilTotal)}${otherTotal ? ` · Other ${fmtNumber(otherTotal)}` : ""}`;

    setCounts(String(activeTotal), String(inactiveTotal), fmtNumber(total));
    setDetails(catStr(activeProd, activeUtil), catStr(inactiveProd, inactiveUtil), totalDetail);

    if (!latestByAsset.size) {
        setNote(`No WOs in this period — all ${fmtNumber(total)} critical machine${total === 1 ? "" : "s"} counted as Active.`);
    } else {
        setNote(`${fmtNumber(latestByAsset.size)} of ${fmtNumber(total)} critical machine${total === 1 ? "" : "s"} had WOs in this period.`);
    }
}

function getWorkOrdersForAsset(assetId) {
    const rows = getWorkOrderRows(getManagement());
    return rows.filter((row) => {
        const rowId = String(row.asset_id || "").trim().toUpperCase();
        return rowId === String(assetId || "").trim().toUpperCase();
    }).sort((a, b) => {
        const ta = String(a.start_time || a.actual_start_time || a.maintenance_start_time || "");
        const tb = String(b.start_time || b.actual_start_time || b.maintenance_start_time || "");
        return tb.localeCompare(ta);
    });
}

function getWorkOrderCountForAsset(assetId) {
    return getWorkOrdersForAsset(assetId).length;
}

function renderAssetWoHistory(assetId) {
    const container = document.getElementById("asset-wo-section");
    if (!container) return;
    const rows = getWorkOrdersForAsset(assetId);
    const titleEl = container.querySelector(".asset-wo-title");
    if (titleEl) titleEl.textContent = `Work Order History — ${escapeHtml(assetId)} (${rows.length} record${rows.length === 1 ? "" : "s"})`;

    const listEl = container.querySelector(".asset-wo-list");
    const emptyEl = container.querySelector(".asset-wo-empty");
    if (!listEl || !emptyEl) return;

    if (!rows.length) {
        listEl.innerHTML = "";
        listEl.style.display = "none";
        emptyEl.style.display = "block";
        emptyEl.textContent = `No work orders found for ${assetId} in the current loaded data.`;
        return;
    }
    emptyEl.style.display = "none";
    listEl.style.display = "flex";
    listEl.innerHTML = rows.map((row) => {
        const desc = row.description || row.job_description || row.work_description || row.asset_display_name || row.machine_name || "--";
        const ttr = getTtrHours(row);
        const startDate = fmtDateOnly(row.actual_start_time || row.start_time || row.maintenance_start_time);
        const endDate = fmtDateOnly(row.actual_end_time || row.end_time || row.maintenance_end_time);
        const dateRange = startDate && endDate && startDate !== endDate
            ? `${startDate} – ${endDate}`
            : startDate || endDate || "";
        const woId = row.work_order_id || row.wo_id || "";
        const status = row.request_state || row.status || "";
        const priorityBadge = renderWorkOrderPriorityBadge(row.priority);
        const calculationFlags = renderCalculationFlags(getRecordCalculationFlags(row));
        return `
            <div class="asset-wo-item">
                <div>
                    <div class="asset-wo-heading">
                        <div class="asset-wo-desc">${escapeHtml(desc)}</div>
                        ${priorityBadge}
                    </div>
                    <div class="asset-wo-meta">${[dateRange, woId, status].filter(Boolean).join(" · ")}</div>
                    ${calculationFlags}
                </div>
                <div class="asset-wo-ttr">${ttr !== null ? escapeHtml(fmtHours(ttr)) : "--"}</div>
            </div>
        `;
    }).join("");
}

function selectAssetUnit(assetId) {
    selectedAssetId = assetId;
    document.querySelectorAll(".asset-unit-card").forEach((card) => {
        card.classList.toggle("active", card.dataset.assetId === assetId);
    });
    renderAssetWoHistory(assetId);
}

function renderMachineDetail(machine) {
    const panel = document.getElementById("machine-detail-panel");
    if (!panel) return;

    const critClass = (machine.criticality || "").toLowerCase().includes("critical") && !(machine.criticality || "").toLowerCase().includes("non") && !(machine.criticality || "").toLowerCase().includes("facility")
        ? "critical"
        : "facility";

    const unitsHtml = machine.assets.length
        ? machine.assets.map((asset) => {
            const woCount = getWorkOrderCountForAsset(asset.asset_id);
            return `
                <div class="asset-unit-card" data-asset-id="${escapeHtml(asset.asset_id)}">
                    <div class="asset-unit-label">${escapeHtml(asset.label || "Unit")}</div>
                    <div class="asset-unit-id">${escapeHtml(asset.asset_id)}</div>
                    <div class="asset-unit-wo-count">${woCount ? `${woCount} work order${woCount === 1 ? "" : "s"}` : "No work orders"}</div>
                </div>
            `;
        }).join("")
        : `<div class="asset-unit-card" data-asset-id="" style="cursor:default;">
               <div class="asset-unit-label">Unit</div>
               <div class="asset-unit-id">--</div>
           </div>`;

    panel.innerHTML = `
        <div class="machine-detail-inner">
            <div class="machine-detail-title">${escapeHtml(machine.machine_name)}</div>
            <div class="machine-detail-meta">
                ${escapeHtml(machine.location)}
                &nbsp;·&nbsp;
                <span class="crit-badge ${escapeHtml(critClass)}">${escapeHtml(machine.criticality)}</span>
                &nbsp;·&nbsp;
                ${machine.asset_count} unit${machine.asset_count === 1 ? "" : "s"}
            </div>
            <div class="asset-unit-grid">${unitsHtml}</div>
            <div class="asset-wo-section" id="asset-wo-section">
                <div class="asset-wo-title">Work Order History</div>
                <div class="asset-wo-empty" style="display:none;"></div>
                <div class="asset-wo-list" style="display:none;"></div>
            </div>
        </div>
    `;

    // Wire unit card clicks via event delegation
    panel.querySelectorAll(".asset-unit-card[data-asset-id]").forEach((card) => {
        if (card.dataset.assetId) {
            card.addEventListener("click", () => selectAssetUnit(card.dataset.assetId));
        }
    });

    // Auto-select first asset
    const firstAsset = machine.assets[0];
    if (firstAsset) selectAssetUnit(firstAsset.asset_id);
}

function selectMachineName(machineName) {
    selectedMachineName = machineName;
    selectedAssetId = null;
    document.querySelectorAll(".machine-name-item").forEach((item) => {
        item.classList.toggle("active", item.dataset.machineName === machineName);
    });
    const machine = assetListData.find((m) => m.machine_name === machineName);
    if (machine) renderMachineDetail(machine);
}

function getMachineAssetIdSet(machine) {
    return new Set((machine?.assets || []).map((asset) => String(asset.asset_id || "").trim().toUpperCase()).filter(Boolean));
}

function getMachineWorkOrderRows(machine) {
    const assetIds = getMachineAssetIdSet(machine);
    if (!assetIds.size) return [];
    return getWorkOrderRows(getManagement()).filter((row) => {
        const rowAssetId = String(row.asset_id || "").trim().toUpperCase();
        return rowAssetId && assetIds.has(rowAssetId);
    });
}

function getMachineWorkOrderCount(machine) {
    return getMachineWorkOrderRows(machine).length;
}

function machineMatchesExplorerSearch(machine, lowerFilter) {
    const terms = String(lowerFilter || "").split(/\s+/).filter(Boolean);
    if (!terms.length) return true;
    const assetText = (machine.assets || []).flatMap((asset) => [
        asset.asset_id,
        asset.label,
    ]);
    const workOrderText = getMachineWorkOrderRows(machine).flatMap((row) => [
        row.work_order_id,
        row.wo_id,
        row.description,
        row.job_description,
        row.work_description,
        row.asset_display_name,
        row.machine_name,
        row.machine_group,
        row.priority,
        row.request_state,
        row.status,
    ]);
    const haystack = [
        machine.machine_name,
        machine.location,
        machine.criticality,
        machine.asset_count,
        ...assetText,
        ...workOrderText,
    ].join(" ").toLowerCase();
    return terms.every((term) => haystack.includes(term));
}

function sortMachineExplorerRows(machines) {
    const sorted = [...machines];
    if (machineExplorerSort === "work_orders_desc") {
        return sorted.sort((a, b) => {
            const diff = getMachineWorkOrderCount(b) - getMachineWorkOrderCount(a);
            if (diff) return diff;
            return String(a.machine_name || "").localeCompare(String(b.machine_name || ""));
        });
    }
    return sorted.sort((a, b) => String(a.machine_name || "").localeCompare(String(b.machine_name || "")));
}

function renderMachineNameItem(machine) {
    const critClass = (machine.criticality || "").toLowerCase().includes("critical") && !(machine.criticality || "").toLowerCase().includes("non") && !(machine.criticality || "").toLowerCase().includes("facility")
        ? "critical" : "facility";
    const isActive = machine.machine_name === selectedMachineName;
    const woCount = getMachineWorkOrderCount(machine);
    return `
        <div class="machine-name-item${isActive ? " active" : ""}" data-machine-name="${escapeHtml(machine.machine_name)}">
            <div>
                <div class="machine-name-label">${escapeHtml(machine.machine_name)}</div>
                <div class="machine-name-loc">${escapeHtml(machine.location)}</div>
            </div>
            <div class="machine-name-meta">
                <span class="machine-asset-count">${machine.asset_count} unit${machine.asset_count === 1 ? "" : "s"}</span>
                <span class="machine-wo-count">${fmtNumber(woCount)} WO</span>
                <span class="crit-badge ${escapeHtml(critClass)}">${escapeHtml(machine.criticality)}</span>
            </div>
        </div>
    `;
}

function renderMachineNameList(filter = machineExplorerSearch) {
    const container = document.getElementById("machine-name-list");
    if (!container) return;
    machineExplorerSearch = String(filter || "");
    const lowerFilter = machineExplorerSearch.trim().toLowerCase();
    const searchInput = document.getElementById("machine-explorer-search");
    if (searchInput && searchInput.value !== machineExplorerSearch) searchInput.value = machineExplorerSearch;
    const sortSelect = document.getElementById("machine-explorer-sort");
    if (sortSelect && sortSelect.value !== machineExplorerSort) sortSelect.value = machineExplorerSort;
    const filtered = lowerFilter
        ? assetListData.filter((m) => machineMatchesExplorerSearch(m, lowerFilter))
        : [...assetListData];

    if (!filtered.length) {
        container.innerHTML = `<div class="empty-state" style="min-height:200px;">No machines match your search</div>`;
        return;
    }

    if (machineExplorerSort === "work_orders_desc") {
        const items = sortMachineExplorerRows(filtered).map(renderMachineNameItem).join("");
        container.innerHTML = `<div class="machine-name-group-label" style="padding:8px 16px 4px;font-size:0.68rem;font-weight:800;text-transform:uppercase;letter-spacing:0;color:var(--text-muted);background:#f0f4f8;border-bottom:1px solid var(--border);">Most Work Orders</div>${items}`;
        container.querySelectorAll(".machine-name-item").forEach((item) => {
            item.addEventListener("click", () => selectMachineName(item.dataset.machineName));
        });
        return;
    }

    // Group by location
    const groups = {};
    filtered.forEach((m) => {
        const loc = m.location || "Other";
        if (!groups[loc]) groups[loc] = [];
        groups[loc].push(m);
    });

    const locationOrder = ["Production Plant", "Building", "Water", "Boiler", "laundry Plant", "Work Area"];
    const sortedLocs = Object.keys(groups).sort((a, b) => {
        const ia = locationOrder.findIndex((loc) => a.toLowerCase().includes(loc.toLowerCase()));
        const ib = locationOrder.findIndex((loc) => b.toLowerCase().includes(loc.toLowerCase()));
        if (ia !== -1 && ib !== -1) return ia - ib;
        if (ia !== -1) return -1;
        if (ib !== -1) return 1;
        return a.localeCompare(b);
    });

    container.innerHTML = sortedLocs.map((loc) => {
        const items = sortMachineExplorerRows(groups[loc]).map(renderMachineNameItem).join("");
        return `<div class="machine-name-group-label" style="padding:8px 16px 4px;font-size:0.68rem;font-weight:800;text-transform:uppercase;letter-spacing:0;color:var(--text-muted);background:#f0f4f8;border-bottom:1px solid var(--border);">${escapeHtml(loc)}</div>${items}`;
    }).join("");

    container.querySelectorAll(".machine-name-item").forEach((item) => {
        item.addEventListener("click", () => selectMachineName(item.dataset.machineName));
    });
}

function renderCurrentDowntimeKpi() {
    const importedRows = getOverviewSourceRows(getManagement());
    if (importedRows.length) {
        renderDowntimeOverviewFromRows(importedRows);
        return;
    }
    if (!openWorkOrdersLoaded) {
        setText("kpi-maintenance-resolution-time", "--");
        setText("kpi-maintenance-resolution-sub", "Loading current in-progress work orders.");
        setText("kpi-maintenance-resolution-count", "-- in-progress work orders");
        setText("kpi-work-order-count", "--");
        setText("kpi-work-order-count-sub", "--");
        return;
    }

    const inProgressWos = (openWorkOrdersData || []).filter((wo) => {
        const state = normalizeClassification(wo.request_state || wo.status);
        return state === "in progress" || state === "inprogress";
    });
    const machineSet = new Set();
    inProgressWos.forEach((wo) => {
        const assetId = String(wo.asset_id || "").trim().toUpperCase();
        if (assetId) machineSet.add(assetId);
    });

    const inProgressCount = inProgressWos.length;
    const openCount = (openWorkOrdersData || []).length;
    setText("kpi-maintenance-resolution-time", fmtNumber(inProgressCount));
    setText("kpi-maintenance-resolution-sub", "Current downtime shown as work orders with status In progress.");
    setText("kpi-maintenance-resolution-count", `${fmtNumber(inProgressCount)} in-progress work order${inProgressCount === 1 ? "" : "s"}`);
    setText("kpi-work-order-count", fmtNumber(openCount));
    setText("kpi-work-order-count-sub", fmtNumber(machineSet.size));
}

function renderOpenWoKpi() {
    return renderCurrentDowntimeKpi();
    const now = Date.now();
    const criticalAssetIds = getCriticalAssetIdSet();
    const criticalWos = (openWorkOrdersData || []).filter((wo) => {
        const assetId = String(wo.asset_id || "").trim().toUpperCase();
        return assetId && criticalAssetIds.has(assetId);
    });
    let totalHours = 0;
    const machineSet = new Set();
    criticalWos.forEach((wo) => {
        const start = wo.actual_start ? new Date(wo.actual_start).getTime() : null;
        if (!start || isNaN(start)) return;
        const elapsed = (now - start) / 3600000; // ms → hours
        if (elapsed > 0) totalHours += elapsed;
        if (wo.asset_id) machineSet.add(wo.asset_id);
    });
    const openCount = criticalWos.length;
    const machineCount = machineSet.size;
    setText("kpi-maintenance-resolution-time", totalHours > 0 ? fmtHours(totalHours) : "--");
    setText("kpi-maintenance-resolution-sub", `Elapsed time across ${openCount} critical-machine open work orders (In Progress / Confirm / New / reWork) from their start date to today.`);
    setText("kpi-maintenance-resolution-count", `${openCount} critical-machine open work order${openCount === 1 ? "" : "s"}`);
    setText("kpi-work-order-count", fmtNumber(openCount));
    setText("kpi-work-order-count-sub", fmtNumber(machineCount));
}

async function loadOpenWorkOrders() {
    try {
        const response = await fetch("./open-workorders.json", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        openWorkOrdersData = data.open_work_orders || [];
        openWorkOrdersLoaded = true;
        renderCurrentDowntimeKpi();
        if (assetListLoaded || assetListLoadFailed) renderActivityStatusCharts();
    } catch (error) {
        openWorkOrdersLoaded = true;
        console.warn("Open work orders load failed:", error);
        renderCurrentDowntimeKpi();
    }
}

async function loadAssetList() {
    try {
        const response = await fetch("/api/asset-list", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        assetListData = data.machines || [];
        assetProfiles = data.asset_profiles || {};
        assetListLoaded = true;
        assetListLoadFailed = false;
        renderMachineNameList();
        renderActivityStatusCharts();
        if (openWorkOrdersData.length) renderCurrentDowntimeKpi();
        // Re-render MTBF and KDI views once Asset Master has arrived so per-asset
        // labels come from the Excel mapping instead of falling back to IDs.
        renderMtbfSection();
        updateKdiSection();
    } catch (error) {
        assetListData = [];
        assetListLoaded = true;
        assetListLoadFailed = true;
        const container = document.getElementById("machine-name-list");
        if (container) container.innerHTML = `<div class="empty-state" style="min-height:200px;">Could not load asset list</div>`;
        console.warn("Asset list load failed:", error);
        renderActivityStatusCharts();
    }
}

// ─── Export ───────────────────────────────────────────────────────────

function setExportStatus(message, type) {
    const el = document.getElementById("export-status");
    if (!el) return;
    el.textContent = message;
    el.className = `import-status${message ? "" : " hidden"} ${type === "ok" ? "ok" : type === "error" ? "error" : ""}`.trim();
    if (message) el.classList.remove("hidden");
    else el.classList.add("hidden");
}

function exportFmtDate(value) {
    if (!value) return "";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    const y = d.getFullYear();
    const mo = String(d.getMonth() + 1).padStart(2, "0");
    const dy = String(d.getDate()).padStart(2, "0");
    const h = String(d.getHours()).padStart(2, "0");
    const mi = String(d.getMinutes()).padStart(2, "0");
    return `${y}-${mo}-${dy} ${h}:${mi}`;
}

function exportRound2(v) {
    if (v === null || v === undefined || !Number.isFinite(Number(v))) return "";
    return Math.round(Number(v) * 100) / 100;
}

function getExportDateParts(dateStr) {
    if (!dateStr) return { month: "", quarter: "", year: "", periodLabel: "" };
    const d = new Date(dateStr);
    if (Number.isNaN(d.getTime())) return { month: "", quarter: "", year: "", periodLabel: "" };
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const year = String(d.getFullYear());
    const q = Math.ceil((d.getMonth() + 1) / 3);
    const quarter = `Q${q}`;
    return { month, quarter, year, periodLabel: `${year} ${quarter}` };
}

function priorityToSeverity(priority) {
    const p = Number(priority);
    if (priority === null || priority === undefined || String(priority).trim() === "" || Number.isNaN(p)) return "Unknown";
    if (p === 1) return "Critical";
    if (p === 2) return "High";
    if (p === 3) return "Medium";
    return "Low";
}

function getExportPriorityLevel(row) {
    const raw = row?.priority;
    if (raw === null || raw === undefined || String(raw).trim() === "") return "";
    const priority = Number(raw);
    return Number.isFinite(priority) ? priority : "";
}

function pickMostSeverePriority(currentPriority, nextPriority) {
    if (currentPriority === "" || currentPriority === null || currentPriority === undefined) return nextPriority;
    if (nextPriority === "" || nextPriority === null || nextPriority === undefined) return currentPriority;
    return Math.min(Number(currentPriority), Number(nextPriority));
}

function normalizeCriticalityGroup(criticality) {
    const c = normalizeClassification(criticality);
    if (!c) return "Unknown";
    if (NON_PRODUCTION_LABELS.has(c) || c.includes("facility") || (c.includes("non") && c.includes("critical"))) {
        return c.includes("facility") ? "Facility / Non-Critical" : "Non-Critical";
    }
    if (c.includes("support")) return "Support";
    if (PRODUCTION_CRITICAL_LABELS.has(c) || c.includes("critical") || c.includes("semi")) return "Critical";
    return criticality || "Unknown";
}

function isPmJobTrade(jobTrade) {
    const t = String(jobTrade || "").toLowerCase();
    return t.includes("preventive") || t.includes("planned maintenance") || t.includes("scheduled") || /\bpm\b/.test(t);
}

function buildMissingFlagsStr(conditions) {
    return Object.entries(conditions).filter(([, v]) => v).map(([k]) => k).join("; ");
}

function exportMaintenanceData() {
    if (typeof XLSX === "undefined") {
        setExportStatus("Export library (SheetJS) not loaded yet. Please wait and try again.", "error");
        return;
    }

    const btn = document.getElementById("export-btn");
    if (btn) { btn.disabled = true; btn.textContent = "Exporting..."; }
    setExportStatus("Building organized Downtime export...", "");

    // Defer to allow the UI update to paint before the Excel build starts.
    setTimeout(() => _runOrganizedDowntimeExport(btn), 30);
}

function cleanExportIdentifier(value) {
    const cleaned = String(value ?? "").trim();
    return cleaned && cleaned !== "--" ? cleaned : "";
}

function getExportWorkOrderKey(row, index) {
    const workOrderId = cleanExportIdentifier(row?.work_order_id || row?.wo_id);
    if (workOrderId) return `wo:${workOrderId}`;
    const requestId = cleanExportIdentifier(row?.request_id || row?.maintenance_order_id);
    if (requestId) return `request:${requestId}`;
    return `row:${String(row?.asset_id || "").trim()}:${String(row?.start_time || row?.actual_start_time || "").trim()}:${index}`;
}

async function loadExportWorkOrderRows(exportNotes) {
    try {
        const response = await fetch(buildDowntimeApiUrl({ period: "all_years", work_orders_only: "1" }), { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        const rows = getWorkOrderRows(payload.management);
        if (rows.length) {
            exportNotes.push("Export source: Live all-year imported work orders from /api/downtime. SeverityLevel is read from each row's imported Priority value.");
            return rows;
        }
    } catch (error) {
        console.warn("Live all-year export source unavailable, falling back to loaded dashboard data:", error);
        exportNotes.push(`WARNING: Live all-year export source unavailable (${error.message}). Falling back to loaded dashboard/cache rows.`);
    }

    const rowMap = new Map();
    getWorkOrderRows(getManagement()).forEach((row, index) => rowMap.set(getExportWorkOrderKey(row, index), row));
    if (getSelectedDowntimeStage() !== DOWNTIME_STAGE_ALL) {
        return [...rowMap.values()];
    }
    const allCachedPeriods = downtimeCachePayload?.payloads || {};
    Object.keys(allCachedPeriods).forEach((key) => {
        (allCachedPeriods[key]?.management?.work_orders || []).forEach((row, index) => {
            const rowKey = getExportWorkOrderKey(row, `${key}:${index}`);
            if (!rowMap.has(rowKey)) rowMap.set(rowKey, row);
        });
    });
    return [...rowMap.values()];
}

async function ensureAssetListForExport(exportNotes) {
    if (assetListData.length) return;
    try {
        const response = await fetch("/api/asset-list", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        assetListData = data.machines || [];
        exportNotes.push("Asset Master mapping loaded during export so Asset ID maps to the correct machine name.");
    } catch (error) {
        exportNotes.push(`WARNING: Asset Master mapping could not be loaded during export (${error.message}). Machine names use work order fallback values.`);
    }
}

function appendOrganizedExportSheet(wb, sheetName, headers, rows, widths = []) {
    const sheet = XLSX.utils.aoa_to_sheet([headers, ...rows]);
    if (headers.length) {
        sheet["!autofilter"] = {
            ref: XLSX.utils.encode_range({
                s: { r: 0, c: 0 },
                e: { r: rows.length, c: headers.length - 1 },
            }),
        };
    }
    if (widths.length) sheet["!cols"] = widths.map((wch) => ({ wch }));
    XLSX.utils.book_append_sheet(wb, sheet, sheetName);
    return sheet;
}

function exportPercent(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "";
    return `${Number(value).toFixed(1)}%`;
}

function getExportActualStart(row) {
    return parseDateValue(row?.actual_start_time || row?.maintenance_start_time || row?.start_time || row?.actual_start);
}

function getExportActualEnd(row) {
    return parseDateValue(row?.actual_end_time || row?.maintenance_end_time || row?.end_time || row?.actual_end);
}

function getOrganizedExportAssetMeta(row, assetLookup) {
    const assetId = String(getMachineAssetId(row) || "").trim().toUpperCase();
    const meta = assetLookup.get(assetId) || {};
    const rawCriticality = meta.criticality || row?.criticality || row?.normalized_criticality || "";
    return {
        assetId,
        machineName: meta.machine_name || getMachineEquipmentName(row),
        criticality: rowIsCritical({ ...row, criticality: rawCriticality }) ? "Critical" : (rawCriticality || "Non-Critical / Facility"),
        location: meta.location || row?.location || row?.building || "",
    };
}

function buildOrganizedExportEntries(rows, assetLookup) {
    return rows.map((row, index) => {
        const asset = getOrganizedExportAssetMeta(row, assetLookup);
        const raised = getMrRaisedDate(row).date;
        const actualStart = getExportActualStart(row);
        const actualEnd = getExportActualEnd(row);
        const status = getMrStatus(row);
        const ttrHours = getTtrHours(row);
        const description = getMrDescription(row);
        const machineGroup = getPerformanceMachineGroup({ ...row, criticality: asset.criticality });
        return {
            row,
            index,
            maintenanceRequest: getMrRequestId(row, index),
            workOrder: getMrWorkOrderOnlyId(row),
            assetId: asset.assetId,
            machineName: asset.machineName,
            criticality: asset.criticality,
            machineGroup,
            location: asset.location,
            status,
            serviceLevel: getMrServiceLevel(row),
            type: row?.job_trade || row?.maintenance_job_type || row?.request_type || "",
            description,
            translatedDescription: row?.translated_description || description,
            startedBy: getMrStartedBy(row),
            createdBy: getMrCreatedBy(row),
            raisedDate: raised,
            actualStart,
            actualEnd,
            ttrHours,
            ageOrDuration: getRowAgeOrDuration(row),
            acknowledgement: getAcknowledgementStatus(row),
            dataQuality: getDataQualityFlag(row),
            dataQualityFlags: getDataQualityFlags(row),
        };
    });
}

function buildExportMtbfIntervals(entries) {
    const byAsset = new Map();
    entries.forEach((entry) => {
        const raisedOk = !entry.raisedDate || entry.actualEnd >= entry.raisedDate;
        const validFinished = isMrFinishedStatus(entry.status)
            && entry.actualStart
            && entry.actualEnd
            && entry.actualEnd >= entry.actualStart
            && raisedOk;
        if (!validFinished || !entry.assetId) return;
        const bucket = byAsset.get(entry.assetId) || [];
        bucket.push(entry);
        byAsset.set(entry.assetId, bucket);
    });

    const intervals = [];
    byAsset.forEach((assetEntries) => {
        assetEntries.sort((a, b) => a.actualStart - b.actualStart);
        for (let i = 1; i < assetEntries.length; i++) {
            const previous = assetEntries[i - 1];
            const next = assetEntries[i];
            if (!previous.actualEnd || !next.actualStart || next.actualStart <= previous.actualEnd) continue;
            const gapHours = (next.actualStart.getTime() - previous.actualEnd.getTime()) / 3600000;
            intervals.push({ previous, next, gapHours });
        }
    });
    return intervals;
}

function buildExportMachineRows(entries) {
    const byAsset = new Map();
    entries.forEach((entry) => {
        if (!entry.assetId) return;
        const bucket = byAsset.get(entry.assetId) || {
            assetId: entry.assetId,
            machineName: entry.machineName,
            criticality: entry.criticality,
            machineGroup: entry.machineGroup,
            rows: [],
        };
        bucket.rows.push(entry.row);
        bucket.machineName = bucket.machineName || entry.machineName;
        byAsset.set(entry.assetId, bucket);
    });
    return [...byAsset.values()].map((bucket) => {
        const summary = summarizeMachineExplorerRows(bucket.rows);
        return [
            bucket.machineName,
            bucket.assetId,
            bucket.criticality,
            bucket.machineGroup,
            summary.total,
            summary.open,
            summary.notAcknowledged,
            summary.inProgress,
            summary.finished,
            exportPercent(summary.closureRate),
            summary.averageTtr !== null ? exportRound2(summary.averageTtr) : "",
            summary.oldestOpenAge !== null ? summary.oldestOpenAge : "",
            summary.invalid,
            summary.latestDate ? exportFmtDate(summary.latestDate) : "",
        ];
    }).sort((a, b) => Number(b[4] || 0) - Number(a[4] || 0) || String(a[0]).localeCompare(String(b[0])));
}

async function _runOrganizedDowntimeExport(btn) {
    try {
        const now = new Date();
        const ts = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, "0")}${String(now.getDate()).padStart(2, "0")}_${String(now.getHours()).padStart(2, "0")}${String(now.getMinutes()).padStart(2, "0")}`;
        const exportNotes = [];

        await ensureAssetListForExport(exportNotes);
        const sourceRows = await loadExportWorkOrderRows(exportNotes);
        const rowMap = new Map();
        sourceRows.forEach((row, index) => rowMap.set(getExportWorkOrderKey(row, index), row));
        const rows = [...rowMap.values()];
        allWorkOrderRowsCache = rows;
        if (!rows.length) throw new Error("No work order rows available to export.");

        const assetLookup = buildAssetListLookup();
        const entries = buildOrganizedExportEntries(rows, assetLookup);
        const machineRows = buildExportMachineRows(entries);
        const mtbfIntervals = buildExportMtbfIntervals(entries);
        const validEntries = entries.filter((entry) => isDataQualityValid(entry.row));
        const invalidEntries = entries.filter((entry) => !isDataQualityValid(entry.row));
        const reviewEntries = entries.filter((entry) => entry.dataQualityFlags.includes("Review status") || entry.acknowledgement === "Review");
        const openEntries = entries.filter((entry) => isNormalOpenMrStatus(entry.status));
        const notAckEntries = entries.filter((entry) => isMrNewStatus(entry.status) && !entry.workOrder);
        const finishedEntries = entries.filter((entry) => isMrFinishedStatus(entry.status));
        const ttrValues = entries.map((entry) => entry.ttrHours).filter((value) => value !== null && Number.isFinite(Number(value)));
        const sortedTtr = [...ttrValues].sort((a, b) => a - b);
        const medianTtr = sortedTtr.length
            ? (sortedTtr.length % 2 ? sortedTtr[(sortedTtr.length - 1) / 2] : (sortedTtr[sortedTtr.length / 2 - 1] + sortedTtr[sortedTtr.length / 2]) / 2)
            : null;
        const mtbfAssets = new Set(mtbfIntervals.map((item) => item.next.assetId));
        const avgMtbfHours = mtbfIntervals.length
            ? mtbfIntervals.reduce((sum, item) => sum + item.gapHours, 0) / mtbfIntervals.length
            : null;

        const wb = XLSX.utils.book_new();
        wb.Props = {
            Title: "Downtime Organized Export",
            Subject: "Maintenance request and work order history",
            Author: "SFST Dashboard",
            CreatedDate: now,
        };

        appendOrganizedExportSheet(wb, "Export_Summary", ["Metric", "Value", "Notes"], [
            ["Generated At", exportFmtDate(now), "Local browser time"],
            ["Source Rows", entries.length, "All-year imported work orders where available"],
            ["Total Open MR", openEntries.length, "Status is New or In progress"],
            ["New / Not Acknowledged MR", notAckEntries.length, "Status New and blank Work Order"],
            ["In Progress MR", entries.filter((entry) => isMrInProgressStatus(entry.status)).length, "Acknowledged but not completed"],
            ["Finished MR", finishedEntries.length, "Status Finished"],
            ["Valid Data Records", validEntries.length, "Data Quality Flag is Valid"],
            ["Invalid / Missing Date Records", invalidEntries.length, "Any Data Quality Flag other than Valid"],
            ["Review Records", reviewEntries.length, "Review status or Review acknowledgement"],
            ["Average TTR Hours", ttrValues.length ? exportRound2(ttrValues.reduce((sum, value) => sum + value, 0) / ttrValues.length) : "", "Finished valid records only"],
            ["Median TTR Hours", medianTtr !== null ? exportRound2(medianTtr) : "", "Finished valid records only"],
            ["MTBF Interval Count", mtbfIntervals.length, "Finished valid records by Asset ID"],
            ["Assets Included In MTBF", mtbfAssets.size, "Assets with at least one valid interval"],
            ["Average MTBF Days", avgMtbfHours !== null ? exportRound2(avgMtbfHours / 24) : "", "Average valid end-to-next-start gap"],
            ...exportNotes.map((note) => ["Export Note", note, ""]),
        ], [28, 24, 82]);

        const historyRows = entries
            .sort((a, b) => String(a.machineGroup).localeCompare(String(b.machineGroup)) || String(a.machineName).localeCompare(String(b.machineName)) || compareLatestMrDateDesc(a.row, b.row))
            .map((entry) => [
                entry.maintenanceRequest,
                entry.workOrder,
                entry.status,
                entry.serviceLevel,
                entry.criticality,
                entry.machineGroup,
                entry.assetId,
                entry.machineName,
                entry.description,
                entry.translatedDescription,
                entry.startedBy,
                entry.createdBy,
                entry.raisedDate ? exportFmtDate(entry.raisedDate) : "",
                entry.actualStart ? exportFmtDate(entry.actualStart) : "",
                entry.actualEnd ? exportFmtDate(entry.actualEnd) : "",
                entry.ageOrDuration,
                entry.ttrHours !== null ? exportRound2(entry.ttrHours) : "",
                entry.acknowledgement,
                entry.dataQuality,
            ]);
        appendOrganizedExportSheet(wb, "WO_MR_History", [
            "Maintenance Request", "Work Order", "Status", "Service Level", "Criticality", "Machine Group",
            "Asset ID", "Machine / Asset Name", "Description", "Translated Description", "Started By", "Created By",
            "Created Date", "Actual Start", "Actual End", "Age / Duration", "TTR Hours",
            "Acknowledgement Status", "Data Quality Flag",
        ], historyRows, [18, 16, 14, 12, 16, 22, 16, 30, 42, 42, 20, 22, 18, 18, 18, 16, 12, 24, 34]);

        appendOrganizedExportSheet(wb, "Machine_Summary", [
            "Machine / Asset Name", "Asset ID", "Criticality", "Machine Group", "Total WO/MR", "Open WO/MR",
            "New / Not Acknowledged", "In Progress", "Finished", "Closure Rate", "Average TTR Hours",
            "Oldest Open MR Age Days", "Invalid / Missing Records", "Latest WO/MR Date",
        ], machineRows, [32, 16, 16, 22, 12, 12, 18, 12, 12, 14, 16, 20, 22, 18]);

        const enrichedRowsForGroup = entries.map((entry) => ({
            ...entry.row,
            asset_id: entry.assetId,
            asset_display_name: entry.machineName,
            criticality: entry.criticality,
            machine_group: entry.machineGroup,
            location: entry.location,
        }));
        const groupRows = buildMachineGroupPerformanceRows(enrichedRowsForGroup).map((group) => [
            group.group,
            group.assetCounts.size,
            group.total,
            group.open,
            group.finished,
            exportPercent(group.closureRate),
            group.averageTtr !== null ? exportRound2(group.averageTtr) : "",
            group.repeatedMrCount,
            group.oldestOpenAge !== null ? group.oldestOpenAge : "",
            group.critical,
            group.nonCritical,
        ]);
        appendOrganizedExportSheet(wb, "Machine_Group_Summary", [
            "Machine Group", "Machine Count", "Total WO/MR", "Open MR", "Finished MR", "Closure Rate",
            "Average TTR Hours", "Repeated MR Count", "Oldest Open MR Age Days", "Critical Records", "Non-Critical Records",
        ], groupRows, [24, 14, 12, 12, 12, 14, 16, 18, 22, 16, 20]);

        const movementMap = new Map();
        const getMovementBucket = (date) => {
            const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
            const bucket = movementMap.get(key) || { key, raised: 0, finished: 0, openRaised: 0, criticalRaised: 0, criticalFinished: 0 };
            movementMap.set(key, bucket);
            return bucket;
        };
        entries.forEach((entry) => {
            if (entry.raisedDate) {
                const bucket = getMovementBucket(entry.raisedDate);
                bucket.raised += 1;
                if (isNormalOpenMrStatus(entry.status)) bucket.openRaised += 1;
                if (entry.criticality === "Critical") bucket.criticalRaised += 1;
            }
            if (isMrFinishedStatus(entry.status) && entry.actualEnd) {
                const bucket = getMovementBucket(entry.actualEnd);
                bucket.finished += 1;
                if (entry.criticality === "Critical") bucket.criticalFinished += 1;
            }
        });
        const movementRows = [...movementMap.values()].sort((a, b) => a.key.localeCompare(b.key)).map((bucket) => [
            bucket.key.slice(0, 4),
            bucket.key,
            bucket.raised,
            bucket.finished,
            bucket.openRaised,
            bucket.criticalRaised,
            bucket.criticalFinished,
        ]);
        appendOrganizedExportSheet(wb, "Monthly_Movement", [
            "Year", "Month", "MR Raised", "MR Finished", "Raised Still Open", "Critical MR Raised", "Critical MR Finished",
        ], movementRows, [10, 12, 12, 12, 16, 18, 20]);

        const openAckRows = notAckEntries
            .sort((a, b) => (getAgeDaysFrom(b.raisedDate) || 0) - (getAgeDaysFrom(a.raisedDate) || 0))
            .map((entry) => [
                entry.maintenanceRequest,
                entry.assetId,
                entry.machineName,
                entry.criticality,
                entry.machineGroup,
                entry.serviceLevel,
                entry.raisedDate ? exportFmtDate(entry.raisedDate) : "",
                getAgeDaysFrom(entry.raisedDate) ?? "",
                entry.createdBy,
                entry.startedBy,
                entry.description,
                entry.translatedDescription,
            ]);
        appendOrganizedExportSheet(wb, "Open_Acknowledgement", [
            "Maintenance Request", "Asset ID", "Machine / Asset Name", "Criticality", "Machine Group", "Service Level",
            "Created Date", "Age Days", "Created By", "Started By", "Description", "Translated Description",
        ], openAckRows, [18, 16, 30, 16, 22, 12, 18, 10, 22, 22, 42, 42]);

        const qualityRows = entries
            .filter((entry) => entry.dataQuality !== "Valid" || entry.acknowledgement === "Review")
            .map((entry) => [
                entry.maintenanceRequest,
                entry.workOrder,
                entry.assetId,
                entry.machineName,
                entry.status,
                entry.acknowledgement,
                entry.dataQuality,
                entry.raisedDate ? exportFmtDate(entry.raisedDate) : "",
                entry.actualStart ? exportFmtDate(entry.actualStart) : "",
                entry.actualEnd ? exportFmtDate(entry.actualEnd) : "",
                entry.description,
            ]);
        appendOrganizedExportSheet(wb, "Data_Quality_Issues", [
            "Maintenance Request", "Work Order", "Asset ID", "Machine / Asset Name", "Status", "Acknowledgement Status",
            "Data Quality Flag", "Created Date", "Actual Start", "Actual End", "Description",
        ], qualityRows, [18, 16, 16, 30, 14, 24, 36, 18, 18, 18, 44]);

        const mttrRows = entries
            .filter((entry) => entry.ttrHours !== null)
            .sort((a, b) => Number(b.ttrHours || 0) - Number(a.ttrHours || 0))
            .map((entry) => [
                entry.maintenanceRequest,
                entry.workOrder,
                entry.assetId,
                entry.machineName,
                entry.criticality,
                entry.machineGroup,
                entry.serviceLevel,
                entry.actualStart ? exportFmtDate(entry.actualStart) : "",
                entry.actualEnd ? exportFmtDate(entry.actualEnd) : "",
                exportRound2(entry.ttrHours),
                entry.description,
                entry.translatedDescription,
            ]);
        appendOrganizedExportSheet(wb, "MTTR_Valid_Records", [
            "Maintenance Request", "Work Order", "Asset ID", "Machine / Asset Name", "Criticality", "Machine Group",
            "Service Level", "Actual Start", "Actual End", "TTR Hours", "Description", "Translated Description",
        ], mttrRows, [18, 16, 16, 30, 16, 22, 12, 18, 18, 12, 42, 42]);

        const mtbfRows = mtbfIntervals.map((item) => [
            item.next.assetId,
            item.next.machineName,
            item.next.criticality,
            item.next.machineGroup,
            item.previous.maintenanceRequest,
            item.previous.workOrder,
            item.previous.actualEnd ? exportFmtDate(item.previous.actualEnd) : "",
            item.next.maintenanceRequest,
            item.next.workOrder,
            item.next.actualStart ? exportFmtDate(item.next.actualStart) : "",
            exportRound2(item.gapHours),
            exportRound2(item.gapHours / 24),
        ]);
        appendOrganizedExportSheet(wb, "MTBF_Intervals", [
            "Asset ID", "Machine / Asset Name", "Criticality", "Machine Group", "Previous MR", "Previous WO",
            "Previous Actual End", "Next MR", "Next WO", "Next Actual Start", "MTBF Hours", "MTBF Days",
        ], mtbfRows, [16, 30, 16, 22, 18, 16, 18, 18, 16, 18, 14, 14]);

        appendOrganizedExportSheet(wb, "Data_Dictionary", ["Sheet", "Column / Metric", "Meaning"], [
            ["WO_MR_History", "Acknowledgement Status", "Not Acknowledged = New with blank Work Order. Closed = Finished. Review = non-standard status."],
            ["WO_MR_History", "Data Quality Flag", "Valid, missing date, invalid date order, unexpected finished date, or review status."],
            ["Machine_Summary", "Closure Rate", "Finished MR divided by total MR for the asset."],
            ["Machine_Group_Summary", "Repeated MR Count", "Total repeated records above one per Asset ID inside each group."],
            ["Monthly_Movement", "MR Raised", "Created date falls in the month."],
            ["Monthly_Movement", "MR Finished", "Finished status and Actual End falls in the month."],
            ["Open_Acknowledgement", "Age Days", "Created date compared to export date."],
            ["MTTR_Valid_Records", "TTR Hours", "Finished records with valid Actual Start and Actual End only."],
            ["MTBF_Intervals", "MTBF Hours", "Gap from previous Actual End to next Actual Start for the same Asset ID."],
            ["All Sheets", "Machine / Asset Name", "Asset Master is the source of truth when Asset ID is found."],
        ], [26, 28, 90]);

        const filename = `downtime_organized_export_${ts}.xlsx`;
        XLSX.writeFile(wb, filename);
        setExportStatus(`Export complete: ${filename} - ${entries.length} WO/MR records, ${machineRows.length} assets, ${invalidEntries.length} data quality issue records.`, "ok");
    } catch (err) {
        console.error("Organized export failed:", err);
        setExportStatus(`Export failed: ${err.message}`, "error");
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = "Export"; }
    }
}

async function _runExport(btn) {

    try {
        const now = new Date();
        const ts = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, "0")}${String(now.getDate()).padStart(2, "0")}_${String(now.getHours()).padStart(2, "0")}${String(now.getMinutes()).padStart(2, "0")}`;
        const exportNotes = [
            "DateRaised: Not available in current data source. Add to import pipeline to enable ResponseTimeHours.",
            "SeverityLevel: Uses the imported work order Priority column for finished and open WOs. Blank when Priority is missing.",
            "ProductionImpact: Not calculated — requires operating hours configuration. All records show estimated status.",
            "PMCompletedBeforeNextFailure: Detected via job_trade keywords (preventive, planned, scheduled, pm). May miss unlabelled PM tasks.",
        ];

        // Collect imported WOs live so SeverityLevel can read the imported Priority column.
        const finishedWoMap = new Map();
        const exportWorkOrderRows = await loadExportWorkOrderRows(exportNotes);
        exportWorkOrderRows.forEach((w, index) => {
            const key = getExportWorkOrderKey(w, index);
            if (key && !finishedWoMap.has(key)) finishedWoMap.set(key, w);
        });
        const finishedRows = [...finishedWoMap.values()];
        if (!finishedRows.length) exportNotes.push("WARNING: No imported work orders found - only open WOs included.");

        // Asset lookup (Asset Master is source of truth for name/criticality)
        const assetLookup = buildAssetListLookup();
        function getAssetMeta(assetId, fallback) {
            const meta = assetLookup.get(String(assetId || "").trim().toUpperCase()) || {};
            return {
                machine_name: meta.machine_name || fallback?.machine_name || fallback?.asset_display_name || "",
                criticality: meta.criticality || fallback?.criticality || "",
                location: meta.location || fallback?.location || fallback?.building || "",
            };
        }

        // ── Per-asset finished WOs sorted chronologically ─────────────────
        const assetFinishedWos = new Map();
        finishedRows.forEach((w) => {
            const id = String(w.asset_id || "").trim().toUpperCase();
            if (!id) return;
            if (!assetFinishedWos.has(id)) assetFinishedWos.set(id, []);
            assetFinishedWos.get(id).push(w);
        });
        assetFinishedWos.forEach((wos) => {
            wos.sort((a, b) => (new Date(a.start_time || 0).getTime()) - (new Date(b.start_time || 0).getTime()));
        });

        // ── Per-asset total WO count (finished + open) ────────────────────
        const assetTotalCount = new Map();
        finishedRows.forEach((w) => {
            const id = String(w.asset_id || "").trim().toUpperCase();
            if (id) assetTotalCount.set(id, (assetTotalCount.get(id) || 0) + 1);
        });
        openWorkOrdersData.forEach((w) => {
            const id = String(w.asset_id || "").trim().toUpperCase();
            if (id) assetTotalCount.set(id, (assetTotalCount.get(id) || 0) + 1);
        });

        // ── Sheet 1: Maintenance_Raw_Cleaned ─────────────────────────────
        // Internal rawDataRows indices (0–21) — sheet1 only shows a filtered subset:
        // 0 WO_ID | 1 AssetID | 2 MachineName | 3 MachineGroup | 4 ProductionLine
        // 5 Criticality (internal) | 6 CriticalityGroup | 7 SeverityLevel | 8 LifecycleState
        // 9 MaintenanceType | 10 Description (internal) | 11 ActualStartDate | 12 ActualEndDate
        // 13 DateRaised (internal) | 14 DowntimeHours | 15 MTTRHours | 16 FailureCount
        // 17 Month (internal) | 18 Quarter (internal) | 19 Year (internal) | 20 PeriodLabel (internal) | 21 MissingInfoFlag (internal)
        const rawDataRows = [];

        finishedRows.forEach((w) => {
            const assetId = String(w.asset_id || "").trim().toUpperCase();
            const workOrderId = cleanExportIdentifier(w.work_order_id || w.wo_id);
            const meta = getAssetMeta(assetId, w);
            const startStr = w.start_time || w.actual_start_time || "";
            const endStr = w.end_time || w.actual_end_time || "";
            const ttr = getTtrHours(w);
            const { month, quarter, year, periodLabel } = getExportDateParts(startStr);
            const critGroup = normalizeCriticalityGroup(meta.criticality);
            const severityLevel = getExportPriorityLevel(w);
            const lifecycleState = w.request_state || "Finished";
            const openLifecycle = isOpenLifecycleState(lifecycleState);

            const missingFlag = buildMissingFlagsStr({
                Missing_WO_ID: !workOrderId,
                Missing_AssetID: !assetId,
                Missing_MachineName: !meta.machine_name,
                Missing_Criticality: !meta.criticality,
                Missing_ActualStartDate: !startStr,
                Finished_Missing_ActualEndDate: !openLifecycle && !endStr,
                Missing_DowntimeHours: !openLifecycle && ttr === null,
                Invalid_Negative_Duration: ttr !== null && ttr < 0,
            });

            rawDataRows.push([
                workOrderId || w.request_id || "",       // [0] WO_ID
                assetId,                                 // [1] AssetID
                meta.machine_name,                       // [2] MachineName
                w.machine_group || meta.machine_name || "", // [3] MachineGroup
                meta.location,                           // [4] ProductionLine
                meta.criticality,                        // [5] Criticality (internal only)
                critGroup,                               // [6] CriticalityGroup
                severityLevel,                           // [7] SeverityLevel (imported Priority column)
                lifecycleState,                          // [8] LifecycleState
                w.job_trade || "",                       // [9] MaintenanceType
                w.description || "",                     // [10] Description (internal only)
                exportFmtDate(startStr),                 // [11] ActualStartDate
                exportFmtDate(endStr),                   // [12] ActualEndDate
                "",                                      // [13] DateRaised (internal only, unavailable)
                ttr !== null && ttr >= 0 ? exportRound2(ttr) : "", // [14] DowntimeHours
                ttr !== null && ttr >= 0 ? exportRound2(ttr) : "", // [15] MTTRHours
                assetTotalCount.get(assetId) || 1,       // [16] FailureCount
                month, quarter, year, periodLabel,       // [17-20] internal only
                missingFlag,                             // [21] internal only
            ]);
        });

        openWorkOrdersData.forEach((w) => {
            const assetId = String(w.asset_id || "").trim().toUpperCase();
            const workOrderId = cleanExportIdentifier(w.wo_id || w.work_order_id);
            const meta = getAssetMeta(assetId, { machine_name: w.machine_name, location: w.location });
            const startStr = w.actual_start || "";
            const { month, quarter, year, periodLabel } = getExportDateParts(startStr);
            const critGroup = normalizeCriticalityGroup(meta.criticality);
            const rawPriority = getExportPriorityLevel(w);

            const missingFlag = buildMissingFlagsStr({
                Missing_WO_ID: !workOrderId,
                Missing_AssetID: !assetId,
                Missing_MachineName: !meta.machine_name,
                Missing_Criticality: !meta.criticality,
                Missing_ActualStartDate: !startStr,
                Open_WO_No_EndDate: true,
            });

            rawDataRows.push([
                workOrderId,                              // [0] WO_ID
                assetId,                                 // [1] AssetID
                meta.machine_name || w.machine_name || "", // [2] MachineName
                meta.machine_name || w.machine_name || "", // [3] MachineGroup
                meta.location || w.location || "",       // [4] ProductionLine
                meta.criticality,                        // [5] Criticality (internal only)
                critGroup,                               // [6] CriticalityGroup
                rawPriority,                             // [7] SeverityLevel (imported Priority column)
                w.request_state || "",                   // [8] LifecycleState
                "",                                      // [9] MaintenanceType
                "",                                      // [10] Description (internal only)
                exportFmtDate(startStr),                 // [11] ActualStartDate
                "",                                      // [12] ActualEndDate
                "",                                      // [13] DateRaised (internal only)
                "",                                      // [14] DowntimeHours
                "",                                      // [15] MTTRHours
                assetTotalCount.get(assetId) || 1,       // [16] FailureCount
                month, quarter, year, periodLabel,       // [17-20] internal only
                missingFlag,                             // [21] internal only
            ]);
        });

        // Columns to include in sheet1 (internal indices from rawDataRows):
        // Removed from display: [5] Criticality, [10] Description, [13] DateRaised,
        //   [17] Month, [18] Quarter, [19] Year, [20] PeriodLabel, [21] MissingInfoFlag
        const sheet1ColIndices = [0, 1, 2, 3, 4, 6, 7, 8, 9, 11, 12, 14, 15, 16];
        const sheet1DisplayHeaders = [
            "WO_ID", "AssetID", "MachineName", "MachineGroup", "ProductionLine",
            "CriticalityGroup", "SeverityLevel", "LifecycleState", "MaintenanceType",
            "ActualStartDate", "ActualEndDate",
            "DowntimeHours", "MTTRHours", "FailureCount",
        ];
        const sheet1Rows = rawDataRows.map((row) => sheet1ColIndices.map((i) => row[i]));
        const sheet1 = XLSX.utils.aoa_to_sheet([sheet1DisplayHeaders, ...sheet1Rows]);
        sheet1["!cols"] = [14, 16, 26, 22, 18, 22, 12, 16, 20, 18, 18, 14, 12, 12].map((w) => ({ wch: w }));

        // ── Sheet 2: Maintenance_KPI_Summary ─────────────────────────────
        // Per-asset summary for Pareto, ANOVA, and comparative analysis in Minitab
        const kpiHeaders = [
            "AssetID", "MachineName", "MachineGroup", "ProductionLine",
            "Criticality", "CriticalityGroup", "SeverityLevel",
            "FinishedWOs", "OpenWOs", "TotalWOs",
            "TotalDowntimeHours", "AvgMTTRHours", "AvgMTBFHours", "AvgMTBFDays",
            "FailureCount",
        ];

        const kpiByAsset = new Map();
        finishedRows.forEach((w) => {
            const id = String(w.asset_id || "").trim().toUpperCase() || "UNKNOWN";
            const meta = getAssetMeta(id, w);
            if (!kpiByAsset.has(id)) {
                kpiByAsset.set(id, {
                    assetId: id,
                    machineName: meta.machine_name,
                    machineGroup: w.machine_group || meta.machine_name || "",
                    productionLine: meta.location,
                    criticality: meta.criticality,
                    critGroup: normalizeCriticalityGroup(meta.criticality),
                    severity: "",
                    finishedWo: 0, openWo: 0,
                    totalTtr: 0, ttrValues: [], mtbfGaps: [],
                });
            }
            const e = kpiByAsset.get(id);
            e.finishedWo++;
            e.severity = pickMostSeverePriority(e.severity, getExportPriorityLevel(w));
            const ttr = getTtrHours(w);
            if (ttr !== null && ttr >= 0) { e.totalTtr += ttr; e.ttrValues.push(ttr); }
        });

        openWorkOrdersData.forEach((w) => {
            const id = String(w.asset_id || "").trim().toUpperCase() || "UNKNOWN";
            const meta = getAssetMeta(id, { machine_name: w.machine_name, location: w.location });
            if (!kpiByAsset.has(id)) {
                kpiByAsset.set(id, {
                    assetId: id,
                    machineName: meta.machine_name || w.machine_name || "",
                    machineGroup: meta.machine_name || w.machine_name || "",
                    productionLine: meta.location || w.location || "",
                    criticality: meta.criticality,
                    critGroup: normalizeCriticalityGroup(meta.criticality),
                    severity: "",
                    finishedWo: 0, openWo: 0,
                    totalTtr: 0, ttrValues: [], mtbfGaps: [],
                });
            }
            const entry = kpiByAsset.get(id);
            entry.openWo++;
            entry.severity = pickMostSeverePriority(entry.severity, getExportPriorityLevel(w));
        });

        // Compute MTBF gaps per asset (end of prev → start of next)
        assetFinishedWos.forEach((wos, id) => {
            const entry = kpiByAsset.get(id);
            if (!entry) return;
            for (let i = 1; i < wos.length; i++) {
                const prevEnd = new Date(wos[i - 1].end_time || "").getTime();
                const nextStart = new Date(wos[i].start_time || "").getTime();
                if (prevEnd && nextStart && nextStart > prevEnd) {
                    entry.mtbfGaps.push((nextStart - prevEnd) / 3600000);
                }
            }
        });

        const kpiData = [...kpiByAsset.values()]
            .sort((a, b) => String(a.criticality).localeCompare(String(b.criticality)) || String(a.machineName).localeCompare(String(b.machineName)))
            .map((e) => {
                const avgMttr = e.ttrValues.length ? e.totalTtr / e.ttrValues.length : "";
                const avgMtbf = e.mtbfGaps.length ? e.mtbfGaps.reduce((s, v) => s + v, 0) / e.mtbfGaps.length : "";
                return [
                    e.assetId, e.machineName, e.machineGroup, e.productionLine,
                    e.criticality, e.critGroup, e.severity,
                    e.finishedWo, e.openWo, e.finishedWo + e.openWo,
                    exportRound2(e.totalTtr),
                    exportRound2(avgMttr),
                    exportRound2(avgMtbf),
                    exportRound2(avgMtbf !== "" ? avgMtbf / 24 : ""),
                    e.finishedWo + e.openWo,
                ];
            });
        const sheet2 = XLSX.utils.aoa_to_sheet([kpiHeaders, ...kpiData]);
        sheet2["!cols"] = [16, 26, 22, 18, 22, 22, 14, 12, 10, 10, 18, 14, 14, 12, 12].map((w) => ({ wch: w }));

        // ── Sheet 3: MTTR_MTBF_Analysis ───────────────────────────────────
        // One row per WO per asset, with failure sequence and MTBF from previous
        const mttrHeaders = [
            "AssetID", "MachineName", "MachineGroup", "Criticality", "ProductionLine",
            "FailureSequence", "WO_ID",
            "ActualStartDate", "ActualEndDate",
            "DowntimeHours", "MTTRHours",
            "MTBFFromPrevious_Hours", "MTBFFromPrevious_Days",
            "TotalFinishedWOsForAsset",
        ];
        const mttrData = [];
        assetFinishedWos.forEach((wos, id) => {
            const meta = getAssetMeta(id, wos[0]);
            wos.forEach((w, idx) => {
                const ttr = getTtrHours(w);
                let mtbfH = "", mtbfD = "";
                if (idx > 0) {
                    const prevEnd = new Date(wos[idx - 1].end_time || "").getTime();
                    const currStart = new Date(w.start_time || "").getTime();
                    if (prevEnd && currStart) {
                        const gap = (currStart - prevEnd) / 3600000;
                        if (gap >= 0) { mtbfH = exportRound2(gap); mtbfD = exportRound2(gap / 24); }
                    }
                }
                mttrData.push([
                    id,
                    meta.machine_name,
                    w.machine_group || meta.machine_name || "",
                    meta.criticality,
                    meta.location,
                    idx + 1,
                    w.work_order_id || "",
                    exportFmtDate(w.start_time),
                    exportFmtDate(w.end_time),
                    ttr !== null && ttr >= 0 ? exportRound2(ttr) : "",
                    ttr !== null && ttr >= 0 ? exportRound2(ttr) : "",
                    mtbfH, mtbfD,
                    wos.length,
                ]);
            });
        });
        const sheet3 = XLSX.utils.aoa_to_sheet([mttrHeaders, ...mttrData]);
        sheet3["!cols"] = [16, 26, 22, 22, 18, 14, 16, 18, 18, 14, 12, 18, 16, 16].map((w) => ({ wch: w }));

        // ── Sheet 4: MTBF_Comparative_Analysis ───────────────────────────
        // One row per failure interval per asset. Designed for Minitab comparative analysis.
        const compHeaders = [
            "FailureIntervalID", "AssetID", "MachineName", "MachineGroup", "ProductionLine",
            "FailureSequence",
            "PreviousWOID", "NextWOID",
            "PreviousFailureEndDate", "NextFailureStartDate",
            "MTBFHours", "MTBFDays",
            "NextDowntimeHours", "NextMTTRHours",
            "FailureType", "IssueCategory",
            "SeverityLevel", "LifecycleState",
            "Month", "Quarter", "Year", "PeriodLabel",
            "PMCompletedBeforeNextFailure",
            "DataQualityFlag", "DataQualityNotes",
        ];
        const compData = [];

        assetFinishedWos.forEach((wos, id) => {
            if (wos.length < 2) return;
            const meta = getAssetMeta(id, wos[0]);

            for (let i = 0; i < wos.length - 1; i++) {
                const prev = wos[i];
                const next = wos[i + 1];

                const prevEndStr = prev.end_time || "";
                const nextStartStr = next.start_time || "";
                const prevEndTs = prevEndStr ? new Date(prevEndStr).getTime() : null;
                const nextStartTs = nextStartStr ? new Date(nextStartStr).getTime() : null;

                let mtbfH = "", mtbfD = "";
                let dqFlag = "Review";
                const dqNotes = [];

                if (!prevEndStr) dqNotes.push("Missing PreviousFailureEndDate");
                if (!nextStartStr) dqNotes.push("Missing NextFailureStartDate");

                if (prevEndTs && nextStartTs) {
                    const gapH = (nextStartTs - prevEndTs) / 3600000;
                    if (gapH > 0) {
                        mtbfH = exportRound2(gapH);
                        mtbfD = exportRound2(gapH / 24);
                        if (gapH > 8760) {
                            dqFlag = "Review";
                            dqNotes.push(`MTBF exceeds 1 year (${exportRound2(gapH / 24)} days) — verify no data gap exists`);
                        } else {
                            dqFlag = "Valid";
                        }
                    } else if (gapH === 0) {
                        mtbfH = 0; mtbfD = 0;
                        dqFlag = "Review";
                        dqNotes.push("Zero MTBF — consecutive WOs share identical timestamps");
                    } else {
                        mtbfH = exportRound2(gapH); mtbfD = exportRound2(gapH / 24);
                        dqFlag = "Invalid";
                        dqNotes.push(`Negative MTBF (${exportRound2(gapH)} hrs) — next WO started before previous ended (overlap)`);
                    }
                }

                // PM detection: any WO on same asset between prevEnd and nextStart with PM-type trade
                let pmCompleted = "Unknown";
                if (prevEndTs && nextStartTs) {
                    const pmExists = wos.some((w, wi) => {
                        if (wi === i || wi === i + 1) return false;
                        const t = new Date(w.start_time || "").getTime();
                        return t > prevEndTs && t < nextStartTs && isPmJobTrade(w.job_trade);
                    });
                    pmCompleted = pmExists ? "Yes" : "No";
                }

                const nextTtr = getTtrHours(next);
                const { month, quarter, year, periodLabel } = getExportDateParts(nextStartStr);

                compData.push([
                    `${id}-${i + 1}`,
                    id,
                    meta.machine_name,
                    next.machine_group || meta.machine_name || "",
                    meta.location,
                    i + 1,
                    prev.work_order_id || "",
                    next.work_order_id || "",
                    exportFmtDate(prevEndStr),
                    exportFmtDate(nextStartStr),
                    mtbfH, mtbfD,
                    nextTtr !== null && nextTtr >= 0 ? exportRound2(nextTtr) : "",
                    nextTtr !== null && nextTtr >= 0 ? exportRound2(nextTtr) : "",
                    next.job_trade || "Unknown",
                    next.description || "",
                    getExportPriorityLevel(next),
                    next.request_state || "Finished",
                    month, quarter, year, periodLabel,
                    pmCompleted,
                    dqFlag,
                    dqNotes.length ? dqNotes.join("; ") : "None",
                ]);
            }
        });
        const sheet4 = XLSX.utils.aoa_to_sheet([compHeaders, ...compData]);
        sheet4["!cols"] = [20, 16, 26, 22, 18, 14, 16, 16, 20, 20, 12, 10, 16, 14, 20, 30, 14, 14, 8, 6, 6, 14, 22, 14, 56].map((w) => ({ wch: w }));

        // ── Sheet 5: Severity_Validation ──────────────────────────────────
        // One row per WO — designed for boxplot, ANOVA, and Pareto in Minitab
        const sevHeaders = [
            "SeverityLevel", "WO_ID", "AssetID", "MachineName", "MachineGroup",
            "DowntimeHours", "LifecycleState", "MaintenanceType",
            "ProductionLine", "ProductionImpact",
        ];
        const sevData = [];
        finishedRows.forEach((w) => {
            const assetId = String(w.asset_id || "").trim().toUpperCase();
            const meta = getAssetMeta(assetId, w);
            const ttr = getTtrHours(w);
            const sevLevel = getExportPriorityLevel(w);
            sevData.push([
                sevLevel,
                w.work_order_id || "",
                assetId, meta.machine_name,
                w.machine_group || meta.machine_name || "",
                ttr !== null && ttr >= 0 ? exportRound2(ttr) : "",
                w.request_state || "Finished",
                w.job_trade || "",
                meta.location,
                "Estimated — operating hours config required",
            ]);
        });
        openWorkOrdersData.forEach((w) => {
            const assetId = String(w.asset_id || "").trim().toUpperCase();
            const meta = getAssetMeta(assetId, { machine_name: w.machine_name, location: w.location });
            const sevLevel = getExportPriorityLevel(w);
            sevData.push([
                sevLevel,
                w.wo_id || "",
                assetId, meta.machine_name || w.machine_name || "",
                meta.machine_name || w.machine_name || "",
                "",
                w.request_state || "",
                "",
                meta.location || w.location || "",
                "Estimated — operating hours config required",
            ]);
        });
        sevData.sort((a, b) => {
            const sa = typeof a[0] === "number" ? a[0] : 99;
            const sb = typeof b[0] === "number" ? b[0] : 99;
            return (sa - sb) || String(a[3]).localeCompare(String(b[3]));
        });
        const sheet5 = XLSX.utils.aoa_to_sheet([sevHeaders, ...sevData]);
        sheet5["!cols"] = [14, 16, 16, 26, 22, 14, 16, 20, 18, 46].map((w) => ({ wch: w }));

        // ── Sheet 6: Criticality_Analysis ─────────────────────────────────
        // Critical + Semi-Critical → "Critical". Groups for comparative Minitab analysis.
        const critGroupOrder = ["Critical", "Facility / Non-Critical", "Non-Critical", "Support", "Unknown"];
        const critAgg = new Map();

        (assetListData || []).forEach((m) => {
            const cg = normalizeCriticalityGroup(m.criticality);
            if (!critAgg.has(cg)) critAgg.set(cg, { critGroup: cg, machineCount: 0, finishedWo: 0, openWo: 0, totalTtr: 0, ttrValues: [], mtbfGaps: [] });
            critAgg.get(cg).machineCount++;
        });
        finishedRows.forEach((w) => {
            const assetId = String(w.asset_id || "").trim().toUpperCase();
            const meta = getAssetMeta(assetId, w);
            const cg = normalizeCriticalityGroup(meta.criticality);
            if (!critAgg.has(cg)) critAgg.set(cg, { critGroup: cg, machineCount: 0, finishedWo: 0, openWo: 0, totalTtr: 0, ttrValues: [], mtbfGaps: [] });
            const e = critAgg.get(cg);
            e.finishedWo++;
            const ttr = getTtrHours(w);
            if (ttr !== null && ttr >= 0) { e.totalTtr += ttr; e.ttrValues.push(ttr); }
        });
        openWorkOrdersData.forEach((w) => {
            const assetId = String(w.asset_id || "").trim().toUpperCase();
            const meta = getAssetMeta(assetId, { machine_name: w.machine_name, location: w.location });
            const cg = normalizeCriticalityGroup(meta.criticality);
            if (!critAgg.has(cg)) critAgg.set(cg, { critGroup: cg, machineCount: 0, finishedWo: 0, openWo: 0, totalTtr: 0, ttrValues: [], mtbfGaps: [] });
            critAgg.get(cg).openWo++;
        });
        assetFinishedWos.forEach((wos, id) => {
            const meta = getAssetMeta(id, wos[0]);
            const cg = normalizeCriticalityGroup(meta.criticality);
            const entry = critAgg.get(cg);
            if (!entry) return;
            for (let i = 1; i < wos.length; i++) {
                const prevEnd = new Date(wos[i - 1].end_time || "").getTime();
                const nextStart = new Date(wos[i].start_time || "").getTime();
                if (prevEnd && nextStart && nextStart > prevEnd) entry.mtbfGaps.push((nextStart - prevEnd) / 3600000);
            }
        });

        const critHeaders = [
            "CriticalityGroup", "MachineCount",
            "FinishedWOs", "OpenWOs", "TotalWOs",
            "TotalDowntimeHours", "AvgMTTRHours",
            "AvgMTBFHours", "AvgMTBFDays",
            "TotalFailureCount",
            "GroupingNote",
        ];
        const critData = [...critAgg.entries()]
            .sort((a, b) => {
                const ia = critGroupOrder.indexOf(a[0]);
                const ib = critGroupOrder.indexOf(b[0]);
                return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
            })
            .map(([, e]) => {
                const avgMttr = e.ttrValues.length ? e.totalTtr / e.ttrValues.length : "";
                const avgMtbf = e.mtbfGaps.length ? e.mtbfGaps.reduce((s, v) => s + v, 0) / e.mtbfGaps.length : "";
                const note = e.critGroup === "Critical" ? "Includes Critical and Semi-Critical assets" : "";
                return [
                    e.critGroup, e.machineCount,
                    e.finishedWo, e.openWo, e.finishedWo + e.openWo,
                    exportRound2(e.totalTtr),
                    exportRound2(avgMttr),
                    exportRound2(avgMtbf),
                    exportRound2(avgMtbf !== "" ? avgMtbf / 24 : ""),
                    e.finishedWo + e.openWo,
                    note,
                ];
            });
        const sheet6 = XLSX.utils.aoa_to_sheet([critHeaders, ...critData]);
        sheet6["!cols"] = [22, 14, 12, 10, 10, 18, 14, 14, 12, 16, 40].map((w) => ({ wch: w }));

        // ── Sheet 7: Missing_Info_Flags ────────────────────────────────────
        const flagHeaders = [
            "WO_ID", "AssetID", "MachineName", "LifecycleState",
            "ActualStartDate", "ActualEndDate",
            "MissingFields", "AnalysisImpact", "MTBFImpact",
        ];
        function getMtbfFlagImpact(flags) {
            if (flags.includes("Missing_AssetID")) return "Excluded from MTBF: missing AssetID prevents asset-level gap calculation";
            if (flags.includes("Missing_ActualStartDate")) return "Excluded from MTBF: missing start date prevents next-failure gap calculation";
            if (flags.includes("Finished_Missing_ActualEndDate")) return "Excluded from MTBF: missing end date prevents previous-failure completion time";
            if (flags.includes("Open_WO_No_EndDate")) return "Excluded from MTBF until the work order has an end date";
            if (flags.includes("Invalid_Negative_Duration")) return "Excluded from MTBF: invalid negative duration";
            if (flags.includes("Missing_DowntimeHours")) return "MTBF can still use this row if AssetID, start date, and end date are valid; excluded from MTTR/TTR only";
            return "No MTBF exclusion from these flags";
        }
        const flagData = rawDataRows
            .filter((row) => row[21])
            .map((row) => {
                const flags = row[21];
                const impacts = [];
                if (flags.includes("Missing_WO_ID")) impacts.push("Cannot trace record to a unique work order; duplicate checks and audit trail incomplete");
                if (flags.includes("Missing_AssetID")) impacts.push("Cannot link to Asset Master; MTBF and mapping classification unavailable");
                if (flags.includes("Missing_ActualStartDate")) impacts.push("MTBF gap calculation skipped; time-series analysis excluded");
                if (flags.includes("Finished_Missing_ActualEndDate")) impacts.push("TTR/DowntimeHours cannot be calculated; MTBF interval incomplete");
                if (flags.includes("Missing_DowntimeHours")) impacts.push("Excluded from MTTR averages");
                if (flags.includes("Invalid_Negative_Duration")) impacts.push("Excluded from MTTR, MTBF, and downtime totals");
                if (flags.includes("Missing_Criticality")) impacts.push("Criticality analysis may be incomplete for this asset");
                if (flags.includes("Missing_MachineName")) impacts.push("Cannot group by machine; Pareto chart accuracy affected");
                if (flags.includes("Open_WO_No_EndDate")) impacts.push("Expected — open WO has no end date yet");
                return [row[0], row[1], row[2], row[8], row[11], row[12], flags, impacts.join("; "), getMtbfFlagImpact(flags)];
            });
        const sheet7 = XLSX.utils.aoa_to_sheet([flagHeaders, ...flagData]);
        sheet7["!cols"] = [16, 14, 26, 16, 18, 18, 64, 80, 70].map((w) => ({ wch: w }));

        // ── Sheet 8: Data_Dictionary ──────────────────────────────────────
        const dictHeaders = ["Sheet", "Column", "Definition", "DataType", "SourceField", "CalculationMethod", "Notes"];
        const dictRows = [
            dictHeaders,
            ["EXPORT_INFO", "GeneratedAt", exportFmtDate(now.toISOString()), "DateTime", "System", "Auto-generated", ""],
            ["EXPORT_INFO", "ImportedWOCount", String(finishedRows.length), "Integer", "/api/downtime?period=all_years&work_orders_only=1", "Deduplicated by WO ID, Request ID, or AssetID/start fallback", "Live source used so SeverityLevel can read imported Priority."],
            ["EXPORT_INFO", "OpenWOCount", String(openWorkOrdersData.length), "Integer", "open-workorders.json", "All open WOs", ""],
            ["EXPORT_INFO", "AssetMasterMachineCount", String(assetListData.length), "Integer", "/api/asset-list", "Machines from Asset Master", ""],
            ["EXPORT_INFO", "ValidMTBFIntervals", String(compData.filter((r) => r[23] === "Valid").length), "Integer", "Derived", "MTBF_Comparative_Analysis rows where DataQualityFlag = Valid", ""],
            ...exportNotes.map((n) => ["EXPORT_INFO", "DataNote", n, "Note", "Multiple", "", ""]),
            ["", "", "", "", "", "", ""],
            ["Maintenance_Raw_Cleaned", "WO_ID", "Work order identifier", "Text", "work_order_id / wo_id", "Direct from CMMS", ""],
            ["Maintenance_Raw_Cleaned", "AssetID", "Asset identifier from CMMS", "Text", "asset_id", "Normalised to uppercase", ""],
            ["Maintenance_Raw_Cleaned", "MachineName", "Machine name (Asset Master is source of truth)", "Text", "/api/asset-list machine_name", "Asset Master lookup by AssetID; falls back to CMMS name if not found", ""],
            ["Maintenance_Raw_Cleaned", "MachineGroup", "Machine group or production cell", "Text", "machine_group / MachineName", "From WO data; falls back to MachineName", ""],
            ["Maintenance_Raw_Cleaned", "ProductionLine", "Work area or production line location", "Text", "location / building", "Asset Master lookup first, then WO data", ""],
            ["Maintenance_Raw_Cleaned", "CriticalityGroup", "Standardised criticality group for analysis", "Text", "Derived from Asset Master compatibility criticality", "Critical + Semi-Critical → 'Critical'; Facility/Non-Critical unchanged; Support grouped separately", "Used for Criticality_Analysis grouping in Minitab. Full Criticality label is in the Criticality_Analysis sheet."],
            ["Maintenance_Raw_Cleaned", "SeverityLevel", "Priority/severity level from imported work order priority", "Numeric", "priority", "Direct from imported work order Priority column for finished and open WOs. Blank when Priority is missing.", "Numeric for Minitab analysis; lower values indicate higher priority when the source uses 1 as highest."],
            ["Maintenance_Raw_Cleaned", "LifecycleState", "Current lifecycle / status of the WO", "Text", "request_state", "Direct from CMMS", "Values: Finished, In Progress, Confirm, reWork, New"],
            ["Maintenance_Raw_Cleaned", "MaintenanceType", "Type of maintenance activity", "Text", "job_trade", "Direct from CMMS", "e.g. Production Machine, Electrical System, Air Condition. Blank for Open WOs."],
            ["Maintenance_Raw_Cleaned", "ActualStartDate", "Actual start date-time", "DateTime", "start_time / actual_start", "Format: YYYY-MM-DD HH:MM", ""],
            ["Maintenance_Raw_Cleaned", "ActualEndDate", "Actual end date-time (Finished WOs only)", "DateTime", "end_time", "Format: YYYY-MM-DD HH:MM", "Blank for open WOs"],
            ["Maintenance_Raw_Cleaned", "DowntimeHours", "Duration from ActualStartDate to ActualEndDate (TTR)", "Numeric", "ttr_hours", "Direct from CMMS. Same as MTTRHours at individual record level.", "Blank for open WOs"],
            ["Maintenance_Raw_Cleaned", "MTTRHours", "Mean Time To Repair — individual record value", "Numeric", "ttr_hours", "Same as DowntimeHours. Aggregate MTTR computed in KPI_Summary and Criticality_Analysis sheets.", "Blank for open WOs"],
            ["Maintenance_Raw_Cleaned", "FailureCount", "Total WOs for this AssetID (finished + open)", "Integer", "Derived", "Count of all WOs sharing the same AssetID", ""],
            ["Maintenance_Raw_Cleaned", "Removed_Criticality", "Removed from this sheet — full label available in Criticality_Analysis and KPI_Summary sheets", "Note", "/api/asset-list criticality", "", ""],
            ["Maintenance_Raw_Cleaned", "Removed_DateRaised", "Not available in current data source", "Note", "N/A", "Add date_raised to import pipeline to enable ResponseTimeHours calculation", ""],
            ["Maintenance_Raw_Cleaned", "Removed_Description", "Available internally but excluded from this sheet for conciseness", "Note", "description", "", ""],
            ["Maintenance_Raw_Cleaned", "Removed_Month_Quarter_Year_PeriodLabel", "Removed from this sheet — available in MTBF_Comparative_Analysis and MTTR_MTBF_Analysis", "Note", "ActualStartDate", "Derived", ""],
            ["", "", "", "", "", "", ""],
            ["MTBF_Comparative_Analysis", "FailureIntervalID", "Unique ID for each MTBF interval", "Text", "Derived", "Format: {AssetID}-{FailureSequence} e.g. ENWA-240004-1", ""],
            ["MTBF_Comparative_Analysis", "FailureSequence", "Sequential position of this interval for the asset", "Integer", "Derived", "1 = first gap between consecutive WOs, 2 = second gap, etc.", ""],
            ["MTBF_Comparative_Analysis", "PreviousWOID / NextWOID", "Work order IDs bounding this MTBF interval", "Text", "work_order_id", "Direct", "PreviousWOID = earlier WO; NextWOID = later WO"],
            ["MTBF_Comparative_Analysis", "PreviousFailureEndDate", "End date of the previous WO (machine back online)", "DateTime", "end_time of previous WO", "YYYY-MM-DD HH:MM", "Missing = DataQualityFlag will be Review or Invalid"],
            ["MTBF_Comparative_Analysis", "NextFailureStartDate", "Start date of the next WO (machine failed again)", "DateTime", "start_time of next WO", "YYYY-MM-DD HH:MM", ""],
            ["MTBF_Comparative_Analysis", "MTBFHours", "Hours from PreviousFailureEndDate to NextFailureStartDate", "Numeric", "Derived", "(NextFailureStartDate - PreviousFailureEndDate) / 3600000 ms", "Negative = overlap (Invalid). >8760 hrs = Review."],
            ["MTBF_Comparative_Analysis", "MTBFDays", "MTBFHours / 24", "Numeric", "Derived", "MTBFHours / 24", ""],
            ["MTBF_Comparative_Analysis", "NextDowntimeHours / NextMTTRHours", "TTR of the next (following) work order", "Numeric", "ttr_hours of next WO", "Direct from CMMS", "Same value — individual record TTR"],
            ["MTBF_Comparative_Analysis", "PMCompletedBeforeNextFailure", "Whether a PM WO was completed in the MTBF window", "Text", "job_trade of WOs between failures", "Searches for WO with PM-type job_trade on same asset between prevEnd and nextStart", "Yes / No / Unknown. Unknown = dates missing so window cannot be evaluated."],
            ["MTBF_Comparative_Analysis", "DataQualityFlag", "Quality classification for Minitab use", "Text", "Derived", "Valid: positive MTBF ≤1yr with both dates. Review: missing date or >8760 hrs. Invalid: negative MTBF.", "Use ONLY 'Valid' rows for MTBF reliability analysis in Minitab"],
            ["MTBF_Comparative_Analysis", "DataQualityNotes", "Explanation of data quality issues", "Text", "Derived", "Applied per row", "'None' means no issues detected"],
            ["", "", "", "", "", "", ""],
            ["Maintenance_KPI_Summary", "AvgMTTRHours", "Average TTR in hours for Finished WOs per asset", "Numeric", "ttr_hours", "Sum(TTR) / Count(Finished WOs with valid non-negative TTR)", ""],
            ["Maintenance_KPI_Summary", "AvgMTBFHours", "Average MTBF in hours per asset", "Numeric", "Derived", "Average of all valid positive MTBF gaps for this asset (end→start between consecutive WOs)", ""],
            ["Maintenance_KPI_Summary", "FailureCount", "Total WOs (Finished + Open) for this asset", "Integer", "Derived", "Count of all WOs by AssetID", ""],
            ["", "", "", "", "", "", ""],
            ["Severity_Validation", "SeverityLevel", "Priority/severity level from imported work order priority", "Numeric", "priority", "Direct from imported work order Priority column for finished and open WOs. Blank when Priority is missing.", ""],
            ["Severity_Validation", "ProductionImpact", "Estimated production impact", "Text", "Derived", "Not calculated — requires operating hours config", "Connect actual production stop data or configure operating hours to enable"],
            ["Severity_Validation", "Layout note", "One row per WO", "Note", "", "Use DowntimeHours as Y variable for boxplot by SeverityLevel, ANOVA by MachineGroup + SeverityLevel", ""],
            ["", "", "", "", "", "", ""],
            ["Criticality_Analysis", "CriticalityGroup", "Standardised criticality group for cross-group comparison", "Text", "Derived from Criticality", "Critical + Semi-Critical → 'Critical'; Facility/Non-Critical unchanged", "GroupingNote column explains mapping"],
            ["Criticality_Analysis", "AvgMTBFHours", "Average MTBF for assets in this group", "Numeric", "Derived", "Average of all valid positive MTBF gaps for assets in this criticality group", "Suitable for ANOVA comparing MTBF by criticality group"],
            ["", "", "", "", "", "", ""],
            ["Missing_Info_Flags", "AnalysisImpact", "Describes how the missing field affects Minitab analysis", "Text", "Derived", "Applied per row based on MissingFields content", "Review and resolve before running Pareto, ANOVA, or MTBF analysis"],
            ["Missing_Info_Flags", "MTBFImpact", "Shows whether the row is excluded from MTBF and why", "Text", "Derived", "Missing AssetID/start/end date/open WO/invalid duration exclude the row from MTBF; missing TTR alone excludes MTTR/TTR only", "Use this column to separate MTTR data quality issues from MTBF data quality issues"],
            ["Missing_Info_Flags", "Missing_WO_ID", "Flag in MissingFields when the imported WO ID is blank", "Flag", "work_order_id / wo_id", "Set when the source work order ID is missing even if Request ID is available as display fallback", "Review before duplicate checks or audit tracing"],
        ];
        const sheet8 = XLSX.utils.aoa_to_sheet(dictRows);
        sheet8["!cols"] = [{ wch: 28 }, { wch: 34 }, { wch: 64 }, { wch: 12 }, { wch: 28 }, { wch: 60 }, { wch: 64 }];

        // ── Build workbook and trigger download ───────────────────────────
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, sheet1, "Maintenance_Raw_Cleaned");
        XLSX.utils.book_append_sheet(wb, sheet2, "Maintenance_KPI_Summary");
        XLSX.utils.book_append_sheet(wb, sheet3, "MTTR_MTBF_Analysis");
        XLSX.utils.book_append_sheet(wb, sheet4, "MTBF_Comparative_Analysis");
        XLSX.utils.book_append_sheet(wb, sheet5, "Severity_Validation");
        XLSX.utils.book_append_sheet(wb, sheet6, "Criticality_Analysis");
        XLSX.utils.book_append_sheet(wb, sheet7, "Missing_Info_Flags");
        XLSX.utils.book_append_sheet(wb, sheet8, "Data_Dictionary");

        const filename = `maintenance_export_${ts}.xlsx`;
        XLSX.writeFile(wb, filename);

        const validMtbf = compData.filter((r) => r[23] === "Valid").length;
        const flagCount = flagData.length;
        const totalWo = finishedRows.length + openWorkOrdersData.length;
        setExportStatus(
            `Export complete: ${filename} — ${totalWo} work orders | ${compData.length} MTBF intervals (${validMtbf} valid) | ${flagCount} flagged records.`,
            "ok"
        );
        console.info(`[Export] ${filename} - Imported WOs: ${finishedRows.length}, Open helper rows: ${openWorkOrdersData.length}, MTBF intervals: ${compData.length} (Valid: ${validMtbf}), Flagged: ${flagCount}`);
    } catch (err) {
        console.error("Export failed:", err);
        setExportStatus(`Export failed: ${err.message}`, "error");
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = "Export"; }
    }
}

// ─────────────────────────────────────────────────────────────────────

async function init() {
    wireDashboardTopicControls();
    wireFilters();
    setSummaryView("criticality");
    setPerformanceView("utilities");
    const period = document.getElementById("period-select")?.value || "ytd";
    const stageSelect = document.getElementById("downtime-stage-filter");
    if (stageSelect) downtimeStageFilter = stageSelect.value || DOWNTIME_STAGE_ALL;
    toggleCustomDateFilter(period);

    const machineExplorerSortEl = document.getElementById("machine-explorer-sort");
    if (machineExplorerSortEl) {
        machineExplorerSortEl.value = machineExplorerSort;
    }
    const machineHistorySortEl = document.getElementById("machine-history-sort");
    if (machineHistorySortEl) {
        machineHistorySortEl.value = machineHistorySort;
    }
    const machineHistoryYearEl = document.getElementById("machine-history-year");
    if (machineHistoryYearEl) {
        machineHistoryYearEl.value = machineHistoryYearFilter;
    }
    const machineHistoryMonthEl = document.getElementById("machine-history-month");
    if (machineHistoryMonthEl) {
        machineHistoryMonthEl.value = machineHistoryMonthFilter;
    }
    const machineHistoryDateFromEl = document.getElementById("machine-history-date-from");
    if (machineHistoryDateFromEl) {
        machineHistoryDateFromEl.value = machineHistoryDateFrom;
    }
    const machineHistoryDateToEl = document.getElementById("machine-history-date-to");
    if (machineHistoryDateToEl) {
        machineHistoryDateToEl.value = machineHistoryDateTo;
    }

    try {
        await Promise.all([
            loadDowntimeCacheFile().then(() => loadDowntimeData(period, "")).catch((error) => {
                console.error("Downtime page load error:", error);
                renderAlerts([{ level: "critical", message: "Downtime data could not be loaded from the current imported work order source." }]);
            }),
            loadWorkOrderImportStatus(),
            loadAssetList(),
            loadOpenWorkOrders(),
        ]);
    } catch (error) {
        console.error("Downtime page load error:", error);
        renderAlerts([{ level: "critical", message: "Downtime data could not be loaded from the current imported work order source." }]);
    }

    // Apply alert context from MIRA Overview action button if present.
    applyMiraAlertContextToDowntime();
}

// ── MIRA Alert Context ────────────────────────────────────────────────────────
// Reads alert context written to sessionStorage by the MIRA Overview action
// buttons, shows a dismissable banner, and focuses the relevant section.

const ALERT_CTX_KEY = "mira_alert_ctx";

function applyMiraAlertContextToDowntime(ctx) {
    try {
        ctx = ctx || JSON.parse(sessionStorage.getItem(ALERT_CTX_KEY) || "null");
    } catch (_) { return; }
    if (!ctx || ctx.page !== "downtime") return;

    showMiraAlertBanner(ctx.alertDescription || "Showing records related to a Daily Action Alert.");

    const focus = ctx.focus || ctx.navFocus;
    if (focus === "machine_explorer") {
        // Scroll the Machine Explorer section into view and optionally pre-set the
        // status filter to highlight open/in-progress MR.
        const machineSection = document.querySelector(".machine-explorer-module, .machine-performance-card");
        if (machineSection) {
            window.setTimeout(() => {
                machineSection.scrollIntoView({ behavior: "smooth", block: "start" });
            }, 350);
        }
        // Pre-select "Open" or "In Progress" status if the alert is for open MR.
        if (ctx.statusFilter === "open") {
            const statusSel = document.getElementById("machine-explorer-status");
            if (statusSel && statusSel.options.length > 1) {
                // Try to find an "Open" or "In Progress" option.
                const openOpt = Array.from(statusSel.options).find(
                    (o) => /^(open|new|in.?progress)$/i.test(o.value.trim())
                );
                if (openOpt) {
                    statusSel.value = openOpt.value;
                    statusSel.dispatchEvent(new Event("change", { bubbles: true }));
                }
            }
        }
        // Apply stage filter from alert context (e.g. "Stage 1" / "Stage 2" / "all").
        if (ctx.stageFilter && ctx.stageFilter !== "all") {
            const stageSel = document.getElementById("downtime-stage-filter");
            if (stageSel) {
                stageSel.value = ctx.stageFilter;
                stageSel.dispatchEvent(new Event("change", { bubbles: true }));
            }
        }
        // If we have a specific asset/area, apply it to the asset search/filter.
        if (ctx.areaOrAsset) {
            const assetSel = document.getElementById("machine-explorer-asset");
            if (assetSel && assetSel.options.length > 1) {
                const match = Array.from(assetSel.options).find(
                    (o) => o.text.toLowerCase().includes(ctx.areaOrAsset.toLowerCase())
                        || ctx.areaOrAsset.toLowerCase().includes(o.text.toLowerCase())
                );
                if (match) {
                    assetSel.value = match.value;
                    assetSel.dispatchEvent(new Event("change", { bubbles: true }));
                }
            }
        }
    } else if (focus === "data_reliability") {
        // Switch to the Data Reliability topic panel in Downtime.
        if (typeof setDashboardTopic === "function") setDashboardTopic("data-reliability");
        if (ctx.stageFilter && ctx.stageFilter !== "all") {
            const stageSel = document.getElementById("downtime-stage-filter");
            if (stageSel) {
                stageSel.value = ctx.stageFilter;
                stageSel.dispatchEvent(new Event("change", { bubbles: true }));
            }
        }
        window.setTimeout(() => {
            const panel = document.getElementById("topic-data-reliability");
            if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
        }, 300);
    } else if (focus === "yearly_movement") {
        // Switch to the Yearly MR Movement topic panel and pre-filter the
        // Carry-over MR Breakdown table to match the alert (previous-year open MR).
        if (typeof setDashboardTopic === "function") setDashboardTopic("yearly-movement");
        // Apply stage filter first so the panel data loads scoped correctly.
        if (ctx.stageFilter && ctx.stageFilter !== "all") {
            const stageSel = document.getElementById("downtime-stage-filter");
            if (stageSel) {
                stageSel.value = ctx.stageFilter;
                stageSel.dispatchEvent(new Event("change", { bubbles: true }));
            }
        }
        const filterValue = ctx.carryoverFilter || "previous_year_open";
        window.setTimeout(() => {
            const filterSel = document.getElementById("mr-carryover-filter");
            if (filterSel) {
                filterSel.value = filterValue;
                filterSel.dispatchEvent(new Event("change", { bubbles: true }));
            }
            // Scroll to the Carry-over MR Breakdown table inside the yearly-movement panel.
            const carryoverCard = document.querySelector(
                "#topic-yearly-movement .mr-carryover-table-card"
            );
            if (carryoverCard) {
                carryoverCard.scrollIntoView({ behavior: "smooth", block: "start" });
            }
        }, 300);
    }
}

function showMiraAlertBanner(message) {
    const banner = document.getElementById("mira-alert-ctx-banner");
    const textEl = document.getElementById("mira-alert-ctx-text");
    const clearBtn = document.getElementById("mira-alert-ctx-clear");
    if (!banner || !textEl) return;
    textEl.textContent = "Showing records related to Daily Action Alert: " + message;
    banner.classList.remove("hidden");
    if (clearBtn && !clearBtn.dataset.bound) {
        clearBtn.dataset.bound = "true";
        clearBtn.addEventListener("click", () => {
            banner.classList.add("hidden");
            try { sessionStorage.removeItem(ALERT_CTX_KEY); } catch (_) {}
            // Reset filters applied by the alert.
            const statusSel = document.getElementById("machine-explorer-status");
            if (statusSel) { statusSel.value = ""; statusSel.dispatchEvent(new Event("change", { bubbles: true })); }
            const assetSel = document.getElementById("machine-explorer-asset");
            if (assetSel) { assetSel.value = ""; assetSel.dispatchEvent(new Event("change", { bubbles: true })); }
            const stageSel = document.getElementById("downtime-stage-filter");
            if (stageSel) { stageSel.value = "all"; stageSel.dispatchEvent(new Event("change", { bubbles: true })); }
            const carryoverSel = document.getElementById("mr-carryover-filter");
            if (carryoverSel) { carryoverSel.value = "all"; carryoverSel.dispatchEvent(new Event("change", { bubbles: true })); }
        });
    }
}

// Handle postMessage from the maintenance.html parent (when Downtime runs in an iframe
// and was already loaded before the alert button was clicked).
window.addEventListener("message", (event) => {
    if (event.origin !== window.location.origin) return;
    if (event.data?.type !== "mira_alert_focus") return;
    try {
        const ctx = JSON.parse(sessionStorage.getItem(ALERT_CTX_KEY) || "null");
        if (ctx && ctx.page === "downtime") applyMiraAlertContextToDowntime(ctx);
    } catch (_) {}
});

function refreshCurrentView(options = {}) {
    const minRefreshGapMs = options.force ? 0 : 60000;
    const now = Date.now();
    if (downtimeRefreshInFlight) return;
    if (lastDowntimeRefreshAt && now - lastDowntimeRefreshAt < minRefreshGapMs) return;
    const period = document.getElementById("period-select")?.value || "ytd";
    const start = period === "custom" ? (document.getElementById("custom-start")?.value || "") : "";
    const end = period === "custom" ? (document.getElementById("custom-end")?.value || "") : "";
    if (period === "custom" && (!start || !end)) return;
    downtimeRefreshInFlight = true;
    lastDowntimeRefreshAt = now;
    Promise.all([
        loadDowntimeData(period, "", start, end),
        loadOpenWorkOrders(),
    ]).catch((error) => {
        console.error("Downtime refresh failed:", error);
    }).finally(() => {
        downtimeRefreshInFlight = false;
    });
}

// ─── Duplicate Work Order Detection ─────────────────────────────────────────

const DUP_WO_GROUP_RENDER_LIMIT = 30;
const DUP_WO_ROWS_PER_GROUP_LIMIT = 20;

function dupNormaliseDesc(desc) {
    return String(desc || "").toLowerCase()
        .replace(/\b(?:no|wo|mr|#|id|ref|request|work order)\s*[\d\-/]+/g, "")
        .replace(/[^\w\s]/g, " ")
        .replace(/\s+/g, " ")
        .trim();
}

function dupNormaliseAsset(assetId) {
    return String(assetId || "").toLowerCase().replace(/\s+/g, "").trim();
}

function dupDateDiffDays(dateA, dateB) {
    if (!dateA || !dateB) return Infinity;
    const a = dateA instanceof Date ? dateA : new Date(dateA);
    const b = dateB instanceof Date ? dateB : new Date(dateB);
    if (isNaN(a.getTime()) || isNaN(b.getTime())) return Infinity;
    return Math.abs((a.getTime() - b.getTime()) / 86400000);
}

function dupFmtDate(d) {
    if (!d) return "";
    const dt = d instanceof Date ? d : new Date(d);
    if (isNaN(dt.getTime())) return String(d);
    return formatDateKey(dt);
}

// Duplicate-detection now flags only Possible Double Entry (data-reliability scope).
// PM scheduling overlaps are surfaced by the Preventive vs Corrective view, and
// unresolved recurring faults are covered by the MTBF / Repeat Failure Pairs KPI.
// PM rows are still excluded from the data-entry signal so PM-scheduler artefacts
// don't get blamed on operators.
function detectDuplicateWorkOrders(rows) {
    const groups = new Map();
    for (const row of rows) {
        const assetKey = dupNormaliseAsset(getMachineAssetId(row));
        const descKey = dupNormaliseDesc(getMrDescription(row));
        if (!assetKey && !descKey) continue;
        const key = `${assetKey}||${descKey}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(row);
    }

    const sameDayGroups = [];

    for (const [, grpRows] of groups) {
        if (grpRows.length < 2) continue;

        // Exclude PM-scheduler duplicates — those belong to the PM view, not data-entry.
        const hasPm = grpRows.some((r) =>
            isPmJobTrade(r.maintenance_job_type || r.job_trade || r.maintenance_type || "")
        );
        if (hasPm) continue;

        // Possible Double Entry — same asset+description raised within ≤1 calendar day.
        const dates = grpRows.map((r) => getMrRaisedDate(r).date).filter(Boolean);
        const dayTimes = dates
            .map((date) => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime())
            .sort((a, b) => a - b);
        for (let i = 1; i < dayTimes.length; i++) {
            if ((dayTimes[i] - dayTimes[i - 1]) <= 86400000) {
                sameDayGroups.push(grpRows);
                break;
            }
        }
    }

    return { sameDayGroups };
}

function renderDupWoGroup(grpRows, action, cardClass) {
    const firstRow = grpRows[0];
    const assetId = getMachineAssetId(firstRow);
    const equipment = getMachineEquipmentName(firstRow) || assetId || "Unknown Asset";
    const desc = getMrDescription(firstRow);

    const dupKey = getDuplicateGroupKey(grpRows);
    dataReviewDuplicateSnapshots[dupKey] = {
        assetName: equipment,
        assetId,
        desc,
        count: grpRows.length,
    };

    const shownRows = grpRows.slice(0, DUP_WO_ROWS_PER_GROUP_LIMIT);
    const hiddenCount = Math.max(0, grpRows.length - shownRows.length);
    const tableRows = shownRows.map((row, i) => renderMachineHistoryRow(row, i)).join("");
    const overflowRow = hiddenCount
        ? `<tr><td colspan="15" class="empty-cell">${fmtNumber(hiddenCount)} more duplicate row${hiddenCount === 1 ? "" : "s"} hidden for performance. Export or narrow filters to review all.</td></tr>`
        : "";

    return `
        <div class="dup-group-card ${cardClass}">
            <div class="dup-group-header">
                <span class="dup-group-asset">${escapeHtml(equipment)}</span>
                ${assetId && assetId !== equipment ? `<span class="dup-group-asset-id">${escapeHtml(assetId)}</span>` : ""}
                <span class="dup-group-badge">${grpRows.length} WOs</span>
                <span class="dup-action-label">${escapeHtml(action)}</span>
                <span class="dr-dup-controls">
                    <button type="button" class="dr-btn dr-btn-edit" data-dr-action="edit-duplicate" data-dr-key="${escapeHtml(dupKey)}">Edit</button>
                    <button type="button" class="dr-btn dr-btn-confirm" data-dr-action="confirm-duplicate" data-dr-key="${escapeHtml(dupKey)}">Confirm reviewed</button>
                </span>
            </div>
            ${desc ? `<div class="dup-group-desc">${escapeHtml(desc)}</div>` : ""}
            <div class="dup-wo-table-wrap">
                <table class="dup-wo-table">
                    <thead>
                        <tr>
                            <th>MR</th>
                            <th>WO</th>
                            <th>Status</th>
                            <th>Svc Level</th>
                            <th>Type</th>
                            <th>Description</th>
                            <th>Translated</th>
                            <th>Started By</th>
                            <th>Created By</th>
                            <th>Created Date</th>
                            <th>Actual Start</th>
                            <th>Actual End</th>
                            <th>Age / Duration</th>
                            <th>Ack Status</th>
                            <th>DQ Flag</th>
                        </tr>
                    </thead>
                    <tbody>${tableRows}${overflowRow}</tbody>
                </table>
            </div>
        </div>`;
}

function getDuplicateCategoryConfig(_category) {
    return {
        key: "sameDay",
        panelId: "dup-sameday-panel",
        countId: "dup-sameday-count",
        action: "Check if WO already existed when filed",
        cardClass: "dup-sameday-card",
        empty: "No possible double entries detected.",
    };
}

function renderDuplicateWoPanel(category) {
    const config = getDuplicateCategoryConfig(category);
    const panel = document.getElementById(config.panelId);
    if (!panel) return;
    const groups = config.key === "sameDay" ? duplicateActiveGroups() : (duplicateWorkOrderGroupsCache[config.key] || []);
    if (!groups.length) {
        panel.innerHTML = `<p class="dup-empty">${escapeHtml(config.empty)}</p>`;
        return;
    }
    const shownGroups = groups.slice(0, DUP_WO_GROUP_RENDER_LIMIT);
    const hiddenCount = Math.max(0, groups.length - shownGroups.length);
    panel.innerHTML = [
        ...shownGroups.map((group) => renderDupWoGroup(group, config.action, config.cardClass)),
        hiddenCount ? `<p class="dup-empty">${fmtNumber(hiddenCount)} more duplicate group${hiddenCount === 1 ? "" : "s"} hidden for performance.</p>` : "",
    ].join("");
}

function resetDuplicateWoPanels() {
    const config = getDuplicateCategoryConfig("same-day");
    const panel = document.getElementById(config.panelId);
    const count = document.getElementById(config.countId);
    if (panel) {
        panel.classList.remove("dup-panel-open");
        panel.innerHTML = "";
    }
    if (count) count.textContent = "0";
    const chevron = document.querySelector(`[data-dup-cat="same-day"] .dup-cat-chevron`);
    if (chevron) chevron.innerHTML = "&#9660;";
}

function wireDuplicateWoToggles(section) {
    section.querySelectorAll(".dup-category-toggle").forEach((btn) => {
        btn.onclick = () => {
            const category = btn.dataset.dupCat || "pm";
            const config = getDuplicateCategoryConfig(category);
            const panel = document.getElementById(config.panelId);
            if (!panel) return;
            const open = panel.classList.toggle("dup-panel-open");
            const chevron = btn.querySelector(".dup-cat-chevron");
            if (chevron) chevron.innerHTML = open ? "&#9650;" : "&#9660;";
            if (open) {
                renderDuplicateWoPanel(category);
            } else {
                panel.innerHTML = "";
            }
        };
    });
}

function renderDuplicateWoResults(groups) {
    const summaryEl = document.getElementById("dup-wo-summary-badges");
    duplicateWorkOrderGroupsCache = {
        sameDay: groups.sameDayGroups || [],
    };

    const total = duplicateActiveGroups().length;
    if (summaryEl) {
        summaryEl.innerHTML = total === 0
            ? `<span class="dup-summary-clean">&#10003; No duplicates detected</span>`
            : `<span class="dup-summary-badge dup-sameday-badge">${fmtNumber(total)} Double Entry</span>`;
    }

    const config = getDuplicateCategoryConfig("same-day");
    const count = document.getElementById(config.countId);
    const panel = document.getElementById(config.panelId);
    if (count) count.textContent = fmtNumber(total);
    if (panel?.classList.contains("dup-panel-open")) {
        renderDuplicateWoPanel("same-day");
    }
}

function renderDuplicateWoSection(rows) {
    const section = document.getElementById("dup-wo-section");
    if (!section) return;

    wireDuplicateWoToggles(section);
    cancelLowPriorityWork(duplicateWoAnalysisHandle);

    const summaryEl = document.getElementById("dup-wo-summary-badges");
    if (!rows) {
        duplicateWoAnalysisToken++;
        duplicateWorkOrderGroupsCache = { pm: [], sameDay: [], recurring: [] };
        resetDuplicateWoPanels();
        if (summaryEl) summaryEl.innerHTML = `<span class="dup-summary-loading">Analysing&hellip;</span>`;
        return;
    }

    const token = ++duplicateWoAnalysisToken;
    duplicateWorkOrderGroupsCache = { pm: [], sameDay: [], recurring: [] };
    resetDuplicateWoPanels();
    if (summaryEl) {
        summaryEl.innerHTML = `<span class="dup-summary-loading">Analysing ${fmtNumber(rows.length)} work order rows&hellip;</span>`;
    }

    duplicateWoAnalysisHandle = scheduleLowPriorityWork(() => {
        duplicateWoAnalysisHandle = null;
        if (token !== duplicateWoAnalysisToken) return;
        renderDuplicateWoResults(detectDuplicateWorkOrders(rows));
    }, 1200);
}

// ─── Data Review: edit / amend / confirm + edit history ──────────────────────
// Confirmed corrections and duplicate resolutions are stored as an override
// layer in localStorage (source Excel / D365 data is never modified). Confirming
// a correction marks the row data-quality valid so it counts toward KPIs and
// drops off the review list. Every change is logged to an edit history that can
// be re-opened to amend a mistake.

const DATA_REVIEW_OVERRIDES_KEY = "downtime.dataReviewOverrides.v1";
const DATA_REVIEW_HISTORY_KEY = "downtime.dataReviewHistory.v1";
const DATA_REVIEW_HISTORY_LIMIT = 500;

const DATA_REVIEW_CORRECTION_FIELDS = [
    { key: "createdDate", label: "Corrected Created / Raised Date", type: "date" },
    { key: "assetId", label: "Corrected Asset ID", type: "text" },
    { key: "assetName", label: "Corrected Asset / Machine Name", type: "text" },
    { key: "slaStart", label: "Corrected SLA Start Date", type: "date" },
    { key: "slaEnd", label: "Corrected SLA End Date", type: "date" },
];

let dataReviewOverrides = loadDataReviewOverrides();
let dataReviewHistory = loadDataReviewHistory();
// In-memory snapshots so modal/handlers can read the current row/group by key.
let dataReviewActionSnapshots = {};
let dataReviewDuplicateSnapshots = {};

function loadDataReviewOverrides() {
    const empty = { corrections: {}, duplicates: {} };
    if (typeof window === "undefined" || !window.localStorage) return empty;
    try {
        const parsed = JSON.parse(window.localStorage.getItem(DATA_REVIEW_OVERRIDES_KEY) || "{}");
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return empty;
        return {
            corrections: (parsed.corrections && typeof parsed.corrections === "object") ? parsed.corrections : {},
            duplicates: (parsed.duplicates && typeof parsed.duplicates === "object") ? parsed.duplicates : {},
        };
    } catch (error) {
        console.warn("Data review overrides could not be loaded:", error);
        return empty;
    }
}

function saveDataReviewOverrides() {
    if (typeof window === "undefined" || !window.localStorage) return;
    try {
        window.localStorage.setItem(DATA_REVIEW_OVERRIDES_KEY, JSON.stringify(dataReviewOverrides));
    } catch (error) {
        console.warn("Data review overrides could not be saved:", error);
    }
}

function loadDataReviewHistory() {
    if (typeof window === "undefined" || !window.localStorage) return [];
    try {
        const parsed = JSON.parse(window.localStorage.getItem(DATA_REVIEW_HISTORY_KEY) || "[]");
        return Array.isArray(parsed) ? parsed : [];
    } catch (error) {
        console.warn("Data review history could not be loaded:", error);
        return [];
    }
}

function saveDataReviewHistory() {
    if (typeof window === "undefined" || !window.localStorage) return;
    try {
        if (dataReviewHistory.length > DATA_REVIEW_HISTORY_LIMIT) {
            dataReviewHistory = dataReviewHistory.slice(0, DATA_REVIEW_HISTORY_LIMIT);
        }
        window.localStorage.setItem(DATA_REVIEW_HISTORY_KEY, JSON.stringify(dataReviewHistory));
    } catch (error) {
        console.warn("Data review history could not be saved:", error);
    }
}

function appendDataReviewHistory(entry) {
    dataReviewHistory.unshift({
        id: `dr-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        ts: new Date().toISOString(),
        ...entry,
    });
    saveDataReviewHistory();
}

function getDataReviewRowKey(row) {
    if (!row || typeof row !== "object") return "";
    const wo = cleanExportIdentifier(row.work_order_id || row.wo_id);
    const req = cleanExportIdentifier(row.maintenance_order_id || row.request_id || row.mr_id || row.mr_no);
    const asset = String(row.asset_id || row.machine_code || "").trim();
    let created = "";
    try {
        const d = getMrRaisedDate(row).date;
        if (d instanceof Date && !Number.isNaN(d.getTime())) created = d.toISOString().slice(0, 10);
    } catch (error) { /* ignore */ }
    return [wo, req, asset, created].filter(Boolean).join("|");
}

function getDuplicateGroupKey(grpRows) {
    const ids = (grpRows || [])
        .map((r) => cleanExportIdentifier(r.work_order_id || r.wo_id) || cleanExportIdentifier(r.request_id || r.maintenance_order_id) || "")
        .filter(Boolean)
        .sort();
    if (ids.length) return `dup|${ids.join(",")}`;
    const first = (grpRows || [])[0] || {};
    return `dup|${String(first.asset_id || "").trim()}|${(getMrDescription(first) || "").slice(0, 40)}`;
}

function dataReviewHasCorrections() {
    return !!(dataReviewOverrides.corrections && Object.keys(dataReviewOverrides.corrections).length);
}

// Override-aware: a confirmed correction makes the row count as data-quality valid.
function dataReviewCorrectionResolvedFor(row) {
    if (!dataReviewHasCorrections()) return false;
    const key = getDataReviewRowKey(row);
    if (!key) return false;
    const ov = dataReviewOverrides.corrections[key];
    return !!(ov && ov.status === "confirmed");
}

function getCorrectionOverride(key) {
    return (key && dataReviewOverrides.corrections[key]) || null;
}

function setCorrectionOverride(key, patch, meta = {}) {
    if (!key) return;
    const prev = getCorrectionOverride(key);
    const next = {
        corrections: { ...(prev?.corrections || {}), ...(patch.corrections || {}) },
        note: patch.note !== undefined ? patch.note : (prev?.note || ""),
        status: patch.status || prev?.status || "edited",
        updatedAt: new Date().toISOString(),
    };
    dataReviewOverrides.corrections[key] = next;
    saveDataReviewOverrides();
    appendDataReviewHistory({
        list: "correction",
        key,
        title: meta.title || key,
        action: patch.status === "confirmed" ? "confirmed" : "edited",
        changes: meta.changes || [],
        note: next.note,
        prevSnapshot: prev ? JSON.parse(JSON.stringify(prev)) : null,
    });
}

function getDuplicateOverride(key) {
    return (key && dataReviewOverrides.duplicates[key]) || null;
}

function setDuplicateOverride(key, patch, meta = {}) {
    if (!key) return;
    const prev = getDuplicateOverride(key);
    const next = {
        resolution: patch.resolution || prev?.resolution || "Confirmed reviewed",
        note: patch.note !== undefined ? patch.note : (prev?.note || ""),
        status: patch.status || prev?.status || "edited",
        updatedAt: new Date().toISOString(),
    };
    dataReviewOverrides.duplicates[key] = next;
    saveDataReviewOverrides();
    appendDataReviewHistory({
        list: "duplicate",
        key,
        title: meta.title || key,
        action: patch.status === "confirmed" ? "confirmed" : "edited",
        changes: meta.changes || [{ field: "Resolution", from: prev?.resolution || "—", to: next.resolution }],
        note: next.note,
        prevSnapshot: prev ? JSON.parse(JSON.stringify(prev)) : null,
    });
}

function revertDataReviewHistoryEntry(historyId) {
    const entry = dataReviewHistory.find((h) => h.id === historyId);
    if (!entry) return;
    const store = entry.list === "duplicate" ? dataReviewOverrides.duplicates : dataReviewOverrides.corrections;
    if (entry.prevSnapshot) {
        store[entry.key] = JSON.parse(JSON.stringify(entry.prevSnapshot));
    } else {
        delete store[entry.key];
    }
    saveDataReviewOverrides();
    appendDataReviewHistory({
        list: entry.list,
        key: entry.key,
        title: entry.title,
        action: "reverted",
        changes: [{ field: "Reverted change from", from: fmtDataReviewTimestamp(entry.ts), to: "previous state" }],
        note: "",
        prevSnapshot: null,
    });
    refreshDataReviewViews();
}

function fmtDataReviewTimestamp(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "--";
    return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

// ── Modal ────────────────────────────────────────────────────────────────────

function ensureDataReviewModal() {
    let modal = document.getElementById("dr-edit-modal");
    if (modal) return modal;
    modal = document.createElement("div");
    modal.id = "dr-edit-modal";
    modal.className = "dr-modal-overlay";
    modal.hidden = true;
    modal.innerHTML = `
        <div class="dr-modal" role="dialog" aria-modal="true" aria-labelledby="dr-modal-title">
            <div class="dr-modal-head">
                <h3 id="dr-modal-title">Edit record</h3>
                <button type="button" class="dr-modal-close" data-dr-action="modal-close" aria-label="Close">&times;</button>
            </div>
            <p class="dr-modal-context" id="dr-modal-context"></p>
            <div class="dr-modal-body" id="dr-modal-body"></div>
            <div class="dr-modal-actions">
                <button type="button" class="dr-btn dr-btn-ghost" data-dr-action="modal-close">Cancel</button>
                <button type="button" class="dr-btn dr-btn-save" data-dr-action="modal-save">Save edit</button>
                <button type="button" class="dr-btn dr-btn-confirm" data-dr-action="modal-confirm">Confirm &amp; use for KPI</button>
            </div>
        </div>`;
    document.body.appendChild(modal);
    return modal;
}

let dataReviewModalContext = null;

function openCorrectionModal(key) {
    const snap = dataReviewActionSnapshots[key];
    if (!snap) return;
    const ov = getCorrectionOverride(key);
    const modal = ensureDataReviewModal();
    dataReviewModalContext = { type: "correction", key };
    modal.querySelector("#dr-modal-title").textContent = "Amend record for KPI use";
    modal.querySelector("#dr-modal-context").innerHTML =
        `<strong>${escapeHtml(snap.mrId || "--")}</strong> · ${escapeHtml(snap.assetName || "--")} `
        + `<span class="dr-flag-chip">${escapeHtml((snap.flags || []).join("; ") || "Review")}</span>`;
    const fieldsHtml = DATA_REVIEW_CORRECTION_FIELDS.map((f) => {
        const current = (ov?.corrections?.[f.key] ?? "") || dataReviewOriginalFieldValue(f.key, snap);
        return `<label class="dr-field"><span>${escapeHtml(f.label)}</span>`
            + `<input type="${f.type}" data-dr-field="${f.key}" value="${escapeHtml(String(current || ""))}"></label>`;
    }).join("");
    modal.querySelector("#dr-modal-body").innerHTML = fieldsHtml
        + `<label class="dr-field dr-field-wide"><span>Reviewer Note</span>`
        + `<textarea data-dr-field="note" rows="2" placeholder="What was corrected and why">${escapeHtml(ov?.note || "")}</textarea></label>`;
    modal.querySelector(`[data-dr-action="modal-confirm"]`).hidden = false;
    modal.hidden = false;
}

function dataReviewOriginalFieldValue(fieldKey, snap) {
    if (fieldKey === "createdDate") return snap.createdISO || "";
    if (fieldKey === "assetId") return snap.assetId || "";
    if (fieldKey === "assetName") return snap.assetName || "";
    return "";
}

function openDuplicateModal(key) {
    const snap = dataReviewDuplicateSnapshots[key];
    if (!snap) return;
    const ov = getDuplicateOverride(key);
    const modal = ensureDataReviewModal();
    dataReviewModalContext = { type: "duplicate", key };
    modal.querySelector("#dr-modal-title").textContent = "Resolve duplicate group";
    modal.querySelector("#dr-modal-context").innerHTML =
        `<strong>${escapeHtml(snap.assetName || "--")}</strong> · ${escapeHtml(snap.count)} work orders `
        + (snap.desc ? `<span class="dr-flag-chip">${escapeHtml(snap.desc)}</span>` : "");
    const options = ["Confirmed reviewed", "Not a duplicate — keep all", "Duplicate confirmed — merge", "Ignore"];
    const chosen = ov?.resolution || "Confirmed reviewed";
    modal.querySelector("#dr-modal-body").innerHTML =
        `<label class="dr-field dr-field-wide"><span>Resolution</span><select data-dr-field="resolution">`
        + options.map((o) => `<option value="${escapeHtml(o)}"${o === chosen ? " selected" : ""}>${escapeHtml(o)}</option>`).join("")
        + `</select></label>`
        + `<label class="dr-field dr-field-wide"><span>Reviewer Note</span>`
        + `<textarea data-dr-field="note" rows="2" placeholder="Decision reasoning">${escapeHtml(ov?.note || "")}</textarea></label>`;
    modal.querySelector(`[data-dr-action="modal-confirm"]`).hidden = false;
    modal.hidden = false;
}

function readDataReviewModalFields() {
    const modal = document.getElementById("dr-edit-modal");
    const out = {};
    modal.querySelectorAll("[data-dr-field]").forEach((el) => {
        out[el.getAttribute("data-dr-field")] = el.value.trim();
    });
    return out;
}

function closeDataReviewModal() {
    const modal = document.getElementById("dr-edit-modal");
    if (modal) modal.hidden = true;
    dataReviewModalContext = null;
}

function saveDataReviewModal(confirm) {
    if (!dataReviewModalContext) return;
    const fields = readDataReviewModalFields();
    if (dataReviewModalContext.type === "correction") {
        const key = dataReviewModalContext.key;
        const snap = dataReviewActionSnapshots[key] || {};
        const prev = getCorrectionOverride(key);
        const corrections = {};
        const changes = [];
        DATA_REVIEW_CORRECTION_FIELDS.forEach((f) => {
            const val = fields[f.key] || "";
            if (val) corrections[f.key] = val;
            const from = (prev?.corrections?.[f.key] ?? "") || dataReviewOriginalFieldValue(f.key, snap) || "—";
            if (String(val) !== String(prev?.corrections?.[f.key] ?? dataReviewOriginalFieldValue(f.key, snap) ?? "")) {
                changes.push({ field: f.label, from: from || "—", to: val || "—" });
            }
        });
        setCorrectionOverride(key, {
            corrections,
            note: fields.note || "",
            status: confirm ? "confirmed" : "edited",
        }, { title: snap.mrId || key, changes });
    } else if (dataReviewModalContext.type === "duplicate") {
        const key = dataReviewModalContext.key;
        const snap = dataReviewDuplicateSnapshots[key] || {};
        setDuplicateOverride(key, {
            resolution: fields.resolution || "Confirmed reviewed",
            note: fields.note || "",
            status: confirm ? "confirmed" : "edited",
        }, { title: snap.assetName || key });
    }
    closeDataReviewModal();
    refreshDataReviewViews();
}

// ── History panel ────────────────────────────────────────────────────────────

function renderDataReviewHistoryPanel() {
    const body = document.getElementById("dr-history-body");
    if (!body) return;
    if (!dataReviewHistory.length) {
        body.innerHTML = `<p class="dr-history-empty">No edits yet. Confirmed corrections and duplicate resolutions will be logged here.</p>`;
        return;
    }
    body.innerHTML = dataReviewHistory.slice(0, 100).map((h) => {
        const actionClass = h.action === "confirmed" ? "dr-hist-confirm" : h.action === "reverted" ? "dr-hist-revert" : "dr-hist-edit";
        const changesHtml = (h.changes || []).length
            ? `<ul class="dr-hist-changes">${h.changes.map((c) => `<li>${escapeHtml(c.field)}: <span class="dr-hist-from">${escapeHtml(String(c.from))}</span> &rarr; <span class="dr-hist-to">${escapeHtml(String(c.to))}</span></li>`).join("")}</ul>`
            : "";
        const noteHtml = h.note ? `<div class="dr-hist-note">&ldquo;${escapeHtml(h.note)}&rdquo;</div>` : "";
        const listLabel = h.list === "duplicate" ? "Duplicate" : "Correction";
        const canAmend = h.action !== "reverted";
        return `
            <div class="dr-hist-item">
                <div class="dr-hist-row">
                    <span class="dr-hist-badge ${actionClass}">${escapeHtml(h.action)}</span>
                    <span class="dr-hist-list">${escapeHtml(listLabel)}</span>
                    <span class="dr-hist-title">${escapeHtml(h.title || h.key || "")}</span>
                    <span class="dr-hist-time">${escapeHtml(fmtDataReviewTimestamp(h.ts))}</span>
                </div>
                ${changesHtml}
                ${noteHtml}
                <div class="dr-hist-actions">
                    ${canAmend ? `<button type="button" class="dr-btn dr-btn-mini" data-dr-action="amend-history" data-dr-id="${escapeHtml(h.id)}" data-dr-list="${escapeHtml(h.list)}" data-dr-key="${escapeHtml(h.key)}">Re-amend</button>` : ""}
                    ${canAmend ? `<button type="button" class="dr-btn dr-btn-mini dr-btn-ghost" data-dr-action="revert-history" data-dr-id="${escapeHtml(h.id)}">Undo</button>` : ""}
                </div>
            </div>`;
    }).join("");
}

// ── Duplicate active-group helpers ───────────────────────────────────────────

function duplicateActiveGroups() {
    const groups = duplicateWorkOrderGroupsCache.sameDay || [];
    if (!dataReviewOverrides.duplicates || !Object.keys(dataReviewOverrides.duplicates).length) return groups;
    return groups.filter((g) => {
        const ov = dataReviewOverrides.duplicates[getDuplicateGroupKey(g)];
        return !(ov && ov.status === "confirmed");
    });
}

function refreshDuplicateWoCounts() {
    const total = duplicateActiveGroups().length;
    const summaryEl = document.getElementById("dup-wo-summary-badges");
    if (summaryEl) {
        summaryEl.innerHTML = total === 0
            ? `<span class="dup-summary-clean">&#10003; No duplicates detected</span>`
            : `<span class="dup-summary-badge dup-sameday-badge">${fmtNumber(total)} Double Entry</span>`;
    }
    const count = document.getElementById("dup-sameday-count");
    if (count) count.textContent = fmtNumber(total);
    const panel = document.getElementById("dup-sameday-panel");
    if (panel && panel.classList.contains("dup-panel-open")) renderDuplicateWoPanel("same-day");
}

function refreshDataReviewViews() {
    try {
        if (typeof downtimeOverviewRowsCache !== "undefined" && Array.isArray(downtimeOverviewRowsCache) && downtimeOverviewRowsCache.length) {
            renderDowntimeOverviewFromRows(downtimeOverviewRowsCache);
        } else {
            renderTopicDataReliabilityPanel();
        }
    } catch (error) {
        console.warn("Data review view refresh failed:", error);
    }
    refreshDuplicateWoCounts();
    renderDataReviewHistoryPanel();
}

// ── Click handling (delegated, attached once) ────────────────────────────────

function handleDataReviewClick(event) {
    const btn = event.target.closest("[data-dr-action]");
    if (!btn) return;
    const action = btn.getAttribute("data-dr-action");
    const key = btn.getAttribute("data-dr-key");
    if (action === "modal-close") { closeDataReviewModal(); return; }
    if (action === "modal-save") { saveDataReviewModal(false); return; }
    if (action === "modal-confirm") { saveDataReviewModal(true); return; }
    if (action === "edit-correction") { openCorrectionModal(key); return; }
    if (action === "confirm-correction") {
        const snap = dataReviewActionSnapshots[key] || {};
        setCorrectionOverride(key, { status: "confirmed" }, { title: snap.mrId || key, changes: [{ field: "Status", from: "Needs correction", to: "Confirmed for KPI" }] });
        refreshDataReviewViews();
        return;
    }
    if (action === "edit-duplicate") { openDuplicateModal(key); return; }
    if (action === "confirm-duplicate") {
        const snap = dataReviewDuplicateSnapshots[key] || {};
        setDuplicateOverride(key, { status: "confirmed" }, { title: snap.assetName || key });
        refreshDataReviewViews();
        return;
    }
    if (action === "amend-history") {
        const list = btn.getAttribute("data-dr-list");
        if (list === "duplicate") openDuplicateModal(key); else openCorrectionModal(key);
        return;
    }
    if (action === "revert-history") {
        revertDataReviewHistoryEntry(btn.getAttribute("data-dr-id"));
        return;
    }
}

if (typeof document !== "undefined") {
    document.addEventListener("click", handleDataReviewClick);
}

// ─── Key Downtime Indicators (KDI) ───────────────────────────────────────────
// Standalone year/month-filtered MTTR and MTBF cards in the Key Downtime
// Indicators section. These are independent of the main period-select and
// draw from the full MTBF history payload (when loaded) for cross-year access.

let kdiMttrCriticalityFilter = "";
let kdiMtbfCriticalityFilter = "";
let kdiMttrSelectedGroup = "";
let kdiMtbfSelectedGroup = "";
let kdiMttrRankMode = "group";
let kdiMtbfRankMode = "group";
let kdiMttrDrilldownMachineGroup = "";
let kdiMtbfDrilldownMachineGroup = "";
let kdiCurrentMttrData = null;
let kdiCurrentMtbfData = null;

// ── Critical S1 vs S2 MTTR comparison (Section B of the MTTR card) ───────────
// S1 = single-machine critical group (asset_count === 1).
// S2 = multi-machine critical group (asset_count > 1).
// This sub-section has NO stage selector — it follows the page-level Stage filter
// (getSelectedDowntimeStage) and the page-level Category filter, exactly like the
// rest of the Downtime / MTTR page. Its own FY selector drives the time axis.
let kdiCritCmpFy = "";            // selected financial-year start (string)
let kdiCritCmpFy2 = "";           // "Compare with" FY start, "" = none
let kdiCritCmpType = "both";      // both | s1 | s2
let kdiCritCmpMetric = "average"; // average | median
let kdiCritCmpCategory = "all";   // all | Production Equipment | Utilities

// Auto-YTD: pick the latest year present in the loaded work orders. Falls back
// to the current calendar year when no dated rows are available. This means the
// indicator section automatically rolls over when imports for a new year begin.
function kdiGetAutoYtdYear(rows) {
    const today = new Date();
    let maxYear = today.getFullYear();
    if (Array.isArray(rows)) {
        for (const wo of rows) {
            const raw = wo?.start_time || wo?.actual_start_time || wo?.actual_start
                || wo?.request_created_time || wo?.created_date || wo?.maintenance_start_time;
            if (!raw) continue;
            const yearStr = String(raw).slice(0, 4);
            const year = Number(yearStr);
            if (Number.isFinite(year) && year > maxYear) maxYear = year;
        }
    }
    return String(maxYear);
}

function kdiGetSelectedYear() {
    let allWos = (typeof getAllWorkOrdersForMtbf === "function") ? getAllWorkOrdersForMtbf() : [];
    if (!allWos.length && typeof getWorkOrderRows === "function") {
        allWos = getWorkOrderRows(typeof getManagement === "function" ? getManagement() : null);
    }
    return kdiGetAutoYtdYear(allWos);
}

function kdiGetSelectedMonth() {
    // Indicator section is YTD by design — no month scoping.
    return "";
}

function kdiNormalizeSearchTerm(value) {
    return String(value || "").trim().toLowerCase();
}

function kdiAssetMatchesSearch(entry, searchTerm) {
    const normalizedSearch = kdiNormalizeSearchTerm(searchTerm);
    if (!normalizedSearch) return true;
    const assetName = kdiNormalizeSearchTerm(entry?.assetName);
    const assetId = kdiNormalizeSearchTerm(entry?.assetId);
    return assetName.includes(normalizedSearch) || assetId.includes(normalizedSearch);
}

function kdiPopulateMachineGroupSelect(selectId, entries = [], selectedValue = "") {
    const select = document.getElementById(selectId);
    if (!select) return "";
    const categories = [...new Set(entries.map((entry) => String(entry?.group || "").trim()).filter(Boolean))]
        .sort((a, b) => a.localeCompare(b));
    const nextValue = categories.includes(selectedValue) ? selectedValue : "";
    select.innerHTML = `<option value="">All Categories</option>` +
        categories.map((cat) => `<option value="${escapeHtml(cat)}">${escapeHtml(cat)}</option>`).join("");
    select.value = nextValue;
    return nextValue;
}

function kdiFilterWorkOrders(wos, year, month) {
    if (!year && !month) return wos;
    return wos.filter((wo) => {
        const t = String(wo.start_time || wo.actual_start_time || wo.actual_start || "").trim();
        if (!t) return false;
        if (year && month) return t.startsWith(`${year}-${month}`);
        if (year) return t.startsWith(year);
        // month-only: match any year but specific month slice
        return t.length >= 7 && t.slice(5, 7) === month;
    });
}

function kdiNormalizeCriticality(raw) {
    const n = normalizeClassification(raw);
    if (PRODUCTION_CRITICAL_LABELS.has(n)) return "Critical";
    if (NON_PRODUCTION_LABELS.has(n)) return "Non-Critical / Facility";
    return "Unclassified";
}

function kdiGetAssetCriticality(wo, assetLookup) {
    const meta = getAssetMetaFromLookup(assetLookup, wo.asset_id);
    const raw = String(meta?.criticality || wo.criticality || wo.normalized_criticality || wo.raw_criticality || "").trim();

    // If the asset master has already assigned a real criticality, use it.
    if (raw && raw !== "Non-Critical / Facility" && raw !== "Non-Critical") {
        return kdiNormalizeCriticality(raw);
    }

    // Fall back to machine-group-name classification.
    // The backend currently sets all rows to "Non-Critical / Facility" as a
    // generic default when no explicit criticality column exists in the MR file.
    // We override that by looking at the machine group (derived from Asset_Master
    // "Main Asset Group") which does carry the production/facility distinction.
    const machineGroup = String(
        meta?.machine_name || meta?.group || wo.machine_group || wo.equipment_name || ""
    ).trim();
    if (CRITICAL_MACHINE_GROUPS.has(machineGroup)) return "Critical";
    if (machineGroup === "Facility / Building" || machineGroup === "Unknown / Review") return "Non-Critical / Facility";

    // If the raw value was the generic Non-Critical default from the backend,
    // propagate it now (better than Unclassified for known-mapped assets).
    if (raw) return kdiNormalizeCriticality(raw);
    return "Unclassified";
}

// ── MTTR ─────────────────────────────────────────────────────────────────────

function kdiComputeMttrMetrics(wos, assetLookup) {
    let totalHours = 0;
    let validCount = 0;
    let missingCount = 0;
    const validHours = [];
    const assetMap = new Map();

    wos.forEach((wo) => {
        const assetId = String(wo.asset_id || wo.equipment_id || "").trim() || "Missing Asset";
        if (!assetMap.has(assetId)) {
            const meta = getAssetMetaFromLookup(assetLookup, wo.asset_id);
            // Prefer the per-asset friendly name from Asset_Master.xlsx,
            // then the machine-group name, then whatever the work-order export
            // carried, and only finally the asset ID. machine_name (group) is
            // kept in `group` below for the group-by view.
            assetMap.set(assetId, {
                assetId,
                assetName: String(meta?.asset_name || meta?.machine_name || wo.machine_name || wo.asset_name || wo.equipment_name || assetId).trim(),
                group: String(meta?.machine_name || wo.machine_group || wo.equipment_name || "Unclassified").trim(),
                assetMachineGroup: String(meta?.asset_machine_group || wo.asset_machine_group || "").trim(),
                criticality: kdiGetAssetCriticality(wo, assetLookup),
                hours: [],
                totalDowntimeHours: 0,
                workOrderCount: 0,
                lastFailureDate: null,
                missingCount: 0,
            });
        }
        const entry = assetMap.get(assetId);
        entry.workOrderCount++;
        const failureDate = parseDateValue(wo.actual_start_time || wo.actual_start || wo.maintenance_start_time || wo.start_time);
        if (failureDate && (!entry.lastFailureDate || failureDate > entry.lastFailureDate)) {
            entry.lastFailureDate = failureDate;
        }
        const h = getTtrHours(wo);
        if (h !== null && Number.isFinite(h) && h >= 0) {
            totalHours += h;
            validCount++;
            validHours.push(h);
            entry.hours.push(h);
            entry.totalDowntimeHours += h;
        } else {
            missingCount++;
            entry.missingCount++;
        }
    });

    return {
        averageMttr: validCount > 0 ? totalHours / validCount : null,
        medianMttr: median(validHours),
        validCount,
        missingCount,
        totalDowntimeHours: totalHours,
        assetMap,
    };
}

function kdiBuildMttrAssetRows(assetMap, critFilter, selectedGroup = "") {
    return [...assetMap.values()]
        .filter((entry) => (!critFilter || entry.criticality === critFilter) && (!selectedGroup || entry.group === selectedGroup))
        .map((entry) => ({
            assetId: entry.assetId,
            assetName: entry.assetName,
            group: entry.group,
            criticality: entry.criticality,
            woCount: entry.hours.length,
            totalWoCount: entry.workOrderCount || entry.hours.length + entry.missingCount,
            avgMttr: entry.hours.length > 0 ? entry.hours.reduce((sum, value) => sum + value, 0) / entry.hours.length : null,
            highestMttr: entry.hours.length > 0 ? Math.max(...entry.hours) : null,
            totalDowntimeHours: entry.totalDowntimeHours || 0,
            lastFailureDate: entry.lastFailureDate || null,
            missingCount: entry.missingCount,
        }))
        .filter((entry) => entry.woCount > 0 || entry.missingCount > 0)
        .sort((a, b) => (b.avgMttr || 0) - (a.avgMttr || 0));
}

function kdiGroupMttrByMachineGroup(assetMap, critFilter) {
    const groups = new Map();
    assetMap.forEach((entry) => {
        if (critFilter && entry.criticality !== critFilter) return;
        const key = entry.group;
        if (!groups.has(key)) {
            groups.set(key, { machineName: key, criticality: entry.criticality, hours: [], assetIds: new Set(), missingCount: 0, totalDowntimeHours: 0 });
        }
        const g = groups.get(key);
        g.assetIds.add(entry.assetId);
        g.hours.push(...entry.hours);
        g.missingCount += entry.missingCount;
        g.totalDowntimeHours += entry.totalDowntimeHours || 0;
        if (!g.criticality || g.criticality === "Unclassified") g.criticality = entry.criticality;
    });
    return [...groups.values()]
        .map((g) => ({
            machineName: g.machineName,
            criticality: g.criticality || "Unclassified",
            woCount: g.hours.length,
            assetCount: g.assetIds.size,
            avgMttr: g.hours.length > 0 ? g.hours.reduce((a, b) => a + b, 0) / g.hours.length : null,
            medianMttr: median(g.hours),
            highestMttr: g.hours.length > 0 ? Math.max(...g.hours) : null,
            totalDowntimeHours: g.totalDowntimeHours,
            missingCount: g.missingCount,
        }))
        .filter((g) => g.woCount > 0 || g.missingCount > 0)
        .sort((a, b) => (b.avgMttr || 0) - (a.avgMttr || 0));
}

function kdiGroupMttrByMachineGroupLabel(assetMap, critFilter, categoryFilter = "") {
    const groups = new Map();
    assetMap.forEach((entry) => {
        if (critFilter && entry.criticality !== critFilter) return;
        if (categoryFilter && entry.group !== categoryFilter) return;
        const key = entry.assetMachineGroup || "Unclassified";
        if (!groups.has(key)) {
            groups.set(key, { machineName: key, category: entry.group, criticality: entry.criticality, hours: [], assetIds: new Set(), missingCount: 0, totalDowntimeHours: 0 });
        }
        const g = groups.get(key);
        g.assetIds.add(entry.assetId);
        g.hours.push(...entry.hours);
        g.missingCount += entry.missingCount;
        g.totalDowntimeHours += entry.totalDowntimeHours || 0;
        if (!g.criticality || g.criticality === "Unclassified") g.criticality = entry.criticality;
    });
    return [...groups.values()]
        .map((g) => ({
            machineName: g.machineName,
            criticality: g.criticality || "Unclassified",
            woCount: g.hours.length,
            assetCount: g.assetIds.size,
            avgMttr: g.hours.length > 0 ? g.hours.reduce((a, b) => a + b, 0) / g.hours.length : null,
            medianMttr: median(g.hours),
            highestMttr: g.hours.length > 0 ? Math.max(...g.hours) : null,
            totalDowntimeHours: g.totalDowntimeHours,
            missingCount: g.missingCount,
        }))
        .filter((g) => g.woCount > 0 || g.missingCount > 0)
        .sort((a, b) => (b.avgMttr || 0) - (a.avgMttr || 0));
}

function kdiGroupMtbfByMachineGroupLabel(allAssets, critFilter, categoryFilter = "") {
    const groups = new Map();
    allAssets.forEach((entry) => {
        if (critFilter && entry.criticality !== critFilter) return;
        if (categoryFilter && entry.group !== categoryFilter) return;
        const key = entry.assetMachineGroup || "Unclassified";
        if (!groups.has(key)) {
            groups.set(key, { machineName: key, criticality: entry.criticality, avgMtbfs: [], minMtbfs: [], maxMtbfs: [], assetCount: 0, assetsWithMtbf: 0, woCount: 0, gapCount: 0, insufficientCount: 0 });
        }
        const g = groups.get(key);
        g.assetCount++;
        g.woCount += entry.woCount || 0;
        if (entry.hasMtbf) {
            g.assetsWithMtbf++;
            g.avgMtbfs.push(entry.avgMtbf);
            g.minMtbfs.push(entry.minMtbf);
            g.maxMtbfs.push(entry.maxMtbf);
            g.gapCount += entry.gapCount;
        } else {
            g.insufficientCount++;
        }
        if (!g.criticality || g.criticality === "Unclassified") g.criticality = entry.criticality;
    });
    return [...groups.values()]
        .map((g) => ({
            machineName: g.machineName,
            criticality: g.criticality || "Unclassified",
            assetCount: g.assetCount,
            assetsWithMtbf: g.assetsWithMtbf,
            woCount: g.woCount,
            gapCount: g.gapCount,
            avgMtbf: g.avgMtbfs.length > 0 ? g.avgMtbfs.reduce((a, b) => a + b, 0) / g.avgMtbfs.length : null,
            lowestMtbf: g.minMtbfs.length > 0 ? Math.min(...g.minMtbfs) : null,
            highestMtbf: g.maxMtbfs.length > 0 ? Math.max(...g.maxMtbfs) : null,
            insufficientCount: g.insufficientCount,
        }))
        .filter((g) => g.avgMtbf !== null)
        .sort((a, b) => (a.avgMtbf || 0) - (b.avgMtbf || 0));
}

function kdiRenderMttrGroupChart(groups, assetRows = [], selectedGroup = "", rankMode = "group") {
    const id = "kdiMttrGroupChart";
    const canvas = ensureCanvas(id);
    const title = document.getElementById("kdi-mttr-chart-title");
    if (!canvas) return;
    destroyChart(id);
    const groupRows = selectedGroup ? groups.filter((row) => row.machineName === selectedGroup) : groups;
    const usingAssets = rankMode === "asset";
    const usingMachineGroup = rankMode === "machine-group";
    const valid = (usingAssets ? assetRows.filter((row) => row.avgMttr !== null) : groupRows.filter((g) => g.avgMttr !== null)).slice(0, 14);
    if (title) {
        const suffix = selectedGroup ? ` — ${selectedGroup}` : "";
        title.textContent = usingAssets
            ? `MTTR by Asset / Machine Name${suffix} — highest first`
            : usingMachineGroup
                ? `MTTR by Machine Group${suffix} — highest first`
                : `MTTR by Machine Category${suffix} — highest first`;
    }
    setChartContainerHeight(id, valid.length * 32 + 72);
    if (!valid.length) { renderEmptyChart(id, "No MTTR data for the selected filters"); return; }
    const labels = valid.map((row) => usingAssets ? (row.assetName || row.assetId || "--") : row.machineName);
    const data = valid.map((row) => Number(row.avgMttr || 0));
    const colors = data.map((v) => v > 48 ? "#ef4444" : v > 24 ? "#f59e0b" : "#10b981");
    chartRefs[id] = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: { labels, datasets: [{ data, backgroundColor: colors, borderRadius: 6 }] },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            indexAxis: "y",
            onClick: (_event, elements) => {
                if (usingAssets || !elements.length) return;
                const row = valid[elements[0].index];
                if (row?.machineName) kdiOpenMttrGroupDrilldown(row.machineName, rankMode);
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => `Avg MTTR: ${fmtHours(ctx.raw)}`,
                        afterLabel: (ctx) => usingAssets
                            ? `${valid[ctx.dataIndex].assetId || "--"} | ${valid[ctx.dataIndex].woCount} WO(s)`
                            : `${valid[ctx.dataIndex].woCount} WO(s)`,
                    },
                },
            },
            scales: {
                x: {
                    beginAtZero: true,
                    grid: { color: "#e2e8f0" },
                    ticks: { callback: (v) => fmtAxisHours(v) },
                    title: { display: true, text: "Avg MTTR (hrs)" },
                },
                y: {
                    grid: { display: false },
                    ticks: {
                        autoSkip: false,
                        padding: 6,
                        font: { size: 11, weight: "600" },
                    },
                },
            },
        },
    });
}

function kdiRenderMttrSummaryTable(groups, assetRows = [], selectedGroup = "", rankMode = "group") {
    const tbody = document.getElementById("kdi-mttr-group-table-body");
    const title = document.getElementById("kdi-mttr-summary-title");
    const head = document.getElementById("kdi-mttr-summary-head");
    const searchInput = document.getElementById("kdi-mttr-group-search");
    if (!tbody) return;
    const search = kdiNormalizeSearchTerm(searchInput?.value || "");
    const usingAssets = rankMode === "asset";
    const usingMachineGroup = rankMode === "machine-group";
    const groupRows = selectedGroup ? groups.filter((row) => row.machineName === selectedGroup) : groups;
    const suffix = selectedGroup ? ` — ${selectedGroup}` : "";
    if (title) {
        title.textContent = usingAssets
            ? `MTTR Summary by Asset / Machine Name${suffix}`
            : usingMachineGroup
                ? `MTTR Summary by Machine Group${suffix}`
                : `MTTR Summary by Machine Category${suffix}`;
    }
    const dimLabel = usingMachineGroup ? "Machine Group" : "Machine Category";
    if (head) {
        head.innerHTML = usingAssets
            ? "<th>Asset Name</th><th>Asset ID</th><th>Criticality</th><th>WO Count</th><th>Avg MTTR</th><th>Total Downtime</th><th>Missing / Invalid</th>"
            : `<th>${dimLabel}</th><th>MTTR</th><th>WO Count</th><th>Total Downtime</th><th>Assets Affected</th><th>Missing / Invalid</th>`;
    }
    if (searchInput) {
        searchInput.placeholder = usingAssets ? "Search assets..." : `Search ${dimLabel.toLowerCase()}s…`;
        searchInput.setAttribute("aria-label", usingAssets ? "Search assets" : `Search ${dimLabel.toLowerCase()}s`);
    }
    const visible = usingAssets
        ? (search
            ? assetRows.filter((row) => kdiAssetMatchesSearch(row, search) || kdiNormalizeSearchTerm(row.criticality).includes(search))
            : assetRows)
        : (search
            ? groupRows.filter((row) => kdiNormalizeSearchTerm(row.machineName).includes(search) || kdiNormalizeSearchTerm(row.criticality).includes(search))
            : groupRows);
    if (!visible.length) {
        tbody.innerHTML = `<tr><td colspan="${usingAssets ? 7 : 6}" class="empty-cell">${search ? `No ${usingAssets ? "assets" : "groups"} match the search.` : "No MTTR data for the selected filters."}</td></tr>`;
        return;
    }
    tbody.innerHTML = usingAssets
        ? visible.map((row) => `
            <tr>
                <td>${escapeHtml(row.assetName || "--")}</td>
                <td>${escapeHtml(row.assetId || "--")}</td>
                <td>${escapeHtml(row.criticality)}</td>
                <td class="kdi-number-cell">${fmtNumber(row.woCount)}</td>
                <td class="kdi-number-cell">${row.avgMttr !== null ? fmtHours(row.avgMttr) : "--"}</td>
                <td class="kdi-number-cell">${row.totalDowntimeHours ? fmtDaysHours(row.totalDowntimeHours) : "--"}</td>
                <td class="kdi-number-cell">${fmtNumber(row.missingCount)}</td>
            </tr>
        `).join("")
        : visible.map((row, index) => `
            <tr class="kdi-drill-row${index === 0 ? " kdi-attention-row" : ""}" data-kdi-group="${escapeHtml(row.machineName)}" tabindex="0" title="Click to view assets in ${escapeHtml(row.machineName)}">
                <td>${escapeHtml(row.machineName)}</td>
                <td class="kdi-number-cell">${row.avgMttr !== null ? fmtHours(row.avgMttr) : "--"}</td>
                <td class="kdi-number-cell">${fmtNumber(row.woCount)}</td>
                <td class="kdi-number-cell">${row.totalDowntimeHours ? fmtDaysHours(row.totalDowntimeHours) : "--"}</td>
                <td class="kdi-number-cell">${fmtNumber(row.assetCount)}</td>
                <td class="kdi-number-cell">${fmtNumber(row.missingCount)}</td>
            </tr>
        `).join("");
}

function kdiGetLaterDate(a, b) {
    if (!a) return b || null;
    if (!b) return a || null;
    return a > b ? a : b;
}

function kdiBuildCombinedAssetRows(critFilter = "", selectedGroup = "", sortMode = "mttr", machineGroupFilter = "") {
    const rowsById = new Map();
    const ensureRow = (entry) => {
        const key = String(entry?.assetId || entry?.assetName || "").trim() || `${entry?.group || "Unclassified"}:${rowsById.size}`;
        if (!rowsById.has(key)) {
            rowsById.set(key, {
                assetId: entry?.assetId || "--",
                assetName: entry?.assetName || entry?.assetId || "--",
                group: entry?.group || "Unclassified",
                assetMachineGroup: entry?.assetMachineGroup || "",
                criticality: entry?.criticality || "Unclassified",
                woCount: 0,
                mttrWoCount: 0,
                mtbfWoCount: 0,
                avgMttr: null,
                avgMtbf: null,
                totalDowntimeHours: 0,
                lastFailureDate: null,
                repeatFailureCount: 0,
            });
        }
        const row = rowsById.get(key);
        if ((!row.assetName || row.assetName === row.assetId) && entry?.assetName) row.assetName = entry.assetName;
        if (!row.group || row.group === "Unclassified") row.group = entry?.group || row.group;
        if (!row.assetMachineGroup && entry?.assetMachineGroup) row.assetMachineGroup = entry.assetMachineGroup;
        if ((!row.criticality || row.criticality === "Unclassified") && entry?.criticality) row.criticality = entry.criticality;
        return row;
    };
    const includeEntry = (entry) =>
        (!critFilter || entry?.criticality === critFilter) &&
        (!selectedGroup || entry?.group === selectedGroup) &&
        (!machineGroupFilter || (entry?.assetMachineGroup || "") === machineGroupFilter);

    kdiCurrentMttrData?.assetMap?.forEach((entry) => {
        if (!includeEntry(entry)) return;
        const row = ensureRow(entry);
        row.mttrWoCount = entry.hours?.length || 0;
        row.woCount = Math.max(row.woCount, entry.workOrderCount || row.mttrWoCount + (entry.missingCount || 0));
        row.avgMttr = row.mttrWoCount > 0 ? entry.hours.reduce((sum, value) => sum + value, 0) / row.mttrWoCount : null;
        row.totalDowntimeHours = entry.totalDowntimeHours || 0;
        row.lastFailureDate = kdiGetLaterDate(row.lastFailureDate, entry.lastFailureDate || null);
    });

    (kdiCurrentMtbfData?.allAssets || []).forEach((entry) => {
        if (!includeEntry(entry)) return;
        const row = ensureRow(entry);
        row.mtbfWoCount = entry.woCount || 0;
        row.woCount = Math.max(row.woCount, row.mtbfWoCount);
        row.avgMtbf = entry.avgMtbf ?? null;
        row.repeatFailureCount = entry.gapCount || 0;
        row.lastFailureDate = kdiGetLaterDate(row.lastFailureDate, entry.lastFailureDate || null);
    });

    return [...rowsById.values()]
        .filter((row) => row.woCount > 0 || row.mttrWoCount > 0 || row.mtbfWoCount > 0)
        .sort((a, b) => {
            if (sortMode === "mtbf") {
                if (a.avgMtbf === null && b.avgMtbf !== null) return 1;
                if (a.avgMtbf !== null && b.avgMtbf === null) return -1;
                if (a.avgMtbf !== null && b.avgMtbf !== null && a.avgMtbf !== b.avgMtbf) return a.avgMtbf - b.avgMtbf;
                return (b.repeatFailureCount || 0) - (a.repeatFailureCount || 0);
            }
            if (a.avgMttr === null && b.avgMttr !== null) return 1;
            if (a.avgMttr !== null && b.avgMttr === null) return -1;
            if (a.avgMttr !== null && b.avgMttr !== null && a.avgMttr !== b.avgMttr) return b.avgMttr - a.avgMttr;
            return (b.totalDowntimeHours || 0) - (a.totalDowntimeHours || 0);
        });
}

function kdiRenderCombinedAssetDrilldown(tbodyId, groupSelectId, searchId, critFilter = "", selectedGroup = "", sortMode = "mttr", isMachineGroupMode = false) {
    const tbody = document.getElementById(tbodyId);
    const groupSelect = document.getElementById(groupSelectId);
    if (!tbody) return;
    const availableRows = kdiBuildCombinedAssetRows(critFilter, "", sortMode);
    if (groupSelect) {
        const values = isMachineGroupMode
            ? [...new Set(availableRows.map((row) => row.assetMachineGroup).filter(Boolean))].sort((a, b) => a.localeCompare(b))
            : [...new Set(availableRows.map((row) => row.group).filter(Boolean))].sort((a, b) => a.localeCompare(b));
        const prev = groupSelect.value;
        const preferred = selectedGroup && values.includes(selectedGroup) ? selectedGroup : prev;
        const allLabel = isMachineGroupMode ? "All Machine Groups" : "All Groups";
        groupSelect.innerHTML = `<option value="">${allLabel}</option>` +
            values.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
        groupSelect.value = values.includes(preferred) ? preferred : "";
    }
    const activeFilter = groupSelect?.value || "";
    const drilldownSearch = kdiNormalizeSearchTerm(document.getElementById(searchId)?.value || "");
    const rows = kdiBuildCombinedAssetRows(
        critFilter,
        isMachineGroupMode ? "" : activeFilter,
        sortMode,
        isMachineGroupMode ? activeFilter : ""
    ).filter((row) => !drilldownSearch || kdiAssetMatchesSearch(row, drilldownSearch) || kdiNormalizeSearchTerm(row.group).includes(drilldownSearch));
    if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="8" class="empty-cell">${drilldownSearch ? "No assets match the search." : "No data for the selected filter."}</td></tr>`;
        return;
    }
    tbody.innerHTML = rows.map((row) => `
        <tr>
            <td>${escapeHtml(row.assetId || "--")}</td>
            <td>${escapeHtml(row.assetName || "--")}</td>
            <td class="kdi-number-cell">${row.avgMttr !== null ? fmtHours(row.avgMttr) : "--"}</td>
            <td class="kdi-number-cell">${row.avgMtbf !== null ? fmtDaysHours(row.avgMtbf) : "--"}</td>
            <td class="kdi-number-cell">${fmtNumber(row.woCount)}</td>
            <td class="kdi-number-cell">${row.totalDowntimeHours ? fmtDaysHours(row.totalDowntimeHours) : "--"}</td>
            <td>${row.lastFailureDate ? escapeHtml(fmtDateOnly(row.lastFailureDate)) : "--"}</td>
            <td class="kdi-number-cell">${fmtNumber(row.repeatFailureCount)}</td>
        </tr>
    `).join("");
}

function kdiRenderMttrAssetDrilldown(assetMap, critFilter, groupOrMgFilter = "") {
    const isMachineGroupMode = kdiMttrRankMode === "machine-group";
    kdiRenderCombinedAssetDrilldown("kdi-mttr-asset-table-body", "kdi-mttr-asset-group-filter", "kdi-mttr-asset-search", critFilter, groupOrMgFilter, "mttr", isMachineGroupMode);
}

// ── MTBF ─────────────────────────────────────────────────────────────────────

function kdiComputeMtbfMetrics(wos, assetLookup) {
    const byAsset = new Map();

    wos.forEach((wo) => {
        const assetId = String(wo.asset_id || "").trim();
        if (!assetId || assetId.toUpperCase() === "WO-ASSET") return;
        if (isMtbfGeneralAreaWo(wo)) return;   // exclude general area/location placeholders
        const start = parseDateValue(wo.actual_start_time || wo.actual_start || wo.maintenance_start_time || wo.start_time);
        if (!start) return;
        // STRICT: no fallback to start — must have an actual end for end-to-start MTBF.
        const end = parseDateValue(wo.actual_end_time || wo.actual_end || wo.maintenance_end_time || wo.end_time);
        if (!byAsset.has(assetId)) {
            const meta = getAssetMetaFromLookup(assetLookup, assetId);
            // Prefer the per-asset friendly name from Asset_Master.xlsx
            // (same precedence rule as the MTTR computation above).
            byAsset.set(assetId, {
                assetId,
                assetName: String(meta?.asset_name || meta?.machine_name || wo.machine_name || wo.asset_name || wo.equipment_name || assetId).trim(),
                group: String(meta?.machine_name || wo.machine_group || wo.equipment_name || "Unclassified").trim(),
                assetMachineGroup: String(meta?.asset_machine_group || wo.asset_machine_group || "").trim(),
                criticality: kdiGetAssetCriticality(wo, assetLookup),
                wos: [],
                lastFailureDate: null,
            });
        }
        const entry = byAsset.get(assetId);
        if (start && (!entry.lastFailureDate || start > entry.lastFailureDate)) {
            entry.lastFailureDate = start;
        }
        // Only store WO if we have both start and a valid actual end (strict end-to-start).
        if (end) entry.wos.push({ _start: start, _end: end });
    });

    let totalMtbfHours = 0;
    let assetsWithMtbf = 0;
    let totalGaps = 0;
    let assetsInsufficient = 0;
    const assetResults = [];
    const allAssets = [];

    byAsset.forEach((entry) => {
        const sorted = [...entry.wos].sort((a, b) => a._start - b._start);
        const gaps = [];
        for (let i = 1; i < sorted.length; i++) {
            // Previous WO must have an actual end (_end was only stored when valid).
            // Gap = next Actual Start − previous Actual End (end-to-start, no fallback).
            const gapHrs = (sorted[i]._start.getTime() - sorted[i - 1]._end.getTime()) / 3600000;
            if (gapHrs > MTBF_MIN_GAP_HOURS) gaps.push(gapHrs);   // exclude zero/negative and ≤ 1 min
        }
        if (gaps.length > 0) {
            const avg = gaps.reduce((a, b) => a + b, 0) / gaps.length;
            totalMtbfHours += avg;
            totalGaps += gaps.length;
            assetsWithMtbf++;
            const result = {
                ...entry,
                avgMtbf: avg,
                minMtbf: Math.min(...gaps),
                maxMtbf: Math.max(...gaps),
                gapCount: gaps.length,
                woCount: sorted.length,
                hasMtbf: true,
            };
            assetResults.push(result);
            allAssets.push(result);
        } else {
            assetsInsufficient++;
            allAssets.push({ ...entry, avgMtbf: null, minMtbf: null, maxMtbf: null, gapCount: 0, woCount: sorted.length, hasMtbf: false });
        }
    });

    return {
        overallMtbf: assetsWithMtbf > 0 ? totalMtbfHours / assetsWithMtbf : null,
        assetsWithMtbf,
        totalGaps,
        assetsInsufficient,
        assetResults,
        allAssets,
    };
}

function kdiBuildMtbfAssetRows(allAssets, critFilter, selectedGroup = "") {
    return allAssets
        .filter((entry) => entry.hasMtbf && (!critFilter || entry.criticality === critFilter) && (!selectedGroup || entry.group === selectedGroup))
        .map((entry) => ({
            assetId: entry.assetId,
            assetName: entry.assetName,
            group: entry.group,
            criticality: entry.criticality,
            woCount: entry.woCount,
            gapCount: entry.gapCount,
            avgMtbf: entry.avgMtbf,
            minMtbf: entry.minMtbf,
            maxMtbf: entry.maxMtbf,
            lastFailureDate: entry.lastFailureDate || null,
        }))
        .sort((a, b) => (a.avgMtbf || 0) - (b.avgMtbf || 0));
}

function kdiGroupMtbfByMachineGroup(allAssets, critFilter) {
    const groups = new Map();
    allAssets.forEach((entry) => {
        if (critFilter && entry.criticality !== critFilter) return;
        const key = entry.group;
        if (!groups.has(key)) {
            groups.set(key, { machineName: key, criticality: entry.criticality, avgMtbfs: [], minMtbfs: [], maxMtbfs: [], assetCount: 0, assetsWithMtbf: 0, woCount: 0, gapCount: 0, insufficientCount: 0 });
        }
        const g = groups.get(key);
        g.assetCount++;
        g.woCount += entry.woCount || 0;
        if (entry.hasMtbf) {
            g.assetsWithMtbf++;
            g.avgMtbfs.push(entry.avgMtbf);
            g.minMtbfs.push(entry.minMtbf);
            g.maxMtbfs.push(entry.maxMtbf);
            g.gapCount += entry.gapCount;
        } else {
            g.insufficientCount++;
        }
        if (!g.criticality || g.criticality === "Unclassified") g.criticality = entry.criticality;
    });
    return [...groups.values()]
        .map((g) => ({
            machineName: g.machineName,
            criticality: g.criticality || "Unclassified",
            assetCount: g.assetCount,
            assetsWithMtbf: g.assetsWithMtbf,
            woCount: g.woCount,
            gapCount: g.gapCount,
            avgMtbf: g.avgMtbfs.length > 0 ? g.avgMtbfs.reduce((a, b) => a + b, 0) / g.avgMtbfs.length : null,
            lowestMtbf: g.minMtbfs.length > 0 ? Math.min(...g.minMtbfs) : null,
            highestMtbf: g.maxMtbfs.length > 0 ? Math.max(...g.maxMtbfs) : null,
            insufficientCount: g.insufficientCount,
        }))
        .filter((g) => g.avgMtbf !== null)
        .sort((a, b) => (a.avgMtbf || 0) - (b.avgMtbf || 0));
}

function kdiRenderMtbfGroupChart(groups, assetRows = [], selectedGroup = "", rankMode = "group") {
    const id = "kdiMtbfGroupChart";
    const canvas = ensureCanvas(id);
    const title = document.getElementById("kdi-mtbf-chart-title");
    if (!canvas) return;
    destroyChart(id);
    const groupRows = selectedGroup ? groups.filter((row) => row.machineName === selectedGroup) : groups;
    const usingAssets = rankMode === "asset";
    const usingMachineGroup = rankMode === "machine-group";
    const valid = (usingAssets ? assetRows : groupRows).slice(0, 14);
    if (title) {
        const suffix = selectedGroup ? ` — ${selectedGroup}` : "";
        title.textContent = usingAssets
            ? `MTBF by Asset / Machine Name${suffix} — lowest first (needs most attention)`
            : usingMachineGroup
                ? `MTBF by Machine Group${suffix} — lowest first (needs most attention)`
                : `MTBF by Machine Category${suffix} — lowest first (needs most attention)`;
    }
    setChartContainerHeight(id, valid.length * 32 + 72);
    if (!valid.length) { renderEmptyChart(id, "No MTBF data for the selected filters"); return; }
    const labels = valid.map((row) => usingAssets ? (row.assetName || row.assetId || "--") : row.machineName);
    const dataHours = valid.map((row) => Number(row.avgMtbf || 0));
    const dataDays = dataHours.map((h) => Math.round((h / 24) * 10) / 10);
    const colors = dataHours.map((v) => v < 168 ? "#ef4444" : v < 720 ? "#f59e0b" : "#16a34a");
    chartRefs[id] = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: { labels, datasets: [{ data: dataDays, backgroundColor: colors, borderRadius: 6 }] },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            indexAxis: "y",
            onClick: (_event, elements) => {
                if (usingAssets || !elements.length) return;
                const row = valid[elements[0].index];
                if (row?.machineName) kdiOpenMtbfGroupDrilldown(row.machineName, rankMode);
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => `Avg MTBF: ${fmtDaysHours(dataHours[ctx.dataIndex])}`,
                        afterLabel: (ctx) => usingAssets
                            ? `${valid[ctx.dataIndex].assetId || "--"} | ${valid[ctx.dataIndex].gapCount} repeat failure pair(s)`
                            : `${valid[ctx.dataIndex].assetCount} asset(s), ${valid[ctx.dataIndex].gapCount} repeat failure pair(s)`,
                    },
                },
            },
            scales: {
                x: {
                    beginAtZero: true,
                    grid: { color: "#e2e8f0" },
                    ticks: { callback: (v) => `${v}d` },
                    title: { display: true, text: "Avg MTBF (days)" },
                },
                y: {
                    grid: { display: false },
                    ticks: {
                        autoSkip: false,
                        padding: 6,
                        font: { size: 11, weight: "600" },
                    },
                },
            },
        },
    });
}

function kdiRenderMtbfSummaryTable(groups, assetRows = [], selectedGroup = "", rankMode = "group") {
    const tbody = document.getElementById("kdi-mtbf-group-table-body");
    const title = document.getElementById("kdi-mtbf-summary-title");
    const head = document.getElementById("kdi-mtbf-summary-head");
    const searchInput = document.getElementById("kdi-mtbf-group-search");
    if (!tbody) return;
    const search = kdiNormalizeSearchTerm(searchInput?.value || "");
    const usingAssets = rankMode === "asset";
    const usingMachineGroup = rankMode === "machine-group";
    const groupRows = selectedGroup ? groups.filter((row) => row.machineName === selectedGroup) : groups;
    const suffix = selectedGroup ? ` — ${selectedGroup}` : "";
    if (title) {
        title.textContent = usingAssets
            ? `MTBF Summary by Asset / Machine Name${suffix}`
            : usingMachineGroup
                ? `MTBF Summary by Machine Group${suffix}`
                : `MTBF Summary by Machine Category${suffix}`;
    }
    const dimLabel = usingMachineGroup ? "Machine Group" : "Machine Category";
    if (head) {
        head.innerHTML = usingAssets
            ? "<th>Asset Name</th><th>Asset ID</th><th>Criticality</th><th>WO Count</th><th>Repeat Failures</th><th>Avg MTBF</th><th>Lowest MTBF</th><th>Highest MTBF</th>"
            : `<th>${dimLabel}</th><th>MTBF</th><th>Failure / WO Count</th><th>Assets Included</th><th>Repeat Failure Count</th><th>Lowest MTBF</th><th>Insufficient Data</th>`;
    }
    if (searchInput) {
        searchInput.placeholder = usingAssets ? "Search assets..." : `Search ${dimLabel.toLowerCase()}s…`;
        searchInput.setAttribute("aria-label", usingAssets ? "Search assets" : `Search ${dimLabel.toLowerCase()}s`);
    }
    const visible = usingAssets
        ? (search
            ? assetRows.filter((row) => kdiAssetMatchesSearch(row, search) || kdiNormalizeSearchTerm(row.criticality).includes(search))
            : assetRows)
        : (search
            ? groupRows.filter((row) => kdiNormalizeSearchTerm(row.machineName).includes(search) || kdiNormalizeSearchTerm(row.criticality).includes(search))
            : groupRows);
    if (!visible.length) {
        tbody.innerHTML = `<tr><td colspan="${usingAssets ? 8 : 7}" class="empty-cell">${search ? `No ${usingAssets ? "assets" : "groups"} match the search.` : "No MTBF data for the selected filters."}</td></tr>`;
        return;
    }
    tbody.innerHTML = usingAssets
        ? visible.map((row) => `
            <tr>
                <td>${escapeHtml(row.assetName || "--")}</td>
                <td>${escapeHtml(row.assetId || "--")}</td>
                <td>${escapeHtml(row.criticality)}</td>
                <td class="kdi-number-cell">${fmtNumber(row.woCount)}</td>
                <td class="kdi-number-cell">${fmtNumber(row.gapCount)}</td>
                <td class="kdi-number-cell">${row.avgMtbf !== null ? fmtDaysHours(row.avgMtbf) : "--"}</td>
                <td class="kdi-number-cell">${row.minMtbf !== null ? fmtDaysHours(row.minMtbf) : "--"}</td>
                <td class="kdi-number-cell">${row.maxMtbf !== null ? fmtDaysHours(row.maxMtbf) : "--"}</td>
            </tr>
        `).join("")
        : visible.map((row, index) => `
            <tr class="kdi-drill-row${index === 0 ? " kdi-attention-row" : ""}" data-kdi-group="${escapeHtml(row.machineName)}" tabindex="0" title="Click to view assets in ${escapeHtml(row.machineName)}">
                <td>${escapeHtml(row.machineName)}</td>
                <td class="kdi-number-cell">${row.avgMtbf !== null ? fmtDaysHours(row.avgMtbf) : "--"}</td>
                <td class="kdi-number-cell">${fmtNumber(row.gapCount)} / ${fmtNumber(row.woCount)}</td>
                <td class="kdi-number-cell">${fmtNumber(row.assetsWithMtbf)}</td>
                <td class="kdi-number-cell">${fmtNumber(row.gapCount)}</td>
                <td class="kdi-number-cell">${row.lowestMtbf !== null ? fmtDaysHours(row.lowestMtbf) : "--"}</td>
                <td class="kdi-number-cell">${fmtNumber(row.insufficientCount)}</td>
            </tr>
        `).join("");
}

function kdiRenderMtbfAdditionalKpis(groups, assetRows, selectedGroup = "", rankMode = "group") {
    const container = document.getElementById("kdi-mtbf-additional-kpis");
    if (!container) return;
    const groupRows = selectedGroup ? groups.filter((row) => row.machineName === selectedGroup) : groups;
    const usingAssets = rankMode === "asset";
    if (!(usingAssets ? assetRows.length : groupRows.length)) { container.innerHTML = ""; return; }
    if (usingAssets) {
        const lowestAsset = assetRows[0];
        const mostFrequentAsset = [...assetRows].sort((a, b) => (b.gapCount || 0) - (a.gapCount || 0))[0];
        container.innerHTML = `
            <div class="kdi-mtbf-kpi-strip">
                <div class="kdi-mtbf-kpi-item">
                    <div class="kdi-mtbf-kpi-label">Lowest MTBF Asset</div>
                    <div class="kdi-mtbf-kpi-value">${escapeHtml(lowestAsset.assetName || lowestAsset.assetId || "--")}</div>
                    <div class="kdi-mtbf-kpi-sub">${fmtDaysHours(lowestAsset.avgMtbf)} avg MTBF${lowestAsset.assetId ? ` | ${escapeHtml(lowestAsset.assetId)}` : ""}</div>
                </div>
                <div class="kdi-mtbf-kpi-item">
                    <div class="kdi-mtbf-kpi-label">Most Frequent Repeat Failures</div>
                    <div class="kdi-mtbf-kpi-value">${escapeHtml(mostFrequentAsset.assetName || mostFrequentAsset.assetId || "--")}</div>
                    <div class="kdi-mtbf-kpi-sub">${fmtNumber(mostFrequentAsset.gapCount)} failure pair(s)</div>
                </div>
                <div class="kdi-mtbf-kpi-item">
                    <div class="kdi-mtbf-kpi-label">Assets with Repeat Failures</div>
                    <div class="kdi-mtbf-kpi-value">${fmtNumber(assetRows.length)}</div>
                    <div class="kdi-mtbf-kpi-sub">${selectedGroup ? `Within ${escapeHtml(selectedGroup)}` : "Across all selected groups"}</div>
                </div>
            </div>
        `;
        return;
    }
    const lowestGroup = groupRows[0];
    const mostFrequent = [...groupRows].sort((a, b) => (b.gapCount || 0) - (a.gapCount || 0))[0];
    container.innerHTML = `
        <div class="kdi-mtbf-kpi-strip">
            <div class="kdi-mtbf-kpi-item">
                <div class="kdi-mtbf-kpi-label">Lowest MTBF Group</div>
                <div class="kdi-mtbf-kpi-value">${escapeHtml(lowestGroup.machineName)}</div>
                <div class="kdi-mtbf-kpi-sub">${fmtDaysHours(lowestGroup.avgMtbf)} avg MTBF</div>
            </div>
            <div class="kdi-mtbf-kpi-item">
                <div class="kdi-mtbf-kpi-label">Most Frequent Repeat Failures</div>
                <div class="kdi-mtbf-kpi-value">${escapeHtml(mostFrequent.machineName)}</div>
                <div class="kdi-mtbf-kpi-sub">${fmtNumber(mostFrequent.gapCount)} failure pair(s)</div>
            </div>
            <div class="kdi-mtbf-kpi-item">
                <div class="kdi-mtbf-kpi-label">Assets with Repeat Failures</div>
                <div class="kdi-mtbf-kpi-value">${fmtNumber(assetRows.length)}</div>
                <div class="kdi-mtbf-kpi-sub">Assets with 2+ work orders</div>
            </div>
        </div>
    `;
}

function kdiRenderMtbfAssetDrilldown(assetResults, critFilter, groupOrMgFilter = "") {
    const isMachineGroupMode = kdiMtbfRankMode === "machine-group";
    kdiRenderCombinedAssetDrilldown("kdi-mtbf-asset-table-body", "kdi-mtbf-asset-group-filter", "kdi-mtbf-asset-search", critFilter, groupOrMgFilter, "mtbf", isMachineGroupMode);
}

// ── Shared helpers ────────────────────────────────────────────────────────────

function kdiGetMttrStatusBadge(avgMttr, validCount, missingCount) {
    if (validCount === 0) return { label: "Unknown — no valid records", cls: "kdi-status-unknown" };
    const missingRatio = missingCount / (validCount + missingCount);
    if (missingRatio > 0.5) return { label: "Attention: high missing data", cls: "kdi-status-warn" };
    if (avgMttr === null) return { label: "Unknown", cls: "kdi-status-unknown" };
    return { label: "", cls: "" };
}

function kdiGetMtbfStatusBadge(overallMtbf, assetsWithMtbf) {
    if (assetsWithMtbf === 0) return { label: "Unknown — insufficient data", cls: "kdi-status-unknown" };
    if (overallMtbf === null) return { label: "Unknown", cls: "kdi-status-unknown" };
    if (overallMtbf < 168) return { label: "Attention: frequent repeat failures", cls: "kdi-status-warn" };
    if (overallMtbf < 720) return { label: "Moderate", cls: "kdi-status-moderate" };
    return { label: "Good", cls: "kdi-status-good" };
}

function kdiPeriodLabel(year, month) {
    const monthNames = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    if (!year && !month) return "All available data";
    if (year && month) return `${monthNames[parseInt(month, 10)] || month} ${year}`;
    if (year) return `Year ${year}`;
    return `All years — ${monthNames[parseInt(month, 10)] || month} only`;
}

function kdiRenderCriticalityBtns(containerId, selected, onSelect) {
    const el = document.getElementById(containerId);
    if (!el) return;
    const options = [
        { label: "All", value: "" },
        { label: "Critical", value: "Critical" },
        { label: "Non-Critical / Facility", value: "Non-Critical / Facility" },
        { label: "Unclassified", value: "Unclassified" },
    ];
    el.innerHTML = options.map((o) => `
        <button type="button" class="kdi-criticality-btn${o.value === selected ? " active" : ""}" data-crit="${escapeHtml(o.value)}">${escapeHtml(o.label)}</button>
    `).join("");
    el.querySelectorAll(".kdi-criticality-btn").forEach((btn) => {
        btn.addEventListener("click", () => onSelect(btn.dataset.crit));
    });
}

function kdiRenderRankToggle(containerId, selectedMode, onSelect) {
    const el = document.getElementById(containerId);
    if (!el) return;
    el.querySelectorAll("[data-kdi-rank]").forEach((btn) => {
        const active = btn.dataset.kdiRank === selectedMode;
        btn.classList.toggle("active", active);
        btn.setAttribute("aria-pressed", active ? "true" : "false");
        btn.onclick = () => {
            const nextMode = btn.dataset.kdiRank || "group";
            if (nextMode === selectedMode) return;
            onSelect(nextMode);
        };
    });
}

// ── Main KDI update ───────────────────────────────────────────────────────────

function updateKdiSection() {
    const year = kdiGetSelectedYear();
    const month = kdiGetSelectedMonth();

    let allWos = getAllWorkOrdersForMtbf();
    if (!allWos.length) allWos = getWorkOrderRows(getManagement());

    const filtered = kdiFilterWorkOrders(allWos, year, month);
    const assetLookup = buildAssetListLookup();

    // Compute and store metrics
    const mttrData = kdiComputeMttrMetrics(filtered, assetLookup);
    kdiCurrentMttrData = { ...mttrData, assetLookup };

    const mtbfData = kdiComputeMtbfMetrics(filtered, assetLookup);
    kdiCurrentMtbfData = { ...mtbfData, assetLookup };

    const periodLabel = kdiPeriodLabel(year, month);
    setText("kdi-ytd-pill", year ? `YTD ${year}` : "YTD");

    // ── Render MTTR card ──
    const mttrStatus = kdiGetMttrStatusBadge(mttrData.averageMttr, mttrData.validCount, mttrData.missingCount);
    const mttrDisplay = mttrData.averageMttr !== null ? fmtHours(mttrData.averageMttr) : "No valid records";
    setText("kdi-mttr-value", mttrDisplay);
    setText("kdi-summary-mttr-value", mttrDisplay);
    // Days-equivalent secondary line — keeps the hours-and-minutes headline accurate
    // while giving operators a quick "≈ N days" reading at a glance.
    const mttrDaysEl = document.getElementById("kdi-mttr-days");
    if (mttrDaysEl) {
        if (mttrData.averageMttr !== null && Number(mttrData.averageMttr) >= 24) {
            mttrDaysEl.textContent = `≈ ${fmtDaysHours(mttrData.averageMttr)}`;
            mttrDaysEl.classList.remove("kdi-secondary-value-empty");
        } else {
            mttrDaysEl.innerHTML = "&nbsp;";
            mttrDaysEl.classList.add("kdi-secondary-value-empty");
        }
    }
    setText("kdi-mttr-wo-count", fmtNumber(mttrData.validCount));
    setText("kdi-summary-work-orders", fmtNumber(mttrData.validCount));
    setText("kdi-mttr-missing-count", fmtNumber(mttrData.missingCount));
    setText("kdi-summary-missing-invalid", fmtNumber(mttrData.missingCount));
    setText("kdi-mttr-median", mttrData.medianMttr !== null ? fmtHours(mttrData.medianMttr) : "--");
    setText("kdi-summary-mttr-median", mttrData.medianMttr !== null ? fmtHours(mttrData.medianMttr) : "--");
    setText("kdi-summary-total-downtime", mttrData.totalDowntimeHours ? fmtDaysHours(mttrData.totalDowntimeHours) : "--");
    setText("kdi-mttr-period-label", periodLabel);
    setText("kdi-summary-period-label", periodLabel);
    const mttrBadge = document.getElementById("kdi-mttr-status-badge");
    if (mttrBadge) {
        const showBadge = Boolean(mttrStatus?.label);
        mttrBadge.textContent = showBadge ? mttrStatus.label : "";
        mttrBadge.className = `kdi-status-badge ${showBadge ? mttrStatus.cls : "kdi-status-unknown"}`;
        mttrBadge.classList.toggle("hidden", !showBadge);
    }

    // ── Render MTBF card ──
    const mtbfStatus = kdiGetMtbfStatusBadge(mtbfData.overallMtbf, mtbfData.assetsWithMtbf);
    const mtbfDisplay = mtbfData.overallMtbf !== null ? fmtDaysHours(mtbfData.overallMtbf) : "Insufficient data";
    setText("kdi-mtbf-value", mtbfDisplay);
    setText("kdi-summary-mtbf-value", mtbfDisplay);
    setText("kdi-mtbf-valid-assets", fmtNumber(mtbfData.assetsWithMtbf));
    setText("kdi-summary-mtbf-assets", fmtNumber(mtbfData.assetsWithMtbf));
    setText("kdi-mtbf-repeat-count", fmtNumber(mtbfData.totalGaps));
    setText("kdi-summary-repeat-pairs", fmtNumber(mtbfData.totalGaps));
    setText("kdi-mtbf-insufficient-count", fmtNumber(mtbfData.assetsInsufficient));
    setText("kdi-summary-mtbf-insufficient", fmtNumber(mtbfData.assetsInsufficient));
    setText("kdi-mtbf-period-label", periodLabel);
    const mtbfBadge = document.getElementById("kdi-mtbf-status-badge");
    if (mtbfBadge) { mtbfBadge.textContent = mtbfStatus.label; mtbfBadge.className = `kdi-status-badge ${mtbfStatus.cls}`; }

    // Machine-group analysis is the default management view, so keep both panels fresh.
    updateKdiMttrAnalysis();
    updateKdiMtbfAnalysis();
    // Critical S1 vs S2 comparison uses all-year rows (its own FY selector scopes
    // the year), so feed it the unfiltered all-year source rather than `filtered`.
    updateKdiCritCmpSection(allWos);
}

function updateKdiMttrAnalysis() {
    if (!kdiCurrentMttrData) return;
    const { assetMap } = kdiCurrentMttrData;
    const crit = kdiMttrCriticalityFilter;
    const availableAssets = [...assetMap.values()].filter((entry) => !crit || entry.criticality === crit);
    kdiMttrSelectedGroup = kdiPopulateMachineGroupSelect("kdi-mttr-machine-group-filter", availableAssets, kdiMttrSelectedGroup);
    const groups = kdiMttrRankMode === "machine-group"
        ? kdiGroupMttrByMachineGroupLabel(assetMap, crit, kdiMttrSelectedGroup)
        : kdiGroupMttrByMachineGroup(assetMap, crit);
    const assetRows = kdiBuildMttrAssetRows(assetMap, crit, kdiMttrSelectedGroup);
    kdiRenderCriticalityBtns("kdi-mttr-criticality-filter", crit, (v) => { kdiMttrCriticalityFilter = v; updateKdiMttrAnalysis(); });
    kdiRenderRankToggle("kdi-mttr-rank-toggle", kdiMttrRankMode, (mode) => { kdiMttrRankMode = mode; updateKdiMttrAnalysis(); });
    kdiRenderMttrGroupChart(groups, assetRows, kdiMttrSelectedGroup, kdiMttrRankMode);
    kdiRenderMttrSummaryTable(groups, assetRows, kdiMttrSelectedGroup, kdiMttrRankMode);
    const dd = document.getElementById("kdi-mttr-asset-drilldown");
    if (dd?.open) kdiRenderMttrAssetDrilldown(assetMap, crit, kdiMttrRankMode === "machine-group" ? kdiMttrDrilldownMachineGroup : kdiMttrSelectedGroup);
}

// ── Critical S1 vs S2 MTTR comparison (Section B of the MTTR card) ───────────
// No stage selector here: the comparison follows the page-level Stage filter
// (getSelectedDowntimeStage) and the page-level Category filter. Its own FY
// selector drives the time axis (the global Year/Month filter does not further
// constrain this chart, since comparing two financial years needs its own
// year control).

// Build the S1 / S2 critical asset-id sets from the Asset_Master mapping.
// NOTE: the /api/asset-list "machines" are top-level Main-Asset-Group categories
// (Production Equipment, Utilities, Refrigeration, …), each holding every asset
// in that category — they are NOT individual machines. The single- vs
// multi-machine unit is the per-asset `mappedMachineGroup`. So we group the
// critical-category assets by mappedMachineGroup and count the machines in each:
//   1 asset  => S1 (single-machine critical group)
//   >1 asset => S2 (multi-machine critical group)
// The single/multi label is intrinsic (counted across all stages); the page-level
// Stage filter then scopes WHICH assets (and their WOs) are actually included.
// Honours the page-level Stage filter, the page-level Category filter, and the
// card's optional Category filter.
function kdiBuildCriticalGroupAssetSets() {
    const s1 = new Set();
    const s2 = new Set();
    if (!assetListLoaded || assetListLoadFailed) return { s1, s2 };
    const selectedStage = getSelectedDowntimeStage();
    const isAllStages = !selectedStage || selectedStage === DOWNTIME_STAGE_ALL;
    const stageFilter = isAllStages ? "" : String(selectedStage).toLowerCase().trim();
    const groups = new Map(); // mappedMachineGroup key -> [{ assetId, stageOk }]
    (assetListData || []).forEach((machine) => {
        if (machine.criticality !== "Critical") return;
        const catName = String(machine.machine_name || machine.mappedMainAssetGroup || "").trim();
        const groupCat = EQUIP_GROUP_TO_CATEGORY[catName] || "Unclassified";
        // Page-level category filter, then the card's optional category filter.
        if (selectedEquipmentCategory !== "all" && groupCat !== selectedEquipmentCategory) return;
        if (kdiCritCmpCategory !== "all" && groupCat !== kdiCritCmpCategory) return;
        (machine.assets || []).forEach((asset) => {
            const assetId = String(asset.asset_id || "").trim().toUpperCase();
            if (!assetId) return;
            // The machine-group unit. Fall back to sub-group / asset id so an
            // un-grouped asset counts as its own single-machine group.
            const mg = String(asset.mappedMachineGroup || asset.mappedSubAssetGroup || asset.asset_id || "").trim().toLowerCase();
            const key = `${groupCat}||${mg}`;
            if (!groups.has(key)) groups.set(key, []);
            const assetStage = String(asset.mappedStage || "").toLowerCase().trim();
            groups.get(key).push({ assetId, stageOk: isAllStages || assetStage === stageFilter });
        });
    });
    groups.forEach((assets) => {
        const target = assets.length === 1 ? s1 : s2;
        assets.forEach((a) => { if (a.stageOk) target.add(a.assetId); });
    });
    return { s1, s2 };
}

// Work-order completion date used for financial-year bucketing of MTTR.
function kdiCritCmpWoDate(wo) {
    return parseDateValue(wo?.actual_end_time || wo?.actual_end || wo?.maintenance_end_time || wo?.end_time);
}

// Whole-FY aggregate MTTR metrics for the summary chips.
function kdiCritCmpMetrics(rows, assetIdSet, fyStart) {
    const hours = [];
    (rows || []).forEach((wo) => {
        const id = String(wo.asset_id || "").trim().toUpperCase();
        if (!id || !assetIdSet.has(id)) return;
        if (!isDateInMrFinancialYear(kdiCritCmpWoDate(wo), fyStart)) return;
        const h = getTtrHours(wo);
        if (h === null || !Number.isFinite(h) || h < 0) return;
        hours.push(h);
    });
    const sorted = [...hours].sort((a, b) => a - b);
    const n = sorted.length;
    const avg = n ? sorted.reduce((s, v) => s + v, 0) / n : null;
    const med = n ? median(sorted) : null;
    const p75 = n ? sorted[Math.min(Math.floor(n * 0.75), n - 1)] : null;
    const p90 = n ? sorted[Math.min(Math.floor(n * 0.90), n - 1)] : null;
    const hasOutlier = n >= 2 && sorted[n - 1] > 720; // flag records >30 days
    return { average: avg, median: med, p75, p90, count: n, hasOutlier, maxHours: sorted[n - 1] ?? null };
}

// Monthly breakdown: one data point per FY month. Null means no valid records that month.
function kdiCritCmpMonthlyMetrics(rows, assetIdSet, fyStart) {
    const fyNum = Number(fyStart);
    // Build 12 buckets aligned to the FY month order.
    const buckets = MR_FINANCIAL_MONTH_ORDER.map((mi) => {
        const yr = (mi + 1) >= MR_FINANCIAL_YEAR_START_MONTH ? fyNum : fyNum + 1;
        return { mi, yr, hours: [], assets: {} };
    });
    (rows || []).forEach((wo) => {
        const id = String(wo.asset_id || "").trim().toUpperCase();
        if (!id || !assetIdSet.has(id)) return;
        const d = kdiCritCmpWoDate(wo);
        if (!d || !isDateInMrFinancialYear(d, fyStart)) return;
        const h = getTtrHours(wo);
        if (h === null || !Number.isFinite(h) || h < 0) return;
        const woMi = d.getMonth();
        const woYr = d.getFullYear();
        const b = buckets.find((bk) => bk.mi === woMi && bk.yr === woYr);
        if (!b) return;
        b.hours.push(h);
        b.assets[id] = (b.assets[id] || 0) + h; // track per-asset total for tooltip
    });
    return buckets.map((b) => {
        if (!b.hours.length) return null;
        const sorted = [...b.hours].sort((a, c) => a - c);
        const n = sorted.length;
        const avg = sorted.reduce((s, v) => s + v, 0) / n;
        const med = median(sorted);
        const p75 = sorted[Math.min(Math.floor(n * 0.75), n - 1)];
        const p90 = sorted[Math.min(Math.floor(n * 0.90), n - 1)];
        const topAsset = Object.entries(b.assets).sort((a, c) => c[1] - a[1])[0]?.[0] ?? null;
        const hasOutlier = sorted[n - 1] > 720;
        return { average: avg, median: med, p75, p90, count: n, topAsset, hasOutlier };
    });
}

function kdiGetCritCmpRows() {
    let rows = getAllWorkOrdersForMtbf();
    if (!rows.length) rows = getWorkOrderRows(getManagement());
    return rows;
}

// Populate both FY selectors from the financial years present in the
// stage/category scoped work orders. Newest year is the default.
function kdiCritCmpPopulateFyOptions(rows) {
    const fySelect = document.getElementById("kdi-crit-cmp-fy");
    const fy2Select = document.getElementById("kdi-crit-cmp-fy2");
    if (!fySelect || !fy2Select) return;
    const years = new Set();
    (rows || []).forEach((wo) => {
        const fy = getMrFinancialYearStart(kdiCritCmpWoDate(wo));
        if (fy !== null) years.add(fy);
    });
    // When no row-derived years are found, seed from the MTBF history years list
    // (calendar-year keys are close enough to FY starts for selector seeding).
    if (!years.size && mtbfHistoryPayload?.years?.length) {
        mtbfHistoryPayload.years.forEach((yr) => {
            const n = Number(yr);
            if (Number.isFinite(n) && n > 2000) years.add(n);
        });
    }
    const sorted = [...years].sort((a, b) => b - a);
    if (!sorted.length) {
        fySelect.innerHTML = `<option value="">No data</option>`;
        fy2Select.innerHTML = `<option value="">None</option>`;
        kdiCritCmpFy = "";
        kdiCritCmpFy2 = "";
        return;
    }
    const sortedStr = sorted.map(String);
    if (!sortedStr.includes(String(kdiCritCmpFy))) kdiCritCmpFy = sortedStr[0];
    if (kdiCritCmpFy2 && !sortedStr.includes(String(kdiCritCmpFy2))) kdiCritCmpFy2 = "";
    const optsFor = (includeNone) =>
        (includeNone ? `<option value="">None</option>` : "") +
        sorted.map((fy) => `<option value="${fy}">${escapeHtml(getMrFinancialYearLabel(fy))}</option>`).join("");
    fySelect.innerHTML = optsFor(false);
    fy2Select.innerHTML = optsFor(true);
    fySelect.value = String(kdiCritCmpFy);
    fy2Select.value = String(kdiCritCmpFy2 || "");
}

// Render one line/area trend chart.
// fyLines: [{fyLabel, critType, monthData: [null|{average,median,p75,p90,count,topAsset}]}]
// monthLabels: short labels ("Apr", "May", …) for X-axis ticks.
// fullLabels: long labels ("Apr 2026", …) for tooltip.
function kdiRenderCritCmpTrendChart(chartId, fyLines, monthLabels, fullLabels, metricKey, yTitle) {
    const canvas = ensureCanvas(chartId);
    if (!canvas) return;
    destroyChart(chartId);

    const palette = [
        { border: "#7c3aed", bg: "rgba(124,58,237,0.07)", dash: [] },
        { border: "#f59e0b", bg: "rgba(245,158,11,0.05)", dash: [5, 4] },
    ];

    const hasAnyData = fyLines.some((fl) => fl.monthData.some((d) => d !== null));
    if (!hasAnyData) {
        renderEmptyChart(chartId, "No critical MTTR data for the selected filters");
        return;
    }
    const datasets = fyLines.map((fl, i) => {
        const pal = palette[i % palette.length];
        return {
            label: fl.fyLabel + (fl.critType ? ` · ${fl.critType}` : ""),
            data: fl.monthData.map((d) => (d ? (d[metricKey] ?? null) : null)),
            counts: fl.monthData.map((d) => (d ? d.count : 0)),
            topAssets: fl.monthData.map((d) => (d ? d.topAsset : null)),
            borderColor: pal.border,
            backgroundColor: pal.bg,
            borderDash: pal.dash,
            borderWidth: i === 0 ? 2.5 : 1.8,
            pointRadius: 3,
            pointHoverRadius: 5,
            fill: true,
            tension: 0.35,
            spanGaps: false,
        };
    });

    chartRefs[chartId] = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: { labels: monthLabels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: {
                    display: fyLines.length > 1,
                    position: "top",
                    labels: { boxWidth: 14, font: { size: 11 }, padding: 10 },
                },
                tooltip: {
                    callbacks: {
                        title: (items) => fullLabels[items[0]?.dataIndex] || items[0]?.label || "",
                        label: (ctx) => {
                            const v = ctx.raw;
                            const cnt = ctx.dataset.counts?.[ctx.dataIndex] ?? 0;
                            const top = ctx.dataset.topAssets?.[ctx.dataIndex];
                            if (v === null || v === undefined)
                                return `${ctx.dataset.label}: No valid MTTR records`;
                            let line = `${ctx.dataset.label}: ${fmtHours(v)} (${cnt} record${cnt !== 1 ? "s" : ""})`;
                            if (top) line += `  · top: ${top}`;
                            return line;
                        },
                    },
                },
            },
            scales: {
                y: {
                    beginAtZero: true,
                    grid: { color: "#e8edf3" },
                    ticks: {
                        font: { size: 10 },
                        callback: (v) => {
                            if (v >= 24) return `${Math.round(v / 24)}d`;
                            return `${v}h`;
                        },
                    },
                    title: { display: true, text: yTitle, font: { size: 10 } },
                },
                x: {
                    grid: { display: false },
                    ticks: { font: { size: 10 }, maxRotation: 0 },
                },
            },
        },
    });
}

function kdiRenderCritCmpBothChart(chartId, s1Lines, s2Lines, monthLabels, fullLabels, metricKey, yTitle) {
    const canvas = ensureCanvas(chartId);
    if (!canvas) return;
    destroyChart(chartId);

    // S1 = indigo/blue  (solid = current FY, dashed = comparison FY)
    // S2 = red/orange   (solid = current FY, dashed = comparison FY)
    const s1Pal = [
        { border: "#4f46e5", bg: "rgba(79,70,229,0.07)", dash: [],     w: 2.5 },
        { border: "#818cf8", bg: "rgba(129,140,248,0.04)", dash: [5,4], w: 1.8 },
    ];
    const s2Pal = [
        { border: "#dc2626", bg: "rgba(220,38,38,0.06)", dash: [],     w: 2.5 },
        { border: "#f87171", bg: "rgba(248,113,113,0.04)", dash: [5,4], w: 1.8 },
    ];

    const buildDs = (lines, pal, prefix) => lines.map((fl, i) => {
        const p = pal[i % pal.length];
        return {
            label: `${prefix} · ${fl.fyLabel}`,
            data: fl.monthData.map((d) => (d ? (d[metricKey] ?? 0) : 0)),
            counts: fl.monthData.map((d) => (d ? d.count : 0)),
            topAssets: fl.monthData.map((d) => (d ? d.topAsset : null)),
            borderColor: p.border,
            backgroundColor: p.bg,
            borderDash: p.dash,
            borderWidth: p.w,
            pointRadius: 3,
            pointHoverRadius: 5,
            fill: false,
            tension: 0.35,
            spanGaps: true,
        };
    });

    const datasets = [...buildDs(s1Lines, s1Pal, "S1"), ...buildDs(s2Lines, s2Pal, "S2")];
    const hasAnyData = datasets.some((ds) => ds.data.some((v) => v !== null));
    if (!hasAnyData) { renderEmptyChart(chartId, "No critical MTTR data for the selected filters"); return; }

    chartRefs[chartId] = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: { labels: monthLabels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { display: true, position: "top", labels: { boxWidth: 14, font: { size: 11 }, padding: 10 } },
                tooltip: {
                    callbacks: {
                        title: (items) => fullLabels[items[0]?.dataIndex] || items[0]?.label || "",
                        label: (ctx) => {
                            const v = ctx.raw;
                            const cnt = ctx.dataset.counts?.[ctx.dataIndex] ?? 0;
                            const top = ctx.dataset.topAssets?.[ctx.dataIndex];
                            if (v === null || v === undefined || (v === 0 && ctx.dataset.counts?.[ctx.dataIndex] === 0)) return `${ctx.dataset.label}: No data`;
                            let line = `${ctx.dataset.label}: ${fmtHours(v)} (${cnt} record${cnt !== 1 ? "s" : ""})`;
                            if (top) line += `  · top: ${top}`;
                            return line;
                        },
                    },
                },
            },
            scales: {
                y: {
                    beginAtZero: true,
                    grid: { color: "#e8edf3" },
                    ticks: { font: { size: 10 }, callback: (v) => v >= 24 ? `${Math.round(v / 24)}d` : `${v}h` },
                    title: { display: true, text: yTitle, font: { size: 10 } },
                },
                x: { grid: { display: false }, ticks: { font: { size: 10 }, maxRotation: 0 } },
            },
        },
    });
}

function kdiToggleCritCmpSection() {
    const body = document.getElementById("kdi-crit-cmp-section-body");
    const btn  = document.getElementById("kdi-crit-cmp-toggle-btn");
    if (!body || !btn) return;
    const collapsed = body.classList.toggle("kdi-crit-cmp-section-collapsed");
    btn.textContent = collapsed ? "▶ Expand" : "▼ Collapse";
}

function kdiRenderCritCmpChart(model) {
    const metricKey = ["average", "median", "p75", "p90"].includes(kdiCritCmpMetric) ? kdiCritCmpMetric : "average";
    const metricLabel = { average: "Average MTTR", median: "Median MTTR", p75: "P75 MTTR", p90: "P90 MTTR" }[metricKey];
    const title    = document.getElementById("kdi-crit-cmp-chart-title");
    const subtitle = document.getElementById("kdi-crit-cmp-chart-subtitle");
    const singleEl = document.getElementById("kdi-crit-cmp-single");
    const dualEl   = document.getElementById("kdi-crit-cmp-dual");

    // Always hide the dual mini-chart container; always use the single canvas
    if (singleEl) singleEl.classList.remove("hidden");
    if (dualEl) {
        dualEl.classList.add("hidden");
        destroyChart("kdiCritCmpChartS1");
        destroyChart("kdiCritCmpChart2");
    }

    if (model.mode === "both") {
        if (title)    title.textContent    = `S1 vs S2 — ${metricLabel} Monthly Trend`;
        if (subtitle) subtitle.textContent = "S1 (indigo) = single-machine critical · S2 (red) = multi-machine critical. Lower is better.";
        kdiRenderCritCmpBothChart("kdiCritCmpChart", model.s1Lines, model.s2Lines, model.monthLabels, model.fullLabels, metricKey, `${metricLabel} (hrs)`);
    } else {
        if (title)    title.textContent    = "Critical Machine MTTR Monthly Trend";
        if (subtitle) subtitle.textContent = "Monthly MTTR trend for S1/S2 critical machine groups. Lower is better.";
        kdiRenderCritCmpTrendChart("kdiCritCmpChart", model.lines, model.monthLabels, model.fullLabels, metricKey, `${metricLabel} (hrs)`);
    }
}

function kdiRenderCritCmpSummaryChips(el, types, fyList, aggregates, metricKey) {
    if (!el) return;
    const metricLabel = { average: "Average", median: "Median", p75: "P75", p90: "P90" }[metricKey] || "Average";

    // Detect any outlier across all types/FY combinations.
    const anyOutlier = aggregates.some((row) => row.some((a) => a.hasOutlier));

    const chip = (label, values, cls = "") =>
        `<div class="kdi-crit-chip ${cls}"><span class="kdi-crit-chip-label">${label}</span><span class="kdi-crit-chip-value">${values}</span></div>`;

    const fmtVal = (a, key) => a?.[key] !== null && a?.[key] !== undefined ? fmtHours(a[key]) : "n/a";

    // Row 0 = selected FY, Row 1 = compare FY (if present)
    const [agg1, agg2] = aggregates; // each is an array aligned to `types`

    // Selected FY row
    const fy1Parts = types.map((t, i) => `${t.short}: ${fmtVal(agg1[i], metricKey)}`).join(" &nbsp;|&nbsp; ");
    const fy1Chip = chip(fyList[0].label + " " + metricLabel, fy1Parts, "kdi-crit-chip-primary");

    // Compare FY row
    const fy2Chip = fyList[1]
        ? chip(fyList[1].label + " " + metricLabel, types.map((t, i) => `${t.short}: ${fmtVal(agg2?.[i], metricKey)}`).join(" &nbsp;|&nbsp; "), "kdi-crit-chip-secondary")
        : "";

    // Change row
    let changeChip = "";
    if (fyList[1] && agg2) {
        const changeParts = types.map((t, i) => {
            const v1 = agg1[i]?.[metricKey];
            const v2 = agg2[i]?.[metricKey];
            if (v1 === null || v2 === null) return `${t.short}: n/a`;
            const diff = v1 - v2;
            const sign = diff >= 0 ? "+" : "";
            const dir = diff > 0 ? " worse" : (diff < 0 ? " better" : " no change");
            return `${t.short}: ${sign}${fmtHours(Math.abs(diff))}${dir}`;
        }).join(" &nbsp;|&nbsp; ");
        changeChip = chip("Change vs " + fyList[1].label, changeParts, "kdi-crit-chip-change");
    }

    // Worst month row (highest metric value in selected FY)
    const worstParts = types.map((t, i) => {
        if (!agg1[i]?.monthData) return "";
        let bestVal = null, bestLabel = "";
        agg1[i].monthData.forEach((d, mi) => {
            const v = d?.[metricKey];
            if (v !== null && v !== undefined && (bestVal === null || v > bestVal)) {
                bestVal = v; bestLabel = MR_FINANCIAL_MONTH_LABELS[mi] || "";
            }
        });
        if (bestVal === null) return "";
        return `<span>Worst ${t.short}: ${bestLabel} — ${fmtHours(bestVal)}</span>`;
    }).filter(Boolean);
    const worstChip = worstParts.length ? chip("Worst Month", worstParts.join(" &nbsp;|&nbsp; "), "") : "";

    // Record count row
    const totalCount = types.reduce((s, _, i) => s + (agg1[i]?.count ?? 0), 0);
    const countChip = chip("Valid Records", `Based on ${totalCount.toLocaleString()} valid WO/MR record${totalCount !== 1 ? "s" : ""}`, "kdi-crit-chip-count");

    // Outlier note
    const outlierNote = anyOutlier
        ? `<div class="kdi-crit-outlier-note">⚠ Average MTTR may be affected by long-duration/outlier work orders. Use Median MTTR for a more stable view.</div>`
        : "";

    el.innerHTML = `<div class="kdi-crit-chips-wrap">${fy1Chip}${fy2Chip}${changeChip}${worstChip}${countChip}</div>${outlierNote}`;
}

function updateKdiCritCmpSection(rows) {
    if (!document.getElementById("kdiCritCmpChart")) return;
    if (!Array.isArray(rows)) rows = kdiGetCritCmpRows();
    const summaryEl = document.getElementById("kdi-crit-cmp-summary");

    if (!assetListLoaded) {
        kdiRenderCritCmpChart({ mode: "single", lines: [], monthLabels: [], fullLabels: [] });
        if (summaryEl) summaryEl.innerHTML = "<p class='kdi-crit-loading'>Loading critical asset mapping…</p>";
        return;
    }
    if (!rows.length && !downtimePayload && !mtbfHistoryPayload) {
        if (summaryEl) summaryEl.innerHTML = "<p class='kdi-crit-loading'>Loading maintenance data…</p>";
        return;
    }

    kdiCritCmpPopulateFyOptions(rows);
    if (!kdiCritCmpFy) {
        kdiRenderCritCmpChart({ mode: "single", lines: [], monthLabels: [], fullLabels: [] });
        if (summaryEl) summaryEl.innerHTML = "<p class='kdi-crit-loading'>No completed critical work orders available for these filters.</p>";
        return;
    }

    const { s1, s2 } = kdiBuildCriticalGroupAssetSets();
    const types = kdiCritCmpType === "s1"
        ? [{ short: "S1", set: s1 }]
        : kdiCritCmpType === "s2"
            ? [{ short: "S2", set: s2 }]
            : [{ short: "S1", set: s1 }, { short: "S2", set: s2 }];

    const fyList = [{ fy: kdiCritCmpFy, label: getMrFinancialYearLabel(kdiCritCmpFy) }];
    if (kdiCritCmpFy2 && String(kdiCritCmpFy2) !== String(kdiCritCmpFy))
        fyList.push({ fy: kdiCritCmpFy2, label: getMrFinancialYearLabel(kdiCritCmpFy2) });

    const metricKey = ["average", "median", "p75", "p90"].includes(kdiCritCmpMetric) ? kdiCritCmpMetric : "average";
    const monthLabels = MR_FINANCIAL_MONTH_LABELS;          // ["Apr",…,"Mar"]
    const fullLabels  = getMrFinancialMonthLabelsWithYear(kdiCritCmpFy); // ["Apr 2026",…]

    // Compute monthly data for every (type × FY) pair.
    // Also compute whole-FY aggregates (with monthData attached) for summary chips.
    const allAggs = fyList.map(({ fy }) =>
        types.map((t) => {
            const agg = kdiCritCmpMetrics(rows, t.set, fy);
            const monthData = kdiCritCmpMonthlyMetrics(rows, t.set, fy);
            return { ...agg, monthData };
        })
    );

    if (kdiCritCmpType === "both") {
        // Two mini charts: one per critical type, each with FY lines.
        const buildLines = (typeIdx) =>
            fyList.map(({ label }, fi) => ({
                fyLabel: label,
                critType: types[typeIdx].short,
                monthData: allAggs[fi][typeIdx].monthData,
            }));
        kdiRenderCritCmpChart({
            mode: "both",
            s1Lines: buildLines(0), s2Lines: buildLines(1),
            monthLabels, fullLabels,
        });
    } else {
        // Single chart: one type, FY lines.
        const lines = fyList.map(({ label }, fi) => ({
            fyLabel: label,
            critType: types[0].short,
            monthData: allAggs[fi][0].monthData,
        }));
        kdiRenderCritCmpChart({ mode: "single", lines, monthLabels, fullLabels });
    }

    kdiRenderCritCmpSummaryChips(summaryEl, types, fyList, allAggs, metricKey);
}

function kdiWireCritCmpControls() {
    const bind = (id, handler) => document.getElementById(id)?.addEventListener("change", handler);
    bind("kdi-crit-cmp-fy", (e) => { kdiCritCmpFy = e.target.value || ""; updateKdiCritCmpSection(); });
    bind("kdi-crit-cmp-fy2", (e) => { kdiCritCmpFy2 = e.target.value || ""; updateKdiCritCmpSection(); });
    bind("kdi-crit-cmp-type", (e) => { kdiCritCmpType = e.target.value || "both"; updateKdiCritCmpSection(); });
    bind("kdi-crit-cmp-metric", (e) => { kdiCritCmpMetric = e.target.value || "average"; updateKdiCritCmpSection(); });
    bind("kdi-crit-cmp-category", (e) => { kdiCritCmpCategory = e.target.value || "all"; updateKdiCritCmpSection(); });
}

function updateKdiMtbfAnalysis() {
    if (!kdiCurrentMtbfData) return;
    const { allAssets, assetResults } = kdiCurrentMtbfData;
    const crit = kdiMtbfCriticalityFilter;
    const availableAssets = allAssets.filter((entry) => !crit || entry.criticality === crit);
    kdiMtbfSelectedGroup = kdiPopulateMachineGroupSelect("kdi-mtbf-machine-group-filter", availableAssets, kdiMtbfSelectedGroup);
    const groups = kdiMtbfRankMode === "machine-group"
        ? kdiGroupMtbfByMachineGroupLabel(allAssets, crit, kdiMtbfSelectedGroup)
        : kdiGroupMtbfByMachineGroup(allAssets, crit);
    const assetRows = kdiBuildMtbfAssetRows(allAssets, crit, kdiMtbfSelectedGroup);
    const filteredResults = assetResults.filter((entry) => (!crit || entry.criticality === crit) && (!kdiMtbfSelectedGroup || entry.group === kdiMtbfSelectedGroup));
    kdiRenderCriticalityBtns("kdi-mtbf-criticality-filter", crit, (v) => { kdiMtbfCriticalityFilter = v; updateKdiMtbfAnalysis(); });
    kdiRenderRankToggle("kdi-mtbf-rank-toggle", kdiMtbfRankMode, (mode) => { kdiMtbfRankMode = mode; updateKdiMtbfAnalysis(); });
    kdiRenderMtbfGroupChart(groups, assetRows, kdiMtbfSelectedGroup, kdiMtbfRankMode);
    kdiRenderMtbfSummaryTable(groups, assetRows, kdiMtbfSelectedGroup, kdiMtbfRankMode);
    const dd = document.getElementById("kdi-mtbf-asset-drilldown");
    if (dd?.open) kdiRenderMtbfAssetDrilldown(assetResults, crit, kdiMtbfRankMode === "machine-group" ? kdiMtbfDrilldownMachineGroup : kdiMtbfSelectedGroup);
}

function kdiSetSelectValueIfPresent(selectId, value) {
    const select = document.getElementById(selectId);
    if (!select) return;
    const hasOption = [...select.options].some((option) => option.value === value);
    if (hasOption) select.value = value;
}

function kdiOpenMttrGroupDrilldown(group) {
    if (!group || !kdiCurrentMttrData) return;
    if (kdiMttrRankMode === "machine-group") {
        kdiMttrDrilldownMachineGroup = group;
        updateKdiMttrAnalysis();
        const dd = document.getElementById("kdi-mttr-asset-drilldown");
        if (!dd) return;
        dd.open = true;
        kdiRenderMttrAssetDrilldown(kdiCurrentMttrData.assetMap, kdiMttrCriticalityFilter, group);
        dd.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } else {
        kdiMttrRankMode = "group";
        kdiMttrSelectedGroup = group;
        kdiMttrDrilldownMachineGroup = "";
        updateKdiMttrAnalysis();
        const dd = document.getElementById("kdi-mttr-asset-drilldown");
        if (!dd) return;
        dd.open = true;
        kdiSetSelectValueIfPresent("kdi-mttr-asset-group-filter", group);
        kdiRenderMttrAssetDrilldown(kdiCurrentMttrData.assetMap, kdiMttrCriticalityFilter, group);
        dd.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
}

function kdiOpenMtbfGroupDrilldown(group) {
    if (!group || !kdiCurrentMtbfData) return;
    if (kdiMtbfRankMode === "machine-group") {
        kdiMtbfDrilldownMachineGroup = group;
        updateKdiMtbfAnalysis();
        const dd = document.getElementById("kdi-mtbf-asset-drilldown");
        if (!dd) return;
        dd.open = true;
        kdiRenderMtbfAssetDrilldown(kdiCurrentMtbfData.assetResults, kdiMtbfCriticalityFilter, group);
        dd.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } else {
        kdiMtbfRankMode = "group";
        kdiMtbfSelectedGroup = group;
        kdiMtbfDrilldownMachineGroup = "";
        updateKdiMtbfAnalysis();
        const dd = document.getElementById("kdi-mtbf-asset-drilldown");
        if (!dd) return;
        dd.open = true;
        kdiSetSelectValueIfPresent("kdi-mtbf-asset-group-filter", group);
        kdiRenderMtbfAssetDrilldown(kdiCurrentMtbfData.assetResults, kdiMtbfCriticalityFilter, group);
        dd.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
}

function kdiWireGroupDrilldown(tableBodyId, onOpen) {
    const tbody = document.getElementById(tableBodyId);
    if (!tbody) return;
    tbody.addEventListener("click", (event) => {
        const row = event.target.closest(".kdi-drill-row[data-kdi-group]");
        if (!row || !tbody.contains(row)) return;
        onOpen(row.dataset.kdiGroup || "");
    });
    tbody.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        const row = event.target.closest(".kdi-drill-row[data-kdi-group]");
        if (!row || !tbody.contains(row)) return;
        event.preventDefault();
        onOpen(row.dataset.kdiGroup || "");
    });
}

function kdiWireStaticControls() {
    function wireToggle(btnId, panelId, updateFn, label) {
        const btn = document.getElementById(btnId);
        const panel = document.getElementById(panelId);
        if (!btn || !panel) return;
        panel.classList.remove("hidden");
        btn.setAttribute("aria-expanded", "true");
        btn.innerHTML = `${label} Analysis`;
        btn.addEventListener("click", () => {
            panel.classList.remove("hidden");
            updateFn();
        });
    }
    wireToggle("kdi-mttr-analysis-btn", "kdi-mttr-analysis-panel", () => { kdiMttrCriticalityFilter = ""; updateKdiMttrAnalysis(); }, "MTTR");
    wireToggle("kdi-mtbf-analysis-btn", "kdi-mtbf-analysis-panel", () => { kdiMtbfCriticalityFilter = ""; updateKdiMtbfAnalysis(); }, "MTBF");
    kdiWireGroupDrilldown("kdi-mttr-group-table-body", kdiOpenMttrGroupDrilldown);
    kdiWireGroupDrilldown("kdi-mtbf-group-table-body", kdiOpenMtbfGroupDrilldown);
    kdiWireCritCmpControls();

    document.getElementById("kdi-mttr-asset-drilldown")?.addEventListener("toggle", () => {
        if (document.getElementById("kdi-mttr-asset-drilldown")?.open && kdiCurrentMttrData) {
            const grpArg = kdiMttrRankMode === "machine-group" ? kdiMttrDrilldownMachineGroup : kdiMttrSelectedGroup;
            kdiRenderMttrAssetDrilldown(kdiCurrentMttrData.assetMap, kdiMttrCriticalityFilter, grpArg);
        }
    });
    document.getElementById("kdi-mtbf-asset-drilldown")?.addEventListener("toggle", () => {
        if (document.getElementById("kdi-mtbf-asset-drilldown")?.open && kdiCurrentMtbfData) {
            const grpArg = kdiMtbfRankMode === "machine-group" ? kdiMtbfDrilldownMachineGroup : kdiMtbfSelectedGroup;
            kdiRenderMtbfAssetDrilldown(kdiCurrentMtbfData.assetResults, kdiMtbfCriticalityFilter, grpArg);
        }
    });
    document.getElementById("kdi-mttr-asset-group-filter")?.addEventListener("change", (event) => {
        if (!kdiCurrentMttrData) return;
        if (kdiMttrRankMode === "machine-group") {
            kdiMttrDrilldownMachineGroup = event.target.value || "";
            kdiRenderMttrAssetDrilldown(kdiCurrentMttrData.assetMap, kdiMttrCriticalityFilter, kdiMttrDrilldownMachineGroup);
        } else {
            kdiRenderMttrAssetDrilldown(kdiCurrentMttrData.assetMap, kdiMttrCriticalityFilter, kdiMttrSelectedGroup);
        }
    });
    document.getElementById("kdi-mtbf-asset-group-filter")?.addEventListener("change", (event) => {
        if (!kdiCurrentMtbfData) return;
        if (kdiMtbfRankMode === "machine-group") {
            kdiMtbfDrilldownMachineGroup = event.target.value || "";
            kdiRenderMtbfAssetDrilldown(kdiCurrentMtbfData.assetResults, kdiMtbfCriticalityFilter, kdiMtbfDrilldownMachineGroup);
        } else {
            kdiRenderMtbfAssetDrilldown(kdiCurrentMtbfData.assetResults, kdiMtbfCriticalityFilter, kdiMtbfSelectedGroup);
        }
    });
    document.getElementById("kdi-mttr-machine-group-filter")?.addEventListener("change", (event) => {
        kdiMttrSelectedGroup = event.target.value || "";
        if (kdiCurrentMttrData) updateKdiMttrAnalysis();
    });
    document.getElementById("kdi-mtbf-machine-group-filter")?.addEventListener("change", (event) => {
        kdiMtbfSelectedGroup = event.target.value || "";
        if (kdiCurrentMtbfData) updateKdiMtbfAnalysis();
    });
    document.getElementById("kdi-mttr-group-search")?.addEventListener("input", () => {
        if (kdiCurrentMttrData) updateKdiMttrAnalysis();
    });
    document.getElementById("kdi-mtbf-group-search")?.addEventListener("input", () => {
        if (kdiCurrentMtbfData) updateKdiMtbfAnalysis();
    });
    document.getElementById("kdi-mttr-asset-search")?.addEventListener("input", () => {
        if (kdiCurrentMttrData) {
            const grpArg = kdiMttrRankMode === "machine-group" ? kdiMttrDrilldownMachineGroup : kdiMttrSelectedGroup;
            kdiRenderMttrAssetDrilldown(kdiCurrentMttrData.assetMap, kdiMttrCriticalityFilter, grpArg);
        }
    });
    document.getElementById("kdi-mtbf-asset-search")?.addEventListener("input", () => {
        if (kdiCurrentMtbfData) {
            const grpArg = kdiMtbfRankMode === "machine-group" ? kdiMtbfDrilldownMachineGroup : kdiMtbfSelectedGroup;
            kdiRenderMtbfAssetDrilldown(kdiCurrentMtbfData.assetResults, kdiMtbfCriticalityFilter, grpArg);
        }
    });
}

// ─── End KDI ─────────────────────────────────────────────────────────────────

startEmbeddedHeightSync();
document.addEventListener("DOMContentLoaded", init);
window.addEventListener("focus", refreshCurrentView);
document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshCurrentView();
});
setInterval(refreshCurrentView, 60000);
