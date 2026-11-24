(() => {
    "use strict";

    const host = document.querySelector("#interpret-preview");
    const el = ApiLab.element;
    const present = (value) => value !== null && value !== undefined && value !== "";
    const text = (value) => present(value) ? String(value) : "—";
    const rows = (value) => Array.isArray(value) ? value.filter((item) => item && typeof item === "object") : [];
    const reportTypes = {checkup: "体检报告", lab: "检验报告", imaging: "影像报告", microbiome: "菌群报告", unknown: "未知类型"};
    const reportStatuses = {normal: "正常", abnormal: "异常", pending: "待确认", unknown: "未知"};
    const processingStatuses = {complete: "处理完成", partial: "部分读取", unavailable: "无可用结果"};
    const fieldLabels = {itemName: "项目", resultValue: "结果", unit: "单位", referenceRange: "参考范围",
        flag: "异常标记", observedAt: "检测日期", specimen: "标本", method: "方法"};
    const expansionLabels = {findings: "影像所见", diagnosis: "影像诊断", checkupOrgName: "体检机构", checkupTime: "体检时间"};
    const abnormalFlags = new Set(["异常", "偏高", "偏低", "升高", "降低", "高", "低", "↑", "↓", "h", "l", "high", "low", "超标", "缺乏", "危急", "危急值"]);

    function badge(value, labels) {
        const node = el("span", "interpret-badge", text(Object.hasOwn(labels, value) ? labels[value] : value));
        if (["abnormal", "partial", "unavailable"].includes(value)) node.classList.add("interpret-attention");
        return node;
    }

    function pairs(values) {
        const list = el("dl", "interpret-pairs");
        for (const [label, value] of values) {
            const pair = el("div", "interpret-pair");
            pair.append(el("dt", "", label), el("dd", "", text(value)));
            list.append(pair);
        }
        return list;
    }

    function table(items, pending = false) {
        const region = el("div", "interpret-table-wrap");
        region.tabIndex = 0;
        region.setAttribute("role", "region");
        region.setAttribute("aria-label", pending ? "待核实项目表" : "检测结果表");
        const grid = el("table", "interpret-table");
        const header = el("thead");
        const headRow = el("tr");
        const keys = ["itemName", "resultValue", "unit", "referenceRange", "flag", "observedAt",
            ...["specimen", "method"].filter((key) => items.some((item) => present(item[key])))];
        for (const key of keys) headRow.append(el("th", "", fieldLabels[key]));
        if (pending) headRow.append(el("th", "", "缺失字段"), el("th", "", "原因"));
        header.append(headRow);
        const body = el("tbody");
        for (const item of items) {
            const row = el("tr");
            for (const key of keys) {
                const cell = el(key === "itemName" ? "th" : "td", key === "resultValue" ? "interpret-value" : "", text(item[key]));
                if (key === "itemName") cell.setAttribute("scope", "row");
                if (key === "flag" && abnormalFlags.has(String(item.flag).trim().toLowerCase())) {
                    cell.classList.add("interpret-flag");
                }
                row.append(cell);
            }
            if (pending) {
                const missing = Array.isArray(item.missingFields) ? item.missingFields.map((key) =>
                    Object.hasOwn(fieldLabels, key) ? fieldLabels[key] : key).join("、") : "";
                row.append(el("td", "", text(missing)), el("td", "", text(item.reason)));
            }
            body.append(row);
        }
        grid.append(header, body);
        region.append(grid);
        return region;
    }

    function block(title, content) {
        const section = el("section", "interpret-block");
        section.append(el("h3", "", title), content);
        return section;
    }

    function renderContent(parent, report) {
        const observations = rows(report.observations);
        const expansion = rows(report.expansion);
        const mirrored = observations.length === expansion.length && observations.every((item, index) =>
            Object.keys(fieldLabels).every((key) => item[key] === expansion[index][key]));
        if (observations.length || expansion.length) {
            parent.append(block("检测结果", table(observations.length ? observations : expansion)));
        }
        if (observations.length && expansion.length && !mirrored) parent.append(block("扩展检测结果", table(expansion)));
        const pending = rows(report.unresolvedItems);
        if (pending.length) parent.append(block("待核实项目", table(pending, true)));
        if (report.expansion && typeof report.expansion === "object" && !Array.isArray(report.expansion)) {
            const expanded = Object.entries(report.expansion).filter(([, value]) => present(value) && typeof value !== "object");
            for (const [key, value] of expanded.filter(([key]) => key === "findings" || key === "diagnosis")) {
                parent.append(block(expansionLabels[key], el("p", "interpret-prose", text(value))));
            }
            const other = expanded.filter(([key]) => key !== "findings" && key !== "diagnosis");
            if (other.length) parent.append(block("扩展信息", pairs(other.map(([key, value]) =>
                [Object.hasOwn(expansionLabels, key) ? expansionLabels[key] : key, value]))));
        }
    }

    function render({payload, error}) {
        if (error) {
            const label = error.status ? "HTTP " + error.status + " · " : "";
            host.replaceChildren(el("p", "interpret-message interpret-attention", label + error.message));
            return;
        }
        const report = payload?.data;
        if (!report || typeof report !== "object" || Array.isArray(report)) {
            host.replaceChildren(el("p", "interpret-empty", "响应中没有可预览的报告数据。"));
            return;
        }
        const processing = report.processing || {};
        const overview = el("section", "interpret-overview");
        const badges = el("div", "interpret-badges");
        badges.append(badge(report.reportType, reportTypes), badge(report.status, reportStatuses),
            badge(processing.status, processingStatuses));
        overview.append(el("h3", "interpret-title", report.title || "体检报告"), badges, pairs([
            ["报告日期", report.reportTime],
            ["接口耗时", present(payload.elapsed_ms) ? payload.elapsed_ms + " ms" : null],
            ["检测结果", rows(report.observations).length || rows(report.expansion).length],
            ["待核实", rows(report.unresolvedItems).length],
            ["处理问题", processing.issueCount],
        ]));
        if (present(report.aiSummary)) overview.append(el("p", "interpret-prose", text(report.aiSummary)));
        const warnings = Array.isArray(processing.warnings) ? processing.warnings.filter(present) : [];
        if (warnings.length || ["partial", "unavailable"].includes(processing.status)) {
            const notices = el("ul", "interpret-notices");
            for (const warning of warnings.length ? warnings : [payload.message && payload.message !== "success"
                ? payload.message : processingStatuses[processing.status]]) {
                notices.append(el("li", "", text(warning)));
            }
            overview.append(notices);
        }
        host.replaceChildren(overview);
        renderContent(host, report);
        const sections = rows(report.sections);
        if (sections.length) {
            const list = el("div", "interpret-sections");
            for (const section of sections) {
                const card = el("article", "interpret-section");
                card.append(el("h4", "", section.title || (Object.hasOwn(reportTypes, section.reportType)
                    ? reportTypes[section.reportType] : "子报告")), pairs([["报告日期", section.reportTime]]));
                if (present(section.aiSummary)) card.append(el("p", "interpret-prose", text(section.aiSummary)));
                renderContent(card, section);
                list.append(card);
            }
            host.append(block("子报告", list));
        }
        if (host.children.length === 1) host.append(el("p", "interpret-empty", "暂无检测结果或子报告。"));
    }

    ApiLab.initialize({
        group: "interpret",
        onResult: render,
        onReset() {
            host.replaceChildren(el("p", "interpret-empty", "公共设置已改变，请重新发送请求。"));
        },
    });
})();
