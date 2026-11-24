/* Whole-questionnaire client. All business work goes through the production API. */
(() => {
    const $ = (id) => document.querySelector(`#questionnaire-${id}`);
    const submitId = "questionnaire.submit";
    const pageNames = {health_background: "健康背景", report_follow_up: "报告补充", daily_habits: "日常习惯"};
    const statusNames = {queued: "已排队", generating: "正在生成", ready: "问卷已就绪", submitted: "答案已提交，可继续 Chat", expired: "问卷已过期", failed: "问卷生成失败"};
    let current = null, pending = false, pollTimer = null, controls = [], writing = false;
    const factors = ApiLabQuestionnaireFactors.mount({host: $("factors"), download});

    function stopPolling() {
        if (pollTimer !== null) clearTimeout(pollTimer);
        pollTimer = null;
    }
    function shouldPoll() {
        return ["queued", "generating"].includes(current?.status)
            || (current?.status === "failed" && current.retryable === true)
            || (current?.status === "ready" && current.submissionPending === true);
    }
    function pollDelay() {
        return Number.isFinite(current?.retryAfterSeconds) && current.retryAfterSeconds > 0
            ? current.retryAfterSeconds * 1000 : current?.status === "failed" ? 15000 : 2000;
    }
    function schedulePoll() {
        stopPolling();
        if (!shouldPoll()) return;
        const revision = ApiLab.context.revision;
        const snapshot = ApiLab.context.get();
        pollTimer = setTimeout(async () => {
            pollTimer = null;
            if (revision !== ApiLab.context.revision) return;
            if (pending) { schedulePoll(); return; }
            // Automatic recovery always belongs to this view. A manually edited
            // status request remains independently callable from the API editor.
            const endpoint = ApiLabContracts.find((item) => item.id === "questionnaire.status");
            const request = ApiLabCore.buildRequest(endpoint, {userId: snapshot.userId, sessionId: snapshot.sessionId});
            pending = true; syncControls();
            try {
                const payload = await ApiLab.request(request.path, {...request, snapshot, revision});
                pending = false; render(payload.data); syncControls();
            } catch (error) {
                if (!error.stale) handleError(error, {id: "questionnaire.status"});
            }
        }, pollDelay());
    }
    function answerDraft() {
        const value = ApiLab.draft(submitId).parameters(ApiLab.context.get(), false).answers;
        if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("answers 必须为 JSON 对象；全部留空可使用 {}。");
        return value;
    }
    function validAnswers(schema, answers) {
        for (const page of schema?.pages || []) for (const question of page.elements || []) {
            const value = answers[question.name];
            const empty = value === undefined || value === "" || (Array.isArray(value) && !value.length);
            if (empty) {
                if (question.isRequired) throw new Error(`请回答：${question.title}`);
                continue;
            }
            if (question.type === "text") {
                if (typeof value !== "string" || (question.isRequired && !value.trim())) throw new Error(`请填写文本：${question.title}`);
            } else {
                const choices = new Set((question.choices || []).map((choice) => choice.value));
                if (question.type === "radiogroup") {
                    if (typeof value !== "string" || !choices.has(value)) throw new Error(`请选择有效选项：${question.title}`);
                } else if (question.type === "checkbox") {
                    if (!Array.isArray(value) || value.some((item) => typeof item !== "string"
                        || (!choices.has(item) && !(item === "none" && question.showNoneItem)))
                        || (value.includes("none") && value.length !== 1)) throw new Error(`请检查多选答案：${question.title}`);
                }
            }
        }
    }
    function syncControls() {
        let answers = {}, problem = "";
        try { answers = current?.status === "submitted" ? current.answers || {} : answerDraft(); } catch (error) { problem = error.message; }
        const manual = ApiLab.draft(submitId).mode === "json";
        const locked = pending || current?.status !== "ready" || current?.submissionPending === true || manual || !!problem;
        for (const {question, inputs} of controls) {
            const value = answers[question.name];
            for (const input of inputs) {
                input.disabled = locked;
                if (question.type === "text") {
                    if (document.activeElement !== input) input.value = typeof value === "string" ? value : "";
                } else input.checked = Array.isArray(value) ? value.includes(input.value) : value === input.value;
            }
        }
        $("submit").disabled = pending || current?.status !== "ready" || current?.submissionPending === true || !!problem;
        $("generate").disabled = $("restore").disabled = pending;
        $("chat").hidden = current?.status !== "submitted";
        $("mode").textContent = current?.submissionPending
            ? "答案已由服务器受理，正在完成保存；请等待自动恢复，期间不能修改或再次提交。"
            : problem || (manual
            ? "提交使用参数区的手工 JSON；问卷控件暂时只读。切回参数表单可继续填写，手工 JSON 会保留。"
            : current?.status === "submitted" ? "已恢复服务器保存的提交答案。当前会话可继续使用 Chat 的完整响应或 SSE。"
                : "所有题目均可选填；不确定可留空，不会自动选择“不清楚”或“以上均无”。全部留空也可以提交。");
    }
    function changeAnswer(question, inputs, changed) {
        if (ApiLab.draft(submitId).mode !== "form" || current?.status !== "ready" || current?.submissionPending) return;
        const answers = {...answerDraft()};
        let value;
        if (question.type === "text") value = changed.value.trim();
        else if (question.type === "radiogroup") value = changed.checked ? changed.value : undefined;
        else {
            if (changed.checked) {
                for (const input of inputs) if ((changed.value === "none" && input !== changed) || (changed.value !== "none" && input.value === "none")) input.checked = false;
            }
            value = inputs.filter((input) => input.checked).map((input) => input.value);
        }
        if (value === undefined || value === "" || (Array.isArray(value) && !value.length)) delete answers[question.name];
        else answers[question.name] = value;
        writing = true;
        try { ApiLab.setParams(submitId, {answers}); } finally { writing = false; }
        syncControls();
    }
    function renderForm() {
        const host = $("form"); host.replaceChildren(); controls = [];
        if (!current?.questionnaire) {
            host.append(ApiLab.element("p", "empty", "问卷就绪后在这里填写；生成期间会自动查询进度。"));
            return;
        }
        const schema = current.questionnaire;
        host.append(ApiLab.element("h3", "", schema.title || "健康问卷"));
        if (schema.description) host.append(ApiLab.element("p", "survey-bridge", schema.description));
        let number = 0;
        for (const [index, page] of (schema.pages || []).entries()) {
            const section = ApiLab.element("details", "survey-page");
            section.open = index === 0;
            const questions = page.elements || [];
            const heading = ApiLab.element("summary", "lab-disclosure");
            heading.append(
                ApiLab.element("span", "", pageNames[page.name] || page.name),
                ApiLab.element("small", "", `${questions.length} 题`),
            );
            const body = ApiLab.element("div", "survey-page-body");
            for (const question of questions) {
                if (question.description) body.append(ApiLab.element("p", "survey-bridge", question.description));
                const group = ApiLab.element("fieldset", "survey-question");
                group.append(ApiLab.element("legend", "", `${++number}. ${question.title}${question.isRequired ? "（必填）" : ""}`));
                const inputs = [];
                if (question.type === "text") {
                    const input = ApiLab.element("input", "survey-text"); input.type = "text";
                    input.name = question.name; input.setAttribute("aria-label", question.title);
                    input.addEventListener("input", () => changeAnswer(question, inputs, input));
                    inputs.push(input); group.append(input);
                } else {
                    const options = ApiLab.element("div", "survey-options");
                    const choices = [...(question.choices || [])];
                    if (question.type === "checkbox" && question.showNoneItem) choices.push({value: "none", text: question.noneText || "以上均无"});
                    for (const choice of choices) {
                        const label = ApiLab.element("label", "survey-choice");
                        const input = ApiLab.element("input"); input.type = question.type === "checkbox" ? "checkbox" : "radio";
                        input.name = question.name; input.value = choice.value;
                        input.addEventListener("change", () => changeAnswer(question, inputs, input));
                        inputs.push(input); label.append(input, ApiLab.element("span", "", choice.text ?? choice.value)); options.append(label);
                    }
                    group.append(options);
                    if (question.type === "radiogroup") {
                        const clear = ApiLab.element("button", "button ghost compact", "清除此题"); clear.type = "button";
                        clear.addEventListener("click", () => {
                            if (pending || current?.status !== "ready" || current?.submissionPending || ApiLab.draft(submitId).mode !== "form") return;
                            inputs.forEach((input) => { input.checked = false; });
                            changeAnswer(question, inputs, {checked: false});
                        });
                        group.append(clear);
                    }
                }
                controls.push({question, inputs}); body.append(group);
            }
            section.append(heading, body);
            host.append(section);
        }
        syncControls();
    }
    function section(id, title) {
        const node = $(id); node.replaceChildren(); node.hidden = false;
        const head = ApiLab.element("div", "panel-head"); head.append(ApiLab.element("h2", "", title));
        const body = ApiLab.element("div", "panel-body"); node.append(head, body); return body;
    }
    function download(label, filename, value) {
        const button = ApiLab.element("button", "button ghost compact", label); button.type = "button";
        button.addEventListener("click", () => {
            try {
                const data = typeof value === "function" ? value() : value;
                const url = URL.createObjectURL(new Blob([ApiLab.format(data) + "\n"], {type: "application/json;charset=utf-8"}));
                const anchor = ApiLab.element("a"); anchor.href = url; anchor.download = filename;
                document.body.append(anchor); anchor.click(); anchor.remove();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
            } catch (error) { ApiLab.showError(error, false); }
        });
        return button;
    }
    function reportText(value, fallback = "—") {
        return typeof value === "string" && value.trim() ? value.trim()
            : typeof value === "number" && Number.isFinite(value) ? String(value) : fallback;
    }
    function renderReportResults(host, result) {
        const observations = [], imaging = [], seen = new Set();
        function visit(node, inheritedDate = "") {
            if (!node || typeof node !== "object") return;
            const date = reportText(node.reportTime, inheritedDate);
            for (const item of Array.isArray(node.observations) ? node.observations : []) {
                if (!item || typeof item !== "object") continue;
                const value = reportText(item.resultValue), unit = reportText(item.unit, "");
                const row = [reportText(item.observedAt, date || "—"), reportText(item.itemName),
                    value + (unit && value !== "—" && !value.endsWith(unit) ? ` ${unit}` : ""),
                    reportText(item.referenceRange), reportText(item.flag)];
                const key = JSON.stringify(row);
                if (!seen.has(key)) { seen.add(key); observations.push(row); }
            }
            if (node.expansion && !Array.isArray(node.expansion)) {
                for (const [field, label] of [["findings", "影像所见"], ["diagnosis", "影像诊断"]]) {
                    const text = reportText(node.expansion[field], "");
                    const key = JSON.stringify([field, text, date]);
                    if (text && !seen.has(key)) {
                        seen.add(key); imaging.push({label, text, date, title: reportText(node.title, "")});
                    }
                }
            }
            for (const child of Array.isArray(node.sections) ? node.sections : []) visit(child, date);
        }
        visit(result);
        if (observations.length) {
            const scroll = ApiLab.element("div", "survey-report-table-wrap");
            const table = ApiLab.element("table", "survey-report-table");
            table.setAttribute("aria-label", "报告检查结果");
            const head = ApiLab.element("thead"), header = ApiLab.element("tr");
            for (const label of ["检查时间", "检查指标", "结果", "参考范围", "异常标记"]) {
                const cell = ApiLab.element("th", "", label); cell.setAttribute("scope", "col"); header.append(cell);
            }
            head.append(header); table.append(head);
            const body = ApiLab.element("tbody");
            const abnormal = /^(?:异常|偏高|偏低|升高|降低|高|低|↑|↓|h|l|high|low|超标|缺乏|危急|危急值)$/i;
            for (const values of observations) {
                const row = ApiLab.element("tr");
                values.forEach((text, index) => row.append(ApiLab.element("td",
                    index === 4 && abnormal.test(text) ? "survey-report-abnormal" : "", text)));
                body.append(row);
            }
            table.append(body); scroll.append(table); host.append(scroll);
        }
        for (const item of imaging) {
            const block = ApiLab.element("section", "survey-imaging");
            block.append(ApiLab.element("h4", "", [item.title, item.label].filter(Boolean).join(" · ")));
            if (item.date) block.append(ApiLab.element("p", "hint", `检查时间：${item.date}`));
            block.append(ApiLab.element("p", "survey-copy", item.text)); host.append(block);
        }
        if (!observations.length && !imaging.length) host.append(ApiLab.element("p", "hint", "该报告暂未获得可展示的检查结果。"));
    }
    function renderMaterials() {
        for (const id of ["risk", "client", "interview", "reports"]) { $(id).hidden = true; $(id).replaceChildren(); }
        const guidance = current?.guidance || {};
        if (guidance.riskAssessment?.length || guidance.riskNote) {
            const body = section("risk", "简要健康风险评判");
            const risks = guidance.riskAssessment || [];
            for (const risk of risks) {
                body.append(ApiLab.element("strong", "", risk.priority || ""), ApiLab.element("p", "survey-copy", risk.assessment || ""));
                const sources = [risk.basis, risk.evidence].flatMap((value) => Array.isArray(value) ? value : [value]);
                const assessment = reportText(risk.assessment, "").replace(/\s+/g, "");
                const basis = [...new Set(sources.map((value) => reportText(value, "")))]
                    .filter((value) => value && value.replace(/\s+/g, "") !== assessment);
                if (basis.length) body.append(ApiLab.element("p", "hint", `报告依据：${basis.join("、")}`));
            }
            const comparable = (value) => reportText(value, "").replace(/\s+/g, "").replace(/：/g, ":");
            const assessments = risks.map((risk) => reportText(risk.assessment, ""));
            const labelled = risks.map((risk, index) => [reportText(risk.priority, ""), assessments[index]].filter(Boolean).join("："));
            const repeated = new Set([...assessments, ...labelled, assessments.join("；"), labelled.join("；")].map(comparable).filter(Boolean));
            if (guidance.riskNote && !repeated.has(comparable(guidance.riskNote))) body.append(ApiLab.element("p", "hint", guidance.riskNote));
        }
        if (guidance.clientCheckupMessage) {
            const body = section("client", "可发送给客户的补充检查文案");
            body.append(ApiLab.element("p", "survey-copy", guidance.clientCheckupMessage));
            const copy = ApiLab.element("button", "button secondary", "复制客户文案"); copy.type = "button";
            copy.addEventListener("click", async () => { copy.textContent = await ApiLabJson.copyText(document, guidance.clientCheckupMessage) ? "已复制" : "复制失败，请选择文案手动复制"; });
            body.append(copy);
        }
        if (guidance.managerInterview?.length) {
            const body = section("interview", "健管师对话补充清单");
            body.append(ApiLab.element("p", "hint", "结合问卷答案挑选仍需了解的内容，通过后续对话补充。"));
            const list = ApiLab.element("ol", "survey-guidance");
            for (const entry of guidance.managerInterview) {
                const item = ApiLab.element("li"); item.append(ApiLab.element("p", "", entry.question), ApiLab.element("p", "hint", entry.reason)); list.append(item);
            }
            body.append(list);
        }
        if (current?.reports?.length) {
            const body = section("reports", "本次报告资料");
            current.reports.forEach((report, index) => {
                const row = ApiLab.element("section", "survey-page");
                const result = report.interpretation || report.result || report;
                row.append(ApiLab.element("h3", "", report.filename || report.fileName || result.title || `报告 ${index + 1}`));
                if (result.reportTime) row.append(ApiLab.element("p", "hint", `报告时间：${reportText(result.reportTime)}`));
                if (result.aiSummary) row.append(ApiLab.element("p", "survey-copy", result.aiSummary));
                renderReportResults(row, result);
                if (report.error) row.append(ApiLab.element("p", "hint error-text", typeof report.error === "string" ? report.error : ApiLab.format(report.error)));
                row.append(download("下载报告 JSON", `报告-${index + 1}.json`, result));
                const details = ApiLab.element("details", "survey-report-raw");
                details.append(ApiLab.element("summary", "", "查看原始报告 JSON"));
                const json = ApiLab.element("div"); ApiLab.renderJson(json, report, {label: "报告资料 JSON"});
                details.append(json); row.append(details); body.append(row);
            });
        }
        const downloads = $("downloads"); downloads.replaceChildren();
        if (current?.questionnaire) {
            downloads.append(download("下载问卷 JSON", "问卷.json", current.questionnaire), download("下载答案 JSON", "答案.json", () => ({answers: current.status === "submitted" ? current.answers || {} : answerDraft()})));
        }
        if (current?.guidance) downloads.append(download("下载指导 JSON", "指导.json", current.guidance));
        if (current?.reports) downloads.append(download("下载全部报告 JSON", "报告资料.json", current.reports));
    }
    function render(data) {
        if (!data || !data.questionnaireId) return;
        if (data.sessionId && data.sessionId !== ApiLab.context.get().sessionId) {
            $("status").textContent = "响应属于其他会话；请在接口结果中查看，当前问卷未改变。";
            return;
        }
        const changed = data.questionnaireId !== current?.questionnaireId;
        // Status APIs return a complete public snapshot; omitted expired material must disappear.
        current = {...data};
        const update = {questionnaireId: current.questionnaireId};
        if (changed || current.status === "submitted") update.answers = current.answers || {};
        ApiLab.setParams(submitId, update, {touch: false});
        $("status").textContent = `${statusNames[current.status] || current.status} · ${current.questionnaireId}`
            + (current.expiresAt ? `\n有效期：${current.expiresAt}` : "")
            + (shouldPoll() ? `\n每 ${pollDelay() / 1000} 秒自动查询${current.submissionPending ? "提交保存进度" : current.status === "failed" ? "后台重试进度" : "生成进度"}；刷新后可恢复当前问卷。` : "")
            + (current.message ? `\n${current.message}` : "");
        renderForm(); renderMaterials(); syncControls(); schedulePoll(); factors.follow(current);
    }
    function reset() {
        stopPolling(); factors.reset(); current = null; pending = false; controls = [];
        $("form").replaceChildren(ApiLab.element("p", "empty", "已切换公共设置，请生成或恢复当前会话的问卷。"));
        $("status").textContent = "已切换公共设置，之前的问卷与答案不会沿用。";
        renderMaterials(); syncControls();
    }
    function handleError(error, {id}) {
        if (id === ApiLabQuestionnaireFactors.endpointId) { factors.failManual(error); return; }
        pending = false; stopPolling();
        const data = error.payload?.data;
        if (data?.questionnaireId) render(data);
        else if (error.status === 410 && current) { current.status = "expired"; syncControls(); }
        $("status").textContent = id === submitId
            ? `提交未确认成功：${error.message}。答案已保留，请先恢复当前问卷确认状态；相同答案可以重试。`
            : `${error.message}。可以手动恢复当前问卷；生成请求中断时请保留当前会话。`;
        syncControls();
        if (error.name === "QuestionnaireSubmissionPending") schedulePoll();
    }
    ApiLabFiles.mount({endpoint: "questionnaire.create", prefix: "questionnaire-file", action: "生成整卷问卷"});
    ApiLab.initialize({group: "questionnaire", initialEndpoint: "questionnaire.create", formPanels: {"questionnaire.create": "#questionnaire-attachments"}, onReset: reset,
        onStart(meta) {
            const {id, parameters} = meta;
            if (id === ApiLabQuestionnaireFactors.endpointId) { factors.startManual(meta); return; }
            stopPolling();
            if (id === submitId && current?.submissionPending && parameters.questionnaireId === current.questionnaireId) {
                throw Object.assign(new Error("答案已受理，正在恢复保存；请先查询当前状态。"), {name: "QuestionnaireSubmissionPending"});
            }
            if (id === submitId && ApiLab.draft(submitId).mode === "form") validAnswers(current?.questionnaire, parameters.answers);
            pending = true; syncControls();
        },
        onResponse(payload, meta) {
            if (meta.id === ApiLabQuestionnaireFactors.endpointId) { factors.receiveManual(payload, meta); return; }
            pending = false; render(payload.data); syncControls();
        },
        onError: handleError,
    });
    ApiLab.subscribeDrafts(() => { if (!writing) syncControls(); });
    document.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => {
        ApiLab.select(button.dataset.action); return ApiLab.run(button.dataset.action);
    }));
    window.addEventListener("pagehide", () => { stopPolling(); factors.stop(); });
    window.addEventListener("pageshow", () => factors.resume());
    syncControls();
})();
