(() => {

    const subjectPicker = document.querySelector("#subject-picker");
    const stage = document.querySelector("#profile-stage");
    const placeholder = document.querySelector("#profile-placeholder");
    const dashboard = document.querySelector("#profile-dashboard");
    let subjects = [];
    let loadedSubject = null;
    let subjectListFailed = false;

    const labels = {
        gender: {male: "男", female: "女", other: "其他", unknown: "未知"},
        kind: {
            self: "本人",
            blood_relative: "血亲",
            spouse: "配偶",
            friend: "朋友",
            other: "其他",
            unknown: "待确认主体",
        },
        interpretation: {
            normal: "正常",
            high: "偏高",
            low: "偏低",
            critical: "危急",
            abnormal: "异常",
            unknown: "待判断",
        },
        judgment: {
            positive: "阳性",
            negative: "阴性",
            detected: "检出",
            not_detected: "未检出",
            unknown: "判定依据不足",
        },
        judgmentBasis: {
            report_flag: "报告标记",
            source_flag: "来源传入",
            source_result: "原始结果",
            reference_range: "按参考范围",
        },
        judgmentReason: {
            missing_value: "缺少结果",
            non_numeric_value: "缺少可比较的数值",
            missing_reference_range: "缺少参考范围",
            unsupported_reference_range: "参考范围格式不支持",
            unit_mismatch: "结果与参考范围单位不一致",
            inconclusive_comparator: "带比较符的结果无法确定",
        },
        trend: {up: "↑", down: "↓", stable: "—", unknown: "·"},
        evidence: {
            ready: "已检索",
            not_requested: "未请求",
            empty: "暂无结果",
            unavailable: "暂不可用",
        },
        factStatus: {
            active: "已生效",
            pending: "待确认",
            disputed: "有争议",
        },
    };


    function displayValue(value) {
        if (value === null || value === undefined || value === "") return "—";
        if (typeof value === "object") {
            const number = value.normalizedNumber ?? value.number;
            const unit = value.normalizedUnit ?? value.unit;
            if (number !== undefined) return `${number}${unit ? ` ${unit}` : ""}`;
            return JSON.stringify(value);
        }
        return String(value);
    }

    function withUnit(text, unit) {
        const suffix = typeof unit === "string" ? unit.trim() : "";
        if (!suffix || text.normalize("NFKC").endsWith(suffix.normalize("NFKC"))) return text;
        return `${text} ${suffix}`;
    }

    function factResult(fact) {
        const value = fact.value;
        if (fact.valueType !== "quantity" || !value || typeof value !== "object") return displayValue(value);

        const raw = value.raw?.toString().trim();
        const original = raw || value.number;
        const result = original ?? value.normalizedNumber;
        if (result === undefined || result === null) return displayValue(value);
        const comparator = value.comparator && !/^[<>≤≥]/.test(String(result)) ? value.comparator : "";
        const unit = original == null ? value.normalizedUnit : value.unit;
        return withUnit(`${comparator}${result}`, unit);
    }

    function factReference(range) {
        const text = typeof range?.text === "string" ? range.text.trim() : "";
        if (text) return text;
        const low = range?.low;
        const high = range?.high;
        if (low == null && high == null) return "—";
        if (low == null) return withUnit(`上限 ${high}`, range.unit);
        if (high == null) return withUnit(`下限 ${low}`, range.unit);
        return withUnit(`${low}–${high}`, range.unit);
    }

    function formatDate(value, withTime = false) {
        if (!value) return "—";
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        const options = withTime
            ? {year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"}
            : {year: "numeric", month: "2-digit", day: "2-digit"};
        return new Intl.DateTimeFormat("zh-CN", options).format(date);
    }

    function element(tag, className, textValue) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (textValue !== undefined) node.textContent = textValue;
        return node;
    }

    function emptyState(message) {
        return element("div", "hpv-empty-state", message);
    }

    function renderMeta(overview) {
        const target = document.querySelector("#profile-meta");
        target.innerHTML = "";
        const values = [
            ["年龄", overview.age === null || overview.age === undefined ? "—" : `${overview.age} 岁`],
            ["性别", labels.gender[overview.gender] || displayValue(overview.gender)],
            ["身高", overview.heightCm === null || overview.heightCm === undefined ? "—" : `${overview.heightCm} cm`],
            ["体重", overview.weightKg === null || overview.weightKg === undefined ? "—" : `${overview.weightKg} kg`],
            ["BMI", displayValue(overview.bmi)],
        ];
        values.forEach(([name, value]) => {
            const item = element("span", "hpv-meta-item");
            item.append(element("small", "", name), document.createTextNode(value));
            target.append(item);
        });
    }

    function interpretationClass(value) {
        return ["normal", "high", "low", "critical", "abnormal"].includes(value)
            ? value
            : "unknown";
    }

    const indicatorGroupOrder = [
        ["anthropometric", "身体测量"],
        ["laboratory", "检验指标"],
        ["microbiome", "菌群检测"],
        ["assessment", "报告评估"],
        ["report_observation", "未标准化检测结果"],
    ];

    function renderIndicators(groups) {
        const target = document.querySelector("#indicator-groups");
        target.replaceChildren();
        const indicatorsByCode = new Map(groups.map(({code, indicators}) => [code, indicators]));
        let indicatorCount = 0;
        indicatorGroupOrder.forEach(([code, name]) => {
            const indicators = indicatorsByCode.get(code) || [];
            indicatorCount += indicators.length;
            const block = element("details", "hpv-indicator-group");
            const heading = element("summary", "hpv-indicator-group-head");
            heading.append(
                element("strong", "", name),
                element("span", "", `${indicators.length} 项`),
            );
            block.append(heading);
            target.append(block);
            if (!indicators.length) {
                block.append(emptyState("暂无指标。"));
                return;
            }
            const table = element("div", "hpv-indicator-table");
            table.tabIndex = 0;
            table.setAttribute("role", "region");
            table.setAttribute("aria-label", name);
            const header = element("div", "hpv-indicator-row hpv-table-head");
            ["指标", "结果", "参考范围", "结果判定", "趋势"].forEach((name) => header.append(element("span", "", name)));
            table.append(header);
            indicators.forEach((indicator) => table.append(renderIndicatorItem(indicator)));
            block.append(table);
        });
        document.querySelector("#indicator-count").textContent = `${indicatorCount} 项`;
    }

    function indicatorResult(observation) {
        const useRawValue = observation.rawValue != null && String(observation.rawValue).trim()
            && typeof observation.rawUnit === "string" && observation.rawUnit.trim();
        const value = displayValue(useRawValue ? observation.rawValue : observation.value);
        if (value === "—") return value;
        const comparator = /^[<>≤≥]/.test(value) ? "" : observation.comparator || "";
        return withUnit(`${comparator}${value}`, useRawValue ? observation.rawUnit : observation.unit);
    }

    function observationDate(observation) {
        if (observation.observedAt) return formatDate(observation.observedAt);
        if (observation.recordedAt) return `录入于 ${formatDate(observation.recordedAt, true)}`;
        return "时间未知";
    }

    function observationMetadata(observation, valueType = observation.valueType) {
        return [
            ["事实 ID", observation.factId],
            ["值类型", valueType],
            ["原始结果", observation.rawValue != null ? displayValue(observation.rawValue) : null],
            ["标本", observation.specimen],
            ["检测方法", observation.method],
            ["观察时间", observation.observedAt && formatDate(observation.observedAt, true)],
            ["录入时间", observation.recordedAt && formatDate(observation.recordedAt, true)],
            ["事实状态", labels.factStatus[observation.status] || observation.status],
            ["核验状态", observation.verificationStatus],
            ...sourceMetadata(observation.sources, observation.observedAt, observation.recordedAt),
        ];
    }

    function renderIndicatorHistory(observations) {
        const history = element("div", "hpv-indicator-history");
        history.append(element("strong", "", `历史观察（${observations.length} 条）`));
        const list = element("ul", "hpv-indicator-history-list");
        for (const observation of observations) {
            const entry = element("li");
            const item = element("details", "hpv-indicator-history-item");
            const summary = element("summary");
            summary.append(
                element("span", "", observationDate(observation)),
                element("strong", "", indicatorResult(observation)),
            );
            item.append(summary, renderMetadata(observationMetadata(observation)));
            entry.append(item);
            list.append(entry);
        }
        history.append(list);
        return history;
    }

    function renderResultJudgment(current) {
        const judgment = current.resultJudgment;
        const status = judgment?.status || current.interpretation || "unknown";
        const text = labels.judgment[status] || labels.interpretation[status] || status;
        const cell = element("span", "hpv-result-judgment");
        cell.append(element("span", `hpv-interpretation ${interpretationClass(status)}`, text));
        const explanation = labels.judgmentBasis[judgment?.basis] || labels.judgmentReason[judgment?.reason];
        if (explanation) cell.append(element("small", "hpv-judgment-basis", explanation));
        const referenceStatus = labels.interpretation[judgment?.referenceStatus];
        if (referenceStatus) cell.append(element("small", "hpv-judgment-reference", `按范围为${referenceStatus}`));
        return cell;
    }

    function renderIndicatorItem(indicator) {
        const current = indicator.current || {};
        const item = element("details", "hpv-indicator-item");
        const row = element("summary", "hpv-indicator-row");
        const nameCell = element("span", "hpv-indicator-name");
        const name = typeof indicator.name === "string" ? indicator.name.trim() : "";
        nameCell.append(element("strong", "", name || indicator.code || "未命名指标"));
        if (current.status === "pending" || current.status === "disputed") {
            nameCell.append(element("span", `hpv-fact-status ${current.status}`, labels.factStatus[current.status]));
        }
        if (current.observedAt || current.recordedAt) {
            nameCell.append(element("small", "hpv-observed-at", observationDate(current)));
        }
        const trendName = indicator.trend || "unknown";
        const trend = element(
            "span",
            `hpv-trend ${["up", "down", "stable"].includes(trendName) ? trendName : "unknown"}`,
            labels.trend[trendName] || "·",
        );
        trend.title = trendName === "unknown"
            ? "单次、定性、报告评估或缺少可比条件的数据不生成趋势，但仍展示原始结果。"
            : "比较最近两次可比观察值；上升或下降不等于健康好坏。";
        const trendCell = element("span", "hpv-indicator-trends");
        trendCell.append(trend);
        const recorded = indicator.recordedTrend;
        if (recorded?.basis === "recordedAt" && ["up", "down", "stable"].includes(recorded.direction)) {
            trendCell.append(element(
                "small",
                "hpv-recorded-trend",
                `${labels.trend[recorded.direction]} 按录入时间粗略比较`,
            ));
        }
        row.append(
            nameCell,
            element("span", "hpv-result", indicatorResult(current)),
            element("span", "hpv-reference", current.referenceRange || "—"),
            renderResultJudgment(current),
            trendCell,
        );

        const kind = {measurement: "测量或检测", assessment: "报告自带评估", unmapped: "未标准化原始结果"}[indicator.kind];
        const metadata = renderMetadata([
            ["指标编码", indicator.code],
            ["事实类型", indicator.factType],
            ["指标类型", kind],
            ...observationMetadata(current, current.valueType || indicator.valueType),
        ]);
        metadata.classList.add("hpv-indicator-meta");
        item.append(row, metadata);
        const history = (indicator.observations || []).filter((observation) =>
            !current.factId || observation.factId !== current.factId);
        if (history.length) item.append(renderIndicatorHistory(history.reverse()));
        return item;
    }

    function renderFactGroups(groups) {
        const target = document.querySelector("#fact-groups");
        target.replaceChildren();
        const factCount = groups.reduce((count, group) => count + (group.items || []).length, 0);
        document.querySelector("#fact-count").textContent = `${factCount} 条`;
        if (!factCount) {
            target.append(emptyState("当前没有可展示的健康事实。"));
            return;
        }
        groups.forEach((group) => {
            const facts = group.items || [];
            if (!facts.length) return;
            const name = group.name || group.code || "事实分组";
            const block = element("details", "hpv-fact-group");
            const heading = element("summary", "hpv-fact-group-head");
            heading.append(
                element("strong", "", name),
                element("span", "", `${facts.length} 条`),
            );
            const list = element("div", "hpv-fact-list");
            if (facts.length > 10) {
                list.classList.add("is-scrollable");
                list.tabIndex = 0;
                list.setAttribute("role", "region");
                list.setAttribute("aria-label", name);
            }
            const columns = element("div", "hpv-fact-columns");
            columns.append(
                element("span", "hpv-fact-name", "项目"),
                element("span", "hpv-fact-value", "结果"),
                element("span", "hpv-fact-reference", "参考范围"),
                element("span", "hpv-fact-column-status", "状态"),
            );
            list.append(columns);
            facts.forEach((fact) => list.append(renderFactItem(fact)));
            block.append(heading, list);
            target.append(block);
        });
    }

    function renderFactItem(fact) {
        const item = element("details", "hpv-fact-item");
        const row = element("summary", "hpv-fact-row");
        const value = element("span", "hpv-fact-value");
        value.append(element("span", "hpv-fact-result-text", factResult(fact)));
        const interpretation = interpretationClass(fact.interpretation);
        if (interpretation !== "unknown") {
            value.append(element("span", `hpv-interpretation ${interpretation}`, labels.interpretation[interpretation]));
        }

        const reference = element("span", "hpv-fact-reference");
        reference.append(
            element("small", "hpv-fact-reference-label", "参考范围"),
            element("span", "", factReference(fact.referenceRange)),
        );
        const status = fact.status || "pending";
        row.append(
            element("strong", "hpv-fact-name", fact.displayName || fact.factType || "未命名事实"),
            value,
            reference,
            element(
                "span",
                `hpv-fact-status ${["active", "pending", "disputed"].includes(status) ? status : "pending"}`,
                labels.factStatus[status] || status,
            ),
        );

        const fields = [
            ["标本", fact.value?.specimen],
            ["检测方法", fact.value?.method],
            ["事实编码", fact.factType],
            ["事实 ID", fact.factId],
            ["观察时间", formatDate(fact.effectivePeriod?.start, true)],
            ["有效期结束", fact.effectivePeriod?.end && formatDate(fact.effectivePeriod.end, true)],
            ["核验状态", fact.verificationStatus],
            ["断言级别", fact.assertionLevel],
            ["提取置信度", fact.confidence],
            ["版本", fact.version],
            ["更新时间", fact.updatedAt && formatDate(fact.updatedAt, true)],
            ...sourceMetadata(fact.sources, fact.effectivePeriod?.start),
        ];

        item.append(row, renderMetadata(fields));
        return item;
    }

    function sourceMetadata(sources, observedAt, recordedAt) {
        return (sources || []).flatMap((source) => {
            const origin = [
                source.sourceType,
                source.sourceId,
                source.sourceRevision != null ? `v${source.sourceRevision}` : null,
                source.pointer,
            ].filter(Boolean).join(" · ");
            return [
                ["来源", origin],
                ["录入时间", source.recordedAt && source.recordedAt !== recordedAt
                    ? formatDate(source.recordedAt, true) : null],
                ["来源观察时间", source.observedAt && source.observedAt !== observedAt
                    ? formatDate(source.observedAt, true) : null],
                ["确认依据", source.confirmationBasis],
            ];
        });
    }

    function renderMetadata(fields) {
        const metadata = element("dl", "hpv-fact-meta");
        fields.forEach(([label, content]) => {
            if (content === undefined || content === null || content === "") return;
            const field = element("div", "hpv-fact-meta-field");
            field.append(element("dt", "", label), element("dd", "", String(content)));
            metadata.append(field);
        });
        return metadata;
    }

    function safeHttpUrl(value) {
        try {
            const url = new URL(value);
            return ["http:", "https:"].includes(url.protocol) ? url.href : null;
        } catch {
            return null;
        }
    }

    function renderEvidence(evidence) {
        const target = document.querySelector("#evidence-list");
        const status = evidence?.status || "not_requested";
        const badge = document.querySelector("#evidence-status");
        badge.className = `hpv-evidence-status ${status}`;
        badge.textContent = labels.evidence[status] || status;
        target.innerHTML = "";
        const items = evidence?.items || [];
        if (!items.length) {
            const messages = {
                not_requested: "本次未启用循证检索。勾选上方的循证支持选项后重新加载即可查看。",
                empty: "知识库检索成功，但暂未找到足够相关的循证条目。",
                unavailable: "知识库当前不可用，画像中的指标和建议仍然有效。",
                ready: "循证检索已完成，当前没有可展示的条目。",
            };
            target.append(emptyState(messages[status] || "暂无循证支持数据。"));
            return;
        }
        items.forEach((item, index) => {
            const card = element("article", "hpv-evidence-card");
            const titleLine = element("div", "hpv-evidence-title");
            titleLine.append(
                element("span", "hpv-evidence-index", String(index + 1)),
                element("h4", "", item.title || "未命名循证条目"),
            );
            const grade = element("div", "hpv-grade-line");
            grade.append(
                element("span", "", `证据：${item.evidenceGrade || "未分级"}`),
                element("span", "", `相关性：${item.relevanceGrade || "未分级"}`),
            );
            card.append(titleLine, grade);
            if (item.conclusion) card.append(element("p", "hpv-conclusion", item.conclusion));
            const citations = item.citations || [];
            if (citations.length) {
                const list = element("ul", "hpv-citation-list");
                citations.forEach((citation) => {
                    const line = element("li", "");
                    const link = safeHttpUrl(citation.url);
                    if (link) {
                        const anchor = element("a", "", citation.title || citation.publisher || "查看来源");
                        anchor.href = link;
                        anchor.target = "_blank";
                        anchor.rel = "noopener noreferrer";
                        line.append(anchor);
                    } else {
                        line.textContent = citation.title || citation.publisher || "未命名来源";
                    }
                    const details = [citation.publisher, citation.publishedAt].filter(Boolean).join(" · ");
                    if (details) line.append(element("small", "", details));
                    list.append(line);
                });
                card.append(list);
            }
            target.append(card);
        });
    }

    function renderAlerts(alerts) {
        const target = document.querySelector("#alert-list");
        target.innerHTML = "";
        document.querySelector("#alert-count").textContent = `${alerts.length} 项`;
        if (!alerts.length) {
            const message = emptyState("当前未筛选出关键异常提示。 ");
            const link = element("a", "", "查看指标结果");
            link.href = "#indicator-groups";
            message.append(link);
            target.append(message);
            return;
        }
        alerts.forEach((alert) => {
            const severity = alert.severity === "critical" ? "critical" : "important";
            const row = element("div", `hpv-alert-row ${severity}`);
            row.append(
                element("span", "hpv-alert-dot"),
                element("strong", "", alert.title || "未命名提示"),
                element("span", "hpv-alert-level", severity === "critical" ? "紧急关注" : "重点关注"),
            );
            target.append(row);
        });
    }

    function summaryRow(label, content) {
        const row = element("div", "hpv-summary-row");
        row.append(element("strong", "", label), element("p", "", content || "—"));
        return row;
    }

    function renderSummary(summary) {
        const target = document.querySelector("#summary-content");
        target.innerHTML = "";
        if (!summary) {
            target.append(emptyState("暂无综述与建议。"));
            return;
        }
        target.append(summaryRow("综合评估", summary.overall));
        const priorities = (summary.priorities || []).length
            ? summary.priorities.join("\n")
            : "暂无优先建议。";
        target.append(
            summaryRow("建议优先", priorities),
            summaryRow("随访建议", summary.followUp || "暂无随访建议。"),
        );
    }

    function renderProfile(data) {
        if (!data || !data.subject) throw new Error("画像响应缺少 subject 数据");
        const subject = data.subject;
        const overview = data.overview || {};
        const displayName = subject.displayName || "未命名主体";
        document.querySelector("#subject-avatar").textContent = displayName.slice(0, 1) || "健";
        document.querySelector("#profile-title").textContent = `${displayName}的健康画像`;
        const kindLabel = labels.kind[subject.kind] || subject.kind || "未知主体";
        const relation = subject.relationship?.relationType;
        document.querySelector("#subject-kind").textContent = relation ? `${kindLabel} · ${relation}` : kindLabel;
        document.querySelector("#profile-user-id").textContent = data.userId || "—";
        document.querySelector("#profile-subject-id").textContent = subject.subjectId || "—";
        document.querySelector("#profile-version").textContent = `v${data.profileVersion ?? "—"}`;
        document.querySelector("#profile-updated-at").textContent = formatDate(data.updatedAt, true);
        renderMeta(overview);
        renderIndicators(data.indicatorGroups || []);
        renderFactGroups(data.factGroups || []);
        renderEvidence(data.evidence || {status: "not_requested", items: []});
        renderAlerts(data.alerts || []);
        renderSummary(data.summary);

        const pending = Number(overview.pendingConfirmationCount || 0);
        const pendingCard = document.querySelector("#pending-card");
        pendingCard.hidden = pending <= 0;
        document.querySelector("#pending-count").textContent = `${pending} 条信息待处理`;

        loadedSubject = subject;
        syncSubjectPicker();
        placeholder.hidden = true;
        dashboard.hidden = false;
        stage.classList.remove("is-empty", "has-error");
    }

    function showFailure(error) {
        dashboard.hidden = true;
        placeholder.hidden = false;
        placeholder.querySelector("strong").textContent = "画像加载失败";
        placeholder.querySelector("span:last-child").textContent = error.message;
        stage.classList.add("is-empty", "has-error");
        const status = document.querySelector("#health-status");
        status.className = "status bad";
        status.textContent = error.message;
    }

    function subjectOption(subject) {
        const kind = labels.kind[subject.kind] || subject.kind || "未知主体";
        const name = subject.displayName || "未命名主体";
        return new Option(`${name} · ${kind}`, subject.subjectId);
    }

    function syncSubjectPicker() {
        const model = ApiLab.draft("health-profile.get");
        if (model.mode === "json") {
            subjectPicker.replaceChildren(new Option("由手工 JSON 指定", ""));
            subjectPicker.disabled = true;
            return;
        }

        const subjectId = model.values.subjectId.trim();
        const defaultLabel = subjectListFailed ? "默认读取本人（主体列表加载失败）" : "默认读取本人";
        const options = [new Option(defaultLabel, ""), ...subjects.map(subjectOption)];
        if (subjectId && !subjects.some((subject) => subject.subjectId === subjectId)) {
            options.push(loadedSubject?.subjectId === subjectId
                ? subjectOption(loadedSubject)
                : new Option(`手工指定 · ${subjectId}`, subjectId));
        }
        subjectPicker.replaceChildren(...options);
        subjectPicker.value = subjectId;
        subjectPicker.disabled = options.length === 1;
    }

    function clearProfile() {
        subjects = [];
        loadedSubject = null;
        subjectListFailed = false;
        syncSubjectPicker();
        dashboard.hidden = true; placeholder.hidden = false;
        placeholder.querySelector("strong").textContent = "尚未加载用户画像";
        placeholder.querySelector("span:last-child").textContent = "加载当前公共用户的画像后，展示健康总览。";
        stage.className = "hpv-stage is-empty";
    }
    ApiLab.initialize({endpoints: ["health-profile.get", "health-profile.subjects.list"], urlIdentity: true,
        onReset: clearProfile,
        async onResponse(payload, {id}) {
            if (id === "health-profile.get") {
                if (!payload.data?.subject) return;
                renderProfile(payload.data);
                await ApiLab.run("health-profile.subjects.list", {showResult: false});
            } else if (id === "health-profile.subjects.list") {
                subjects = payload.data?.subjects || [];
                subjectListFailed = false;
                syncSubjectPicker();
            }
        },
        onError(error, {id}) {
            if (id === "health-profile.get") {
                showFailure(error);
            } else {
                subjects = [];
                subjectListFailed = true;
                syncSubjectPicker();
            }
        },
    });

    const query = new URLSearchParams(location.search);
    if (query.has("subjectId")) ApiLab.setParams("health-profile.get", {subjectId: query.get("subjectId")}, {touch: false});
    ApiLab.subscribeDrafts(syncSubjectPicker);

    function loadProfile() {
        ApiLab.select("health-profile.get");
        return ApiLab.run("health-profile.get");
    }

    document.querySelector("#load-profile").addEventListener("click", loadProfile);
    document.querySelector("#clear-profile").addEventListener("click", clearProfile);
    subjectPicker.addEventListener("change", () => {
        ApiLab.setParams("health-profile.get", {subjectId: subjectPicker.value});
        loadProfile();
    });
})();
