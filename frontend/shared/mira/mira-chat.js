/*
 * Floating MIRA chat assistant.
 *
 * Read-only: this uses /api/mira/chat only. It never edits any maintenance
 * record or source file. Answers are based on verified dashboard data.
 */
(function () {
    "use strict";

    const CFG = window.MIRA_CONFIG || {};
    if (CFG.enabled === false) return;
    try {
        if (window.self !== window.top) return;
    } catch (_err) {
        // Cross-origin iframe guard: treat as top window.
    }

    const API = CFG.apiBase || "/api/mira";
    const PROMPTS = [
        "Summarise YTD maintenance performance",
        "What should be followed up today?",
        "Which asset has the most MR?",
        "Which functional location has the highest workload?",
        "What is the most common fault this month?",
        "What are the main PM issues?",
        "Which PM tasks are overdue?",
        "Summarise spare parts consumption",
        "Give me a one-line report summary",
        "Stage 1 Combi Oven breakdowns past 1 year by unit",
        "Which Combi Oven unit had the most issues?",
        "Combi Oven breakdown summary with estimated repair cost",
    ];
    const KPI_REGISTRY = [
        { id: "pm_due_today", category: "PM Schedule", label: "PM due today", prompt: "Analyse PM due today." },
        { id: "pm_completed", category: "PM Schedule", label: "PM completed", prompt: "Analyse PM completed." },
        { id: "pm_pending", category: "PM Schedule", label: "PM pending", prompt: "Analyse PM pending." },
        { id: "pm_overdue", category: "PM Schedule", label: "PM overdue", prompt: "Which PM tasks are overdue?" },
        { id: "pm_completion_rate", category: "PM Schedule", label: "PM completion rate", prompt: "Analyse PM completion rate." },
        { id: "pm_upcoming_7_days", category: "PM Schedule", label: "Upcoming PM next 7 days", prompt: "Analyse upcoming PM for the next 7 days." },
        { id: "downtime_active", category: "Downtime", label: "Current active downtime", prompt: "Summarise current active downtime." },
        { id: "downtime_incidents", category: "Downtime", label: "Downtime incidents", prompt: "Summarise downtime incidents." },
        { id: "downtime_total_hours", category: "Downtime", label: "Total downtime hours", prompt: "Analyse total downtime hours." },
        { id: "downtime_mttr", category: "Downtime", label: "MTTR", prompt: "Analyse MTTR." },
        { id: "downtime_mtbf", category: "Downtime", label: "MTBF", prompt: "Analyse MTBF." },
        { id: "preventive_corrective_mix", category: "Downtime", label: "Preventive vs Corrective", prompt: "Analyse preventive vs corrective MR raised using the Downtime classifier." },
        { id: "downtime_top_machine_group", category: "Downtime", label: "Top machine groups", prompt: "Which functional location has the highest workload?" },
        { id: "downtime_repeat_assets", category: "Downtime", label: "Repeated downtime assets", prompt: "Which asset has the most MR?" },
        { id: "spare_parts_low_stock", category: "Spare Parts", label: "Items below minimum stock", prompt: "Analyse spare parts below minimum stock." },
        { id: "spare_parts_consumption", category: "Spare Parts", label: "High-consumption parts", prompt: "Summarise spare parts consumption." },
        { id: "spare_parts_pending_po", category: "Spare Parts", label: "Pending PO / external purchase", prompt: "Analyse pending spare part purchase items." },
        { id: "spare_parts_stockout_risk", category: "Spare Parts", label: "Stock-out risk", prompt: "Analyse spare parts stock-out risk." },
        { id: "mr_tracking_acknowledgement", category: "MR / Work Order", label: "MR tracking and acknowledgement", prompt: "Analyse MR tracking and acknowledgement." },
        { id: "mr_open", category: "MR / Work Order", label: "Open MR", prompt: "Show open MR follow-up." },
        { id: "mr_in_progress", category: "MR / Work Order", label: "In-progress MR", prompt: "Analyse in-progress MR." },
        { id: "wo_response_time", category: "MR / Work Order", label: "Work order response", prompt: "Analyse work order response." },
        { id: "backlog_carry_over", category: "MR / Work Order", label: "Backlog and carry-over", prompt: "Analyse backlog and carry-over." },
        { id: "yearly_mr_movement", category: "MR / Work Order", label: "Yearly MR movement", prompt: "Analyse yearly MR movement." },
        { id: "critical_machine_activity", category: "MR / Work Order", label: "Critical asset activity", prompt: "Analyse critical asset activity using the existing criticality list only." },
        { id: "data_quality", category: "MR / Work Order", label: "Data Reliability", prompt: "Analyse data reliability issues." },
    ];
    const KPI_CATEGORIES = ["PM Schedule", "Downtime", "Spare Parts", "MR / Work Order"];

    const state = {
        open: false,
        busy: false,
        mode: "chat",               // "chat" (Q&A) | "kpi" (KPI Analysis)
        providerStatus: {
            text: "Checking AI mode...",
            tone: "muted",
        },
    };

    document.addEventListener("DOMContentLoaded", init);

    function init() {
        if (document.getElementById("mira-chat-fab")) return;
        document.body.append(buildBackdrop(), buildFab(), buildDrawer());
        document.addEventListener("keydown", handleGlobalKeydown);
        updateProviderBadge("Checking AI mode...", "muted");
        pingHealth();
    }

    function ensureMounted() {
        if (!document.getElementById("mira-chat-fab")) init();
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }

    function mascotSvg(size) {
        // MIRA identity: friendly AI face (navy) with blue eyes + smile, framed by
        // red segmented radar/tech rings. Readable down to ~22px for the FAB/avatars.
        return `<svg viewBox="0 0 64 64" width="${size}" height="${size}" aria-hidden="true">
            <g fill="none" stroke="#e8392f" stroke-linecap="round">
                <circle cx="32" cy="32" r="29" stroke-width="2.4" stroke-dasharray="58 18 30 14"/>
                <circle cx="32" cy="32" r="24" stroke-width="2" stroke-dasharray="42 16 22 12" opacity="0.92"/>
                <circle cx="32" cy="32" r="19.5" stroke-width="1.5" stroke-dasharray="26 18 16 22" opacity="0.72"/>
            </g>
            <ellipse cx="32" cy="33" rx="17.5" ry="11.2" fill="#2c3a4d"/>
            <circle cx="26" cy="31" r="3.4" fill="#46b6ea"/>
            <circle cx="38" cy="31" r="3.4" fill="#46b6ea"/>
            <path d="M26.4 36.4c1.7 2.1 3.7 3.1 5.6 3.1s3.9-1 5.6-3.1" fill="none" stroke="#46b6ea" stroke-width="2.4" stroke-linecap="round"/>
        </svg>`;
    }

    window.getMiraMascotSvg = mascotSvg;
    window.getMiraProviderStatus = function getMiraProviderStatus() {
        return { ...state.providerStatus };
    };
    window.openMiraChat = function openMiraChat(prompt, options) {
        ensureMounted();
        const settings = options && typeof options === "object" ? options : {};
        openDrawer();
        const input = document.getElementById("mira-chat-input");
        const text = String(prompt || "").trim();
        if (!text) {
            input?.focus();
            return;
        }
        if (settings.send === false) {
            if (input) {
                input.value = text;
                input.focus();
            }
            return;
        }
        send(text);
    };
    window.clearMiraChat = function clearMiraChat() {
        ensureMounted();
        clearChat();
    };

    function buildBackdrop() {
        const backdrop = el("div", "mira-chat-backdrop");
        backdrop.id = "mira-chat-backdrop";
        backdrop.hidden = true;
        backdrop.addEventListener("click", closeDrawer);
        return backdrop;
    }

    function buildFab() {
        const fab = el("button", "mira-fab");
        fab.id = "mira-chat-fab";
        fab.type = "button";
        fab.title = "Ask MIRA";
        fab.setAttribute("aria-label", "Ask MIRA");
        fab.innerHTML = mascotSvg(38);
        fab.append(el("span", "mira-fab-label", "Ask MIRA"), el("span", "mira-fab-tooltip", "Ask MIRA"));
        fab.addEventListener("click", toggleDrawer);
        return fab;
    }

    function buildDrawer() {
        const drawer = el("section", "mira-chat-drawer");
        drawer.id = "mira-chat-drawer";
        drawer.hidden = true;

        const head = el("div", "mira-chat-head");
        const brand = el("div", "mira-chat-brand");
        const brandIcon = el("div", "mira-chat-brand-icon");
        brandIcon.innerHTML = mascotSvg(28);
        const brandText = el("div", "mira-chat-brand-copy");
        brandText.append(el("strong", null, "Ask MIRA"), el("span", null, "Read-only maintenance intelligence assistant"));
        brand.append(brandIcon, brandText);

        const status = el("div", "mira-chat-statuses");
        status.append(
            badge("Verified Data", "good"),
            badge("Read-only", "neutral"),
            badge("Checking AI mode...", "muted", "mira-chat-provider-badge")
        );

        const actions = el("div", "mira-chat-actions");
        const minimize = iconButton("Minimise", "\u2212", () => closeDrawer());
        const clear = iconButton("Clear chat", "Clear", clearChat);
        const close = iconButton("Close", "\u00D7", closeDrawer);
        actions.append(minimize, clear, close);

        head.append(brand, actions);

        const modeBar = buildModeBar();

        // Messages scroll area. The starter panel (suggested prompts / KPI picker)
        // lives INSIDE this area so the conversation always gets the full height.
        const log = el("div", "mira-chat-log");
        log.id = "mira-chat-log";
        log.append(welcomeMessage(), buildInlinePanel("chat"));

        const form = el("form", "mira-chat-form");
        const textarea = el("textarea", "mira-chat-input");
        textarea.id = "mira-chat-input";
        textarea.placeholder = "Ask MIRA about downtime, PM, spare parts, or follow-up actions";
        textarea.rows = 2;
        textarea.addEventListener("keydown", handleInputKeydown);
        const sendBtn = el("button", "mira-chat-send", "Send");
        sendBtn.type = "submit";
        form.append(textarea, sendBtn);
        form.addEventListener("submit", function (event) {
            event.preventDefault();
            const value = textarea.value;
            textarea.value = "";
            send(value);
        });

        const footerNote = el("div", "mira-chat-footer-note", "MIRA uses verified dashboard data only.");

        drawer.append(head, status, modeBar, log, form, footerNote);
        return drawer;
    }

    function buildModeBar() {
        const bar = el("div", "mira-chat-mode-bar");
        const chatTab = el("button", "mira-chat-mode-tab is-active", "Chat Q&A");
        chatTab.type = "button"; chatTab.dataset.mode = "chat";
        const kpiTab = el("button", "mira-chat-mode-tab", "KPI Analysis");
        kpiTab.type = "button"; kpiTab.dataset.mode = "kpi";
        [chatTab, kpiTab].forEach((tab) => tab.addEventListener("click", () => setMode(tab.dataset.mode)));
        bar.append(chatTab, kpiTab);
        return bar;
    }

    // Starter panel shown inside the message area: suggested prompts (Chat Q&A)
    // or the maintenance-area picker (KPI Analysis). Kept in the scroll area so
    // it never steals height from the conversation.
    function buildInlinePanel(mode) {
        const m = mode || state.mode;
        const wrap = el("div", "mira-chat-inline mira-chat-inline-" + m);
        wrap.id = "mira-chat-inline";
        if (m === "kpi") {
            const top = el("div", "mira-chat-kpi-top");
            top.append(
                el("div", "mira-chat-inline-title", "KPI Analysis"),
                el("p", "mira-chat-inline-copy", "Select real dashboard KPI areas for MIRA to analyse using the current period and stage.")
            );
            wrap.append(top);
            KPI_CATEGORIES.forEach((category) => {
                const group = el("div", "mira-chat-kpi-group");
                group.append(el("div", "mira-chat-kpi-category", category));
                const grid = el("div", "mira-chat-kpi-grid");
                KPI_REGISTRY.filter((item) => item.category === category).forEach((item) => {
                    const option = el("label", "mira-chat-kpi-option");
                    const cb = el("input");
                    cb.type = "checkbox";
                    cb.value = item.id;
                    option.append(cb, el("span", "mira-chat-kpi-check"), el("span", "mira-chat-kpi-label", item.label));
                    grid.append(option);
                });
                group.append(grid);
                wrap.append(group);
            });
            const analyze = el("button", "mira-chat-kpi-analyze", "Analyse selected");
            analyze.type = "button";
            analyze.addEventListener("click", analyseKpis);
            wrap.append(analyze);
        } else {
            wrap.append(el("div", "mira-chat-inline-title", "Try asking"));
            const chips = el("div", "mira-chat-chips");
            PROMPTS.forEach((prompt) => {
                const chip = el("button", "mira-chat-chip", prompt);
                chip.type = "button";
                chip.addEventListener("click", () => send(prompt));
                chips.append(chip);
            });
            wrap.append(chips);
        }
        return wrap;
    }

    function hasConversation() {
        const log = document.getElementById("mira-chat-log");
        return !!(log && log.querySelector(".mira-msg-row-user"));
    }

    function renderInlinePanel(mode) {
        const log = document.getElementById("mira-chat-log");
        if (!log) return;
        document.getElementById("mira-chat-inline")?.remove();
        const m = mode || state.mode;
        // Suggested prompts only while the chat is fresh; the KPI picker stays
        // available because choosing areas is the whole point of that mode.
        if (m === "kpi" || !hasConversation()) {
            log.append(buildInlinePanel(m));
            log.scrollTop = log.scrollHeight;
        }
    }

    function setMode(mode) {
        state.mode = mode;
        document.querySelectorAll(".mira-chat-mode-tab").forEach((tab) => {
            tab.classList.toggle("is-active", tab.dataset.mode === mode);
        });
        renderInlinePanel(mode);
    }

    function analyseKpis() {
        const selected = Array.from(document.querySelectorAll("#mira-chat-inline input:checked")).map((cb) => cb.value);
        if (!selected.length) return;
        const picked = selected.map((id) => KPI_REGISTRY.find((item) => item.id === id)).filter(Boolean);
        const names = picked.map((item) => item.label).join(", ");
        const prompts = picked.map((item) => item.prompt).filter(Boolean).join(" ");
        const question = `KPI Analysis: analyse selected dashboard KPIs: ${names}. ${prompts}`;
        setMode("chat");
        send(question, {
            mode: "kpi_analysis",
            selectedKpis: selected,
            selectedKpiLabels: picked.map((item) => item.label),
        });
    }

    function welcomeMessage() {
        return buildAssistantMessage({
            period_used: "Period used: YTD " + new Date().getFullYear(),
            answer: "I can summarise verified maintenance performance, explain PM issues, highlight open MR, and point out spare-parts consumption trends.",
            insight: [
                "Prompt chips send the exact question shown.",
                "If you name a month or FY, MIRA will use that period instead of the default YTD view.",
            ],
            recommended_follow_up: [
                "Ask Which asset has the most MR for workload concentration.",
                "Ask What should be followed up today for open MR, overdue PM, and backlog.",
            ],
            provider_mode_label: "Rule-based fallback",
            read_only: true,
        });
    }

    function badge(text, tone, id) {
        const node = el("span", "mira-chat-badge mira-chat-badge-" + (tone || "neutral"), text);
        if (id) node.id = id;
        return node;
    }

    function iconButton(label, text, onClick) {
        const node = el("button", "mira-chat-iconbtn", text);
        node.type = "button";
        node.setAttribute("aria-label", label);
        node.title = label;
        node.addEventListener("click", onClick);
        return node;
    }

    function toggleDrawer() {
        if (state.open) closeDrawer();
        else openDrawer();
    }

    function openDrawer() {
        state.open = true;
        const backdrop = document.getElementById("mira-chat-backdrop");
        const drawer = document.getElementById("mira-chat-drawer");
        const fab = document.getElementById("mira-chat-fab");
        if (backdrop) backdrop.hidden = false;
        if (drawer) drawer.hidden = false;
        requestAnimationFrame(() => {
            backdrop?.classList.add("is-open");
            drawer?.classList.add("is-open");
            fab?.classList.add("mira-fab-open");
        });
        document.getElementById("mira-chat-input")?.focus();
    }

    function closeDrawer() {
        state.open = false;
        const backdrop = document.getElementById("mira-chat-backdrop");
        const drawer = document.getElementById("mira-chat-drawer");
        const fab = document.getElementById("mira-chat-fab");
        backdrop?.classList.remove("is-open");
        drawer?.classList.remove("is-open");
        fab?.classList.remove("mira-fab-open");
        window.setTimeout(() => {
            if (!state.open) {
                if (backdrop) backdrop.hidden = true;
                if (drawer) drawer.hidden = true;
            }
        }, 180);
    }

    function clearChat() {
        const log = document.getElementById("mira-chat-log");
        if (!log) return;
        log.innerHTML = "";
        log.append(welcomeMessage());
        renderInlinePanel(state.mode);
    }

    function handleGlobalKeydown(event) {
        if (event.key === "Escape" && state.open) closeDrawer();
    }

    function handleInputKeydown(event) {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            const input = document.getElementById("mira-chat-input");
            if (!input) return;
            const value = input.value;
            input.value = "";
            send(value);
        }
    }

    function appendMessage(node) {
        const log = document.getElementById("mira-chat-log");
        if (!log) return;
        log.append(node);
        log.scrollTop = log.scrollHeight;
    }

    function buildUserMessage(text) {
        const row = el("div", "mira-msg-row mira-msg-row-user");
        const bubble = el("div", "mira-msg-bubble mira-msg-bubble-user");
        bubble.append(el("p", "mira-msg-text", text));
        row.append(bubble);
        return row;
    }

    function buildAssistantMessage(payload) {
        // Route asset breakdown/cost reports to dedicated renderer
        if (payload && payload.response_type === "asset_report") {
            return buildAssetReportMessage(payload);
        }

        const row = el("div", "mira-msg-row mira-msg-row-bot");
        const avatar = el("div", "mira-msg-avatar");
        avatar.innerHTML = mascotSvg(26);
        const bubble = el("div", "mira-msg-bubble mira-msg-bubble-bot");

        const header = el("div", "mira-msg-head");
        const headBadges = el("div", "mira-msg-meta");
        // Period chip — always show
        if (payload.period_used || payload.period) {
            headBadges.append(badge(payload.period_used || ("Period used: " + payload.period), "soft"));
        }
        // Confidence chip — present only when backend supplies it
        if (payload.confidence) {
            const c = payload.confidence;
            const tone = c.band === "High" ? "good" : c.band === "Med" ? "soft" : "neutral";
            headBadges.append(badge(c.label || c.band, tone, "mira-confidence-chip"));
        }
        // provider_mode_label and read_only moved to "View details" expander below
        header.append(headBadges);
        bubble.append(header);

        const isKpiAnalysis = payload.mode === "kpi_analysis" || Array.isArray(payload.kpi_analysis_sections);
        bubble.append(sectionTitle(isKpiAnalysis ? (Array.isArray(payload.kpi_analysis_sections) && payload.kpi_analysis_sections.length > 1 ? "Overall Summary" : "KPI Summary") : "Answer"));
        bubble.append(paragraph(payload.answer || "No verified answer was available."));

        if (Array.isArray(payload.kpi_analysis_sections) && payload.kpi_analysis_sections.length) {
            bubble.append(buildKpiAnalysisSections(payload.kpi_analysis_sections));
        }

        if (Array.isArray(payload.key_numbers_used) && payload.key_numbers_used.length) {
            bubble.append(listSection("Key Numbers Used", payload.key_numbers_used));
        }

        if (Array.isArray(payload.insight) && payload.insight.length) {
            bubble.append(listSection("Insight", payload.insight));
        }

        if (payload.theme_analysis && Array.isArray(payload.theme_analysis.example_descriptions) && payload.theme_analysis.example_descriptions.length) {
            bubble.append(listSection("Description Examples", payload.theme_analysis.example_descriptions));
        }

        const warnings = (((payload.view_data_used || {}).data_warnings) || []).filter(Boolean);
        if (warnings.length) {
            bubble.append(listSection("Data Notes", warnings, "warning"));
        }

        if (Array.isArray(payload.recommended_follow_up) && payload.recommended_follow_up.length) {
            bubble.append(listSection("Recommended Follow-Up", payload.recommended_follow_up));
        }

        // "View details" expander: interpretation echo + provider/read-only tags
        if (payload.interpretation || payload.provider_mode_label || payload.read_only) {
            bubble.append(buildAnswerDetails(payload));
        }

        if (payload.view_data_used) {
            bubble.append(buildDataUsed(payload.view_data_used));
        }

        row.append(avatar, bubble);
        return row;
    }

    // ── Asset Report rich renderer ──────────────────────────────────────────

    function buildAssetReportMessage(payload) {
        const row = el("div", "mira-msg-row mira-msg-row-bot");
        const avatar = el("div", "mira-msg-avatar");
        avatar.innerHTML = mascotSvg(26);
        const bubble = el("div", "mira-msg-bubble mira-msg-bubble-bot mira-ar-bubble");

        // ── Header badges ──────────────────────────────────────────────────
        const head = el("div", "mira-msg-head");
        const meta = el("div", "mira-msg-meta");
        meta.append(badge(payload.period_used || ("Period: " + (payload.period_label || "")), "soft"));
        if (payload.stage_label && payload.stage_label !== "All Stages") {
            meta.append(badge(payload.stage_label, "neutral"));
        }
        meta.append(badge("Asset Report", "good"));
        head.append(meta);
        bubble.append(head);

        // ── Title ──────────────────────────────────────────────────────────
        const titleEl = el("div", "mira-ar-title", payload.title || "Asset Breakdown Summary");
        bubble.append(titleEl);

        // ── MIRA summary wording ───────────────────────────────────────────
        bubble.append(el("p", "mira-msg-text mira-ar-summary", payload.answer || ""));

        // ── Key stats row ──────────────────────────────────────────────────
        const statsRow = el("div", "mira-ar-stats");
        statsRow.append(
            arStat("Actual Breakdown MR", payload.total_counted ?? "--"),
            arStat("Excluded Rows", payload.total_excluded ?? "--"),
            arStat("Highest MR Unit",
                payload.highest_mr_unit
                    ? payload.highest_mr_unit.replace("Combi Oven No.", "#")
                    : "--"),
        );
        if (payload.include_cost && payload.total_estimated_cost != null) {
            statsRow.append(
                arStat("Est. Repair Cost (PO-based)",
                    payload.total_estimated_cost > 0
                        ? "THB " + Number(payload.total_estimated_cost).toLocaleString(undefined, { maximumFractionDigits: 0 })
                        : "No PO match")
            );
        }
        bubble.append(statsRow);

        // ── Unit table ─────────────────────────────────────────────────────
        const units = payload.units_table || [];
        if (units.length) {
            const tWrap = el("div", "mira-ar-section");
            tWrap.append(sectionTitle("Breakdown by Unit"));
            const table = el("table", "mira-ar-table");
            const thead = el("thead");
            const headerRow = el("tr");
            const headers = ["Unit", "MR Count", "Main Issues"];
            if (payload.include_cost) headers.push("Est. Cost");
            headers.forEach((h) => headerRow.append(el("th", null, h)));
            thead.append(headerRow);
            table.append(thead);

            const tbody = el("tbody");
            units.forEach((unit) => {
                const tr = el("tr");
                tr.append(
                    el("td", "mira-ar-td-unit", unit.unit),
                    el("td", "mira-ar-td-count", String(unit.mr_count ?? "--")),
                    (() => {
                        const td = el("td", "mira-ar-td-issues");
                        const issues = unit.main_issues || [];
                        if (issues.length) {
                            issues.forEach((iss) => {
                                const chip = el("span", "mira-ar-issue-chip", iss);
                                td.append(chip);
                            });
                        } else {
                            td.textContent = "—";
                        }
                        return td;
                    })(),
                );
                if (payload.include_cost) {
                    tr.append(el("td", "mira-ar-td-cost",
                        unit.estimated_cost_formatted || (unit.estimated_cost > 0
                            ? "THB " + Number(unit.estimated_cost).toLocaleString(undefined, { maximumFractionDigits: 0 })
                            : "No PO match")));
                }
                tbody.append(tr);
            });
            table.append(tbody);
            tWrap.append(table);
            bubble.append(tWrap);
        }

        // ── Top issue patterns ─────────────────────────────────────────────
        const patterns = payload.top_issue_patterns || [];
        if (patterns.length) {
            const patWrap = el("div", "mira-ar-section");
            patWrap.append(sectionTitle("Common Issue Patterns"));
            const patList = el("div", "mira-ar-pattern-list");
            patterns.forEach((p) => {
                const chip = el("span", "mira-ar-pattern-chip");
                chip.append(el("span", "mira-ar-pattern-name", p.issue));
                chip.append(el("span", "mira-ar-pattern-count", String(p.count)));
                patList.append(chip);
            });
            patWrap.append(patList);
            bubble.append(patWrap);
        }

        // ── Data notes / warnings ──────────────────────────────────────────
        const notes = (payload.data_notes || []).concat(payload.data_warnings || []).filter(Boolean);
        if (notes.length) {
            bubble.append(listSection("Notes & Warnings", notes, "warning"));
        }
        if (payload.cost_basis_note && payload.include_cost) {
            const noteEl = el("p", "mira-ar-cost-note", payload.cost_basis_note);
            bubble.append(noteEl);
        }

        // ── Action buttons ─────────────────────────────────────────────────
        const actions = el("div", "mira-ar-actions");

        // Copy summary
        const copyBtn = el("button", "mira-ar-btn", "Copy Summary");
        copyBtn.type = "button";
        copyBtn.addEventListener("click", () => {
            const text = buildAssetReportText(payload);
            navigator.clipboard.writeText(text).then(() => {
                copyBtn.textContent = "Copied!";
                setTimeout(() => { copyBtn.textContent = "Copy Summary"; }, 2000);
            }).catch(() => {
                copyBtn.textContent = "Copy failed";
                setTimeout(() => { copyBtn.textContent = "Copy Summary"; }, 2000);
            });
        });
        actions.append(copyBtn);

        // Email format
        const emailBtn = el("button", "mira-ar-btn", "Email Format");
        emailBtn.type = "button";
        emailBtn.addEventListener("click", () => {
            showEmailModal(payload);
        });
        actions.append(emailBtn);

        // View evidence
        const evidenceBtn = el("button", "mira-ar-btn mira-ar-btn-outline", "View Evidence");
        evidenceBtn.type = "button";
        const evidencePanel = buildEvidencePanel(payload);
        evidencePanel.hidden = true;
        evidenceBtn.addEventListener("click", () => {
            evidencePanel.hidden = !evidencePanel.hidden;
            evidenceBtn.textContent = evidencePanel.hidden ? "View Evidence" : "Hide Evidence";
        });
        actions.append(evidenceBtn);

        // Show excluded rows
        const exclBtn = el("button", "mira-ar-btn mira-ar-btn-outline", "Show Excluded Rows");
        exclBtn.type = "button";
        const exclPanel = buildExcludedRowsPanel(payload);
        exclPanel.hidden = true;
        exclBtn.addEventListener("click", () => {
            exclPanel.hidden = !exclPanel.hidden;
            exclBtn.textContent = exclPanel.hidden ? "Show Excluded Rows" : "Hide Excluded Rows";
        });
        actions.append(exclBtn);

        // Export CSV
        const exportBtn = el("button", "mira-ar-btn mira-ar-btn-outline", "Export CSV");
        exportBtn.type = "button";
        exportBtn.addEventListener("click", () => exportAssetReportCsv(payload));
        actions.append(exportBtn);

        bubble.append(actions);
        bubble.append(evidencePanel);
        bubble.append(exclPanel);

        row.append(avatar, bubble);
        return row;
    }

    function arStat(label, value) {
        const box = el("div", "mira-ar-stat");
        box.append(el("div", "mira-ar-stat-value", String(value)));
        box.append(el("div", "mira-ar-stat-label", label));
        return box;
    }

    function buildEvidencePanel(payload) {
        const panel = el("div", "mira-ar-evidence-panel");
        const ev = (payload.evidence || {});

        // Counted MR rows
        const counted = ev.counted_rows || [];
        const cTitle = el("div", "mira-ar-ev-section-title",
            `Counted Breakdown MR (${counted.length}${counted.length === 50 ? "+" : ""})`);
        panel.append(cTitle);

        if (counted.length) {
            const table = el("table", "mira-ar-ev-table");
            const tr = el("tr");
            ["Date", "Unit", "MR#", "Asset", "Description", "Issue"].forEach((h) => {
                tr.append(el("th", null, h));
            });
            const thead = el("thead");
            thead.append(tr);
            table.append(thead);
            const tbody = el("tbody");
            counted.forEach((r) => {
                const row = el("tr");
                row.append(
                    el("td", null, r.date || "—"),
                    el("td", null, (r.unit || "?").replace("Combi Oven No.", "#")),
                    el("td", null, r.mr_number || "—"),
                    el("td", null, (r.asset_name || "").substring(0, 24)),
                    el("td", "mira-ar-ev-desc", (r.description || "").substring(0, 80)),
                    el("td", null, (r.issue_cluster || "").replace("Other / Unclear", "Other")),
                );
                tbody.append(row);
            });
            table.append(tbody);
            panel.append(table);
        } else {
            panel.append(el("p", "mira-ar-ev-empty", "No counted breakdown MR rows found."));
        }

        // PO cost rows
        if (payload.include_cost) {
            const poRows = (ev.po_rows_included || []);
            const poTitle = el("div", "mira-ar-ev-section-title",
                `PO Cost Rows Included (${poRows.length})`);
            panel.append(poTitle);
            if (poRows.length) {
                const table = el("table", "mira-ar-ev-table");
                const tr = el("tr");
                ["PO#", "Item", "Unit", "Supplier", "Value (THB)"].forEach((h) => tr.append(el("th", null, h)));
                const thead = el("thead"); thead.append(tr); table.append(thead);
                const tbody = el("tbody");
                poRows.forEach((r) => {
                    const row = el("tr");
                    row.append(
                        el("td", null, r.po_number || "—"),
                        el("td", null, (r.item_name || "").substring(0, 30)),
                        el("td", null, (r.unit || "—").replace("Combi Oven No.", "#")),
                        el("td", null, r.supplier || "—"),
                        el("td", "mira-ar-ev-num",
                            r.total_value != null
                                ? Number(r.total_value).toLocaleString(undefined, { maximumFractionDigits: 0 })
                                : "—"),
                    );
                    tbody.append(row);
                });
                table.append(tbody);
                panel.append(table);
            } else {
                panel.append(el("p", "mira-ar-ev-empty", "No PO cost rows directly matched to units."));
            }
        }

        return panel;
    }

    function buildExcludedRowsPanel(payload) {
        const panel = el("div", "mira-ar-evidence-panel");
        const ev = (payload.evidence || {});
        const excl = ev.excluded_rows || [];

        const title = el("div", "mira-ar-ev-section-title",
            `Excluded Rows (${excl.length}${excl.length === 30 ? "+" : ""}) — not counted in breakdown MR`);
        panel.append(title);

        // Summary by reason
        const excSummary = payload.exclusion_summary || [];
        if (excSummary.length) {
            const sumWrap = el("div", "mira-ar-excl-summary");
            excSummary.forEach((item) => {
                const chip = el("span", "mira-ar-excl-chip");
                chip.append(el("span", null, item.reason));
                chip.append(el("span", "mira-ar-excl-count", String(item.count)));
                sumWrap.append(chip);
            });
            panel.append(sumWrap);
        }

        if (excl.length) {
            const table = el("table", "mira-ar-ev-table");
            const tr = el("tr");
            ["Date", "MR#", "Asset", "Description", "Excluded Because"].forEach((h) => tr.append(el("th", null, h)));
            const thead = el("thead"); thead.append(tr); table.append(thead);
            const tbody = el("tbody");
            excl.forEach((r) => {
                const row = el("tr");
                row.append(
                    el("td", null, r.date || "—"),
                    el("td", null, r.mr_number || "—"),
                    el("td", null, (r.asset_name || "").substring(0, 24)),
                    el("td", "mira-ar-ev-desc", (r.description || "").substring(0, 80)),
                    el("td", "mira-ar-excl-reason", r.exclusion_reason || "—"),
                );
                tbody.append(row);
            });
            table.append(tbody);
            panel.append(table);
        } else {
            panel.append(el("p", "mira-ar-ev-empty",
                "No excluded rows found — all matched rows were counted as actual breakdowns."));
        }

        return panel;
    }

    function buildAssetReportText(payload) {
        const lines = [];
        lines.push(payload.title || "Asset Breakdown Summary");
        lines.push("");
        lines.push(payload.answer || "");
        lines.push("");
        lines.push(`Total actual breakdown MR: ${payload.total_counted ?? "--"}`);
        lines.push(`Excluded rows: ${payload.total_excluded ?? "--"}`);
        if (payload.highest_mr_unit) {
            lines.push(`Highest MR unit: ${payload.highest_mr_unit} (${payload.highest_mr_count} MR)`);
        }
        if (payload.include_cost && payload.total_estimated_cost != null) {
            lines.push(`Estimated PO-based repair / purchase cost: THB ${Number(payload.total_estimated_cost).toLocaleString(undefined, { maximumFractionDigits: 0 })}`);
        }
        lines.push("");
        lines.push("Unit Breakdown:");
        (payload.units_table || []).forEach((u) => {
            let line = `  ${u.unit}: ${u.mr_count} MR — ${(u.main_issues || []).join(", ") || "no dominant pattern"}`;
            if (payload.include_cost) line += ` | ${u.estimated_cost_formatted || "No PO match"}`;
            lines.push(line);
        });
        lines.push("");
        const patterns = payload.top_issue_patterns || [];
        if (patterns.length) {
            lines.push("Common issue patterns: " + patterns.map((p) => `${p.issue} (${p.count})`).join(", "));
        }
        lines.push("");
        (payload.data_notes || []).forEach((n) => lines.push("Note: " + n));
        if (payload.cost_basis_note && payload.include_cost) lines.push(payload.cost_basis_note);
        return lines.join("\n");
    }

    function showEmailModal(payload) {
        const text = buildEmailText(payload);
        const overlay = el("div", "mira-ar-modal-overlay");
        const modal = el("div", "mira-ar-modal");
        const closeBtn = el("button", "mira-ar-modal-close", "×");
        closeBtn.type = "button";
        closeBtn.addEventListener("click", () => overlay.remove());
        overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });

        const title = el("div", "mira-ar-modal-title", "Email-Ready Summary");
        const ta = el("textarea", "mira-ar-modal-textarea");
        ta.readOnly = true;
        ta.value = text;
        ta.rows = 14;

        const copyBtn = el("button", "mira-ar-btn mira-ar-modal-copy", "Copy to Clipboard");
        copyBtn.type = "button";
        copyBtn.addEventListener("click", () => {
            navigator.clipboard.writeText(text).then(() => {
                copyBtn.textContent = "Copied!";
                setTimeout(() => { copyBtn.textContent = "Copy to Clipboard"; }, 2000);
            });
        });

        modal.append(closeBtn, title, ta, copyBtn);
        overlay.append(modal);
        document.body.append(overlay);
        ta.focus();
        ta.select();
    }

    function buildEmailText(payload) {
        const stage = payload.stage_label && payload.stage_label !== "All Stages"
            ? payload.stage_label + " " : "";
        const machine = payload.machine || "Equipment";
        const period = payload.period_label || "";
        const lines = [];
        lines.push(`Subject: ${stage}${machine} Maintenance Breakdown — ${period}`);
        lines.push("");
        lines.push(`Dear Team,`);
        lines.push("");
        lines.push(`Please find below a breakdown summary for ${stage}${machine} covering ${period}.`);
        lines.push("");
        lines.push("SUMMARY");
        lines.push("──────────────────────────────────────────");
        lines.push(payload.answer || "");
        lines.push("");
        lines.push("UNIT BREAKDOWN");
        lines.push("──────────────────────────────────────────");
        lines.push(`${"Unit".padEnd(22)} ${"MR Count".padEnd(10)} ${"Main Issues"}`);
        lines.push("─".repeat(70));
        (payload.units_table || []).forEach((u) => {
            const issues = (u.main_issues || []).join(", ") || "—";
            let row = `${u.unit.padEnd(22)} ${String(u.mr_count).padEnd(10)} ${issues}`;
            if (payload.include_cost) row += ` | ${u.estimated_cost_formatted || "No PO match"}`;
            lines.push(row);
        });
        lines.push("");
        const patterns = payload.top_issue_patterns || [];
        if (patterns.length) {
            lines.push("COMMON ISSUE PATTERNS");
            lines.push(patterns.map((p) => `  • ${p.issue} (${p.count} MR)`).join("\n"));
            lines.push("");
        }
        lines.push("NOTES");
        lines.push("──────────────────────────────────────────");
        (payload.data_notes || []).forEach((n) => lines.push(`• ${n}`));
        if (payload.cost_basis_note && payload.include_cost) {
            lines.push(`• ${payload.cost_basis_note}`);
        }
        lines.push(`• ${payload.total_excluded ?? 0} rows excluded (PM, facility, support work — not counted above)`);
        lines.push("");
        lines.push("This report is based on available MR/PO records and requires validation before use in formal management reporting.");
        lines.push("");
        lines.push(`Generated by MIRA — ${new Date().toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" })}`);
        return lines.join("\n");
    }

    function exportAssetReportCsv(payload) {
        const rows = [];
        rows.push(["Unit", "MR Count", "Main Issues", "Latest MR Date",
            ...(payload.include_cost ? ["Est. Cost (THB)"] : [])]);
        (payload.units_table || []).forEach((u) => {
            rows.push([
                u.unit,
                u.mr_count,
                (u.main_issues || []).join("; "),
                u.latest_mr || "",
                ...(payload.include_cost ? [u.estimated_cost != null ? Number(u.estimated_cost).toFixed(0) : ""] : []),
            ]);
        });
        // Add excluded summary
        rows.push([]);
        rows.push(["Excluded Rows", "Count"]);
        (payload.exclusion_summary || []).forEach((item) => {
            rows.push([item.reason, item.count]);
        });

        const csv = rows.map((r) =>
            r.map((cell) => '"' + String(cell ?? "").replace(/"/g, '""') + '"').join(",")
        ).join("\r\n");

        const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8;" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        const safeName = (payload.machine || "asset").replace(/\s+/g, "-").toLowerCase();
        const safePeriod = (payload.period_label || "report").replace(/\s+/g, "-").toLowerCase();
        a.download = `mira-${safeName}-${safePeriod}.csv`;
        a.click();
        URL.revokeObjectURL(url);
    }

    // ── End asset report renderer ───────────────────────────────────────────

    function buildAnswerDetails(payload) {
        const details = el("details", "mira-answer-details");
        const summary = el("summary", null, "View details");
        const body = el("div", "mira-answer-details-body");

        const interp = payload.interpretation || {};
        // Interpretation echo
        if (interp.resolved_as) {
            const r = el("div", "mira-detail-row");
            r.append(el("span", "mira-detail-label", "Read as:"));
            r.append(el("span", "mira-detail-value", interp.resolved_as));
            body.append(r);
        } else if (interp.text) {
            const r = el("div", "mira-detail-row");
            r.append(el("span", "mira-detail-label", "Interpreted as:"));
            r.append(el("span", "mira-detail-value", interp.text));
            body.append(r);
        }

        // Provider mode label
        if (payload.provider_mode_label) {
            const r = el("div", "mira-detail-row");
            r.append(el("span", "mira-detail-label", "Source:"));
            r.append(badge(payload.provider_mode_label, "neutral"));
            body.append(r);
        }

        // Read-only tag
        if (payload.read_only) {
            const r = el("div", "mira-detail-row");
            r.append(badge("Read-only", "neutral"));
            body.append(r);
        }

        details.append(summary, body);
        return details;
    }

    function buildThinkingMessage() {
        const row = el("div", "mira-msg-row mira-msg-row-bot");
        row.id = "mira-chat-thinking";
        const avatar = el("div", "mira-msg-avatar");
        avatar.innerHTML = mascotSvg(26);
        const bubble = el("div", "mira-msg-bubble mira-msg-bubble-bot mira-msg-bubble-thinking");
        bubble.append(sectionTitle("MIRA is checking verified data"));
        const dots = el("div", "mira-typing");
        dots.append(el("span"), el("span"), el("span"));
        bubble.append(dots);
        row.append(avatar, bubble);
        return row;
    }

    function sectionTitle(text) {
        return el("div", "mira-msg-section-title", text);
    }

    function paragraph(text) {
        return el("p", "mira-msg-text", text);
    }

    function listSection(title, items, tone) {
        const wrap = el("div", "mira-msg-section");
        wrap.append(sectionTitle(title));
        const list = el("ul", "mira-msg-list" + (tone === "warning" ? " is-warning" : ""));
        items.forEach((item) => {
            if (!item) return;
            list.append(el("li", null, String(item)));
        });
        wrap.append(list);
        return wrap;
    }

    function buildKpiAnalysisSections(sections) {
        const wrap = el("div", "mira-kpi-analysis-wrap");
        wrap.append(sectionTitle(sections.length > 1 ? "Findings By Selected KPI" : "Selected KPI Detail"));
        sections.forEach((section) => {
            const card = el("article", "mira-kpi-section-card");
            card.append(el("div", "mira-kpi-section-title", section.title || "Selected KPI"));
            if (section.summary) {
                card.append(paragraph(section.summary));
            }
            card.append(compactList("Key Findings", section.key_findings));
            card.append(compactList("Issue Focus Areas", section.issue_focus_areas));
            card.append(compactList("Predictive / Risk Indicators", section.risk_indicators));
            card.append(compactList("Follow-Up Actions", section.follow_up_actions));
            if (Array.isArray(section.data_gaps) && section.data_gaps.length) {
                card.append(compactList("Data Gaps", section.data_gaps, "warning"));
            }
            wrap.append(card);
        });
        return wrap;
    }

    function compactList(title, items, tone) {
        const clean = Array.isArray(items) ? items.filter(Boolean) : [];
        const block = el("div", "mira-kpi-compact-block" + (tone === "warning" ? " is-warning" : ""));
        block.append(el("div", "mira-kpi-compact-title", title));
        if (!clean.length) {
            block.append(el("p", "mira-kpi-empty", "-"));
            return block;
        }
        const list = el("ul", "mira-kpi-compact-list");
        clean.slice(0, 6).forEach((item) => list.append(el("li", null, String(item))));
        block.append(list);
        return block;
    }

    function buildDataUsed(viewData) {
        const details = el("details", "mira-data-used");
        const summary = el("summary", null, "View Data Used");
        const body = el("div", "mira-data-used-body");
        body.append(
            dataBlock("Source dataset / table", viewData.source_tables),
            dataBlock("Period / filter used", viewData.filters_applied),
            dataBlock("Rows loaded", viewData.rows_loaded),
            dataBlock("Rows after filter", viewData.rows_after_filter),
            dataBlock("KPI values used", viewData.kpi_values_used)
        );
        if (viewData.last_refreshed) {
            body.append(dataBlock("Last refreshed", [viewData.last_refreshed]));
        }
        details.append(summary, body);
        return details;
    }

    function dataBlock(label, items) {
        const wrap = el("div", "mira-data-block");
        wrap.append(el("div", "mira-data-block-label", label));
        const list = el("ul", "mira-data-block-list");
        const rows = Array.isArray(items) ? items : [];
        if (!rows.length) {
            list.append(el("li", "mira-data-empty", "-"));
        } else {
            rows.forEach((item) => {
                if (item && typeof item === "object" && !Array.isArray(item)) {
                    list.append(el("li", null, `${item.label || "Value"}: ${item.value || ""}`));
                } else if (item) {
                    list.append(el("li", null, String(item)));
                }
            });
        }
        wrap.append(list);
        return wrap;
    }

    function getDashboardFilters() {
        return window.MIRA_DASHBOARD_FILTERS && typeof window.MIRA_DASHBOARD_FILTERS === "object"
            ? window.MIRA_DASHBOARD_FILTERS
            : undefined;
    }

    async function pingHealth() {
        try {
            const response = await fetch(`${API}/health`, { cache: "no-store" });
            if (!response.ok) throw new Error("health");
            const payload = await response.json();
            updateProviderBadge(payload.provider_status || "Rule-based fallback", payload.llm_active ? "good" : "neutral");
        } catch (_err) {
            updateProviderBadge("LLM unavailable", "muted");
        }
    }

    function updateProviderBadge(text, tone) {
        const badgeNode = document.getElementById("mira-chat-provider-badge");
        state.providerStatus = {
            text: text || "Checking AI mode...",
            tone: tone || "neutral",
        };
        window.MIRA_PROVIDER_STATUS = { ...state.providerStatus };
        window.dispatchEvent(new CustomEvent("mira:provider-status", {
            detail: { ...state.providerStatus },
        }));
        if (!badgeNode) return;
        badgeNode.textContent = text;
        badgeNode.className = "mira-chat-badge mira-chat-badge-" + (tone || "neutral");
    }

    async function send(question, options) {
        const trimmed = String(question || "").trim();
        if (!trimmed || state.busy) return;
        const settings = options && typeof options === "object" ? options : {};

        if (!state.open) openDrawer();
        state.busy = true;
        document.getElementById("mira-chat-fab")?.classList.add("is-busy");

        // Conversation started: drop the starter panel so it can't reappear.
        document.getElementById("mira-chat-inline")?.remove();
        appendMessage(buildUserMessage(trimmed));
        appendMessage(buildThinkingMessage());

        try {
            const response = await fetch(`${API}/chat`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                cache: "no-store",
                body: JSON.stringify(buildChatRequest(trimmed, settings)),
            });
            removeThinking();
            if (!response.ok) {
                throw new Error(String(response.status));
            }
            const payload = await response.json();
            updateProviderBadge(payload.provider_mode_label || payload.provider_status || "Rule-based fallback", payload.llm_active ? "good" : "neutral");
            appendMessage(buildAssistantMessage(payload));
        } catch (error) {
            removeThinking();
            const code = String(error && error.message ? error.message : "");
            const message = code === "404"
                ? "MIRA chat is not available on the running backend yet. Please restart the backend and try again."
                : code.toLowerCase().includes("failed to fetch")
                ? "MIRA could not reach /api/mira/chat on this dashboard server. The local backend likely needs a restart."
                : "MIRA could not complete that request because the backend could not be reached. Please try again.";
            appendMessage(buildAssistantMessage({
                answer: message,
                insight: ["The chat UI is still read-only and no dashboard data was changed."],
                recommended_follow_up: ["Refresh the page or restart the local backend if the error continues."],
                provider_mode_label: "LLM unavailable",
                read_only: true,
            }));
            updateProviderBadge("LLM unavailable", "muted");
        } finally {
            state.busy = false;
            document.getElementById("mira-chat-fab")?.classList.remove("is-busy");
        }
    }

    function removeThinking() {
        document.getElementById("mira-chat-thinking")?.remove();
    }

    function buildChatRequest(question, options) {
        const payload = { question, userQuestion: question };
        const filters = getDashboardFilters();
        if (filters) {
            payload.filters = filters;
            payload.selectedPeriod = filters.period_mode;
            payload.selectedYear = filters.year;
            payload.selectedMonth = filters.month;
            payload.selectedStage = filters.stage;
        }
        if (options.mode) payload.mode = options.mode;
        if (Array.isArray(options.selectedKpis)) {
            payload.selected_kpis = options.selectedKpis;
            payload.selectedKpis = options.selectedKpis;
            payload.selectedKpiIds = options.selectedKpis;
        }
        if (Array.isArray(options.selectedKpiLabels)) {
            payload.selected_kpi_labels = options.selectedKpiLabels;
            payload.selectedKpiLabels = options.selectedKpiLabels;
        }
        return payload;
    }
})();
