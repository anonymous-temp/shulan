/* Read-only factor assessment view. Submission and Chat never wait for this work. */
const ApiLabQuestionnaireFactors = (() => {
    const endpointId = "questionnaire.factor-assessment";
    const statuses = {
        queued: "因素分析已排队", running: "因素分析正在进行", completed: "因素分析已完成",
        failed: "因素分析技术失败", unavailable: "暂无可用的因素分析",
    };
    const relations = {cause: "病因关联", risk_factor: "风险因素", trigger: "诱发因素", aggravating_factor: "加重因素",
        relieving_factor: "缓解因素", protective_factor: "保护因素", association: "相关因素", possible_contributor: "可能相关因素"};
    const answerStatuses = {answered: "已回答", unknown: "信息不足", unanswered: "未回答", unclear: "信息不明确"};
    const conditionStatuses = {met: "符合条件", not_met: "不符合条件", unknown: "信息不足", conflicting: "存在冲突"};
    const record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
    const list = (value) => Array.isArray(value) ? value : [];
    const text = (value, fallback = "") => typeof value === "string" ? value : typeof value === "number" ? String(value) : fallback;
    const label = (value, fallback) => text(value?.concept?.label || value?.label || value?.id, fallback);

    function mount({host, download}) {
        let scope = null, assessment = null, loading = false, timer = null, deadline = null, controller = null, epoch = 0, manualEpoch = null;
        function clearDeadline() { if (deadline !== null) clearTimeout(deadline); deadline = null; }
        function stop() {
            if (timer !== null) clearTimeout(timer);
            clearDeadline(); controller?.abort(); controller = null;
            timer = null; epoch++; manualEpoch = null; loading = false;
        }
        function reset() {
            stop(); scope = null; assessment = null; host.replaceChildren(); host.hidden = true;
        }
        function live(token = epoch) { return scope && token === epoch && scope.revision === ApiLab.context.revision; }
        function fold(parent, title, open = false) {
            const node = ApiLab.element("details", "factor-fold"); node.open = open;
            node.append(ApiLab.element("summary", "", title));
            const body = ApiLab.element("div", "factor-fold-body"); node.append(body); parent.append(node); return body;
        }
        function value(parent, item, empty = "暂无相关记录。") {
            if (item === undefined || item === null || (Array.isArray(item) && !item.length)) {
                parent.append(ApiLab.element("p", "hint", empty));
            } else if (typeof item === "string" || typeof item === "number" || typeof item === "boolean") {
                parent.append(ApiLab.element("p", "survey-copy", String(item)));
            } else {
                const json = ApiLab.element("div", "factor-json");
                ApiLab.renderJson(json, item, {label: "因素分析证据 JSON"}); parent.append(json);
            }
        }
        function field(parent, title, item, empty) {
            parent.append(ApiLab.element("h4", "", title)); value(parent, item, empty);
        }
        function factorCards(parent, entries, candidates) {
            if (!Array.isArray(entries)) { value(parent, entries); return; }
            if (!entries.length) { value(parent, null, candidates ? "本次没有形成初步候选。" : "现有证据不足以筛出适用的相关因素；这不表示相关因素不存在。"); return; }
            entries.forEach((entry, index) => {
                if (!record(entry)) { value(parent, entry); return; }
                const card = fold(parent, label(entry, `因素 ${index + 1}`), true);
                if (entry.relationType) card.append(ApiLab.element("p", "hint", `与报告目标的关系：${relations[entry.relationType] || text(entry.relationType)}`));
                if (entry.conclusion) value(card, entry.conclusion);
                if (candidates) {
                    field(card, "医学来源依据", entry.knowledgeEvidence);
                    field(card, "适用条件", entry.conditions);
                } else {
                    field(card, "提交前判断", entry.before);
                    field(card, "结合答案后的判断", entry.after);
                }
                value(fold(card, "完整因素记录"), entry);
            });
        }
        function answers(parent, entries) {
            if (!Array.isArray(entries) || !entries.length) { value(parent, entries, "没有可用的实际问答证据；不能将未回答视为否认。"); return; }
            for (const answer of entries) {
                if (!record(answer)) { value(parent, answer); continue; }
                const row = ApiLab.element("section", "factor-answer");
                field(row, "实际问题", answer.question?.title || answer.question || answer.questionName);
                field(row, "实际答案", answer.answerText ?? answer.answer ?? answer.rawAnswer, "未提供答案。");
                if (answer.status) row.append(ApiLab.element("p", "hint", `回答状态：${answerStatuses[answer.status] || text(answer.status)}`));
                value(fold(row, "原始答案与引用"), answer); parent.append(row);
            }
        }
        function conditions(parent, entries, definitions) {
            if (!Array.isArray(entries) || !entries.length) { value(parent, entries); return; }
            for (const entry of entries) {
                if (!record(entry)) { value(parent, entry); continue; }
                const condition = list(definitions).find((item) => item?.id === entry.conditionId);
                const row = fold(parent, text(condition?.label || entry.conditionId, "条件证据"), true);
                if (entry.status) row.append(ApiLab.element("p", "hint", `判断：${conditionStatuses[entry.status] || text(entry.status)}`));
                field(row, "支持证据", entry.support);
                field(row, "反对证据", entry.contradicts);
                if (entry.explanation) value(row, entry.explanation);
                value(fold(row, "条件状态与完整引用"), entry);
            }
        }
        function answerChain(parent, target) {
            if (target.answerEvidence !== undefined) answers(fold(parent, "形成该分析目标的实际问答", true), target.answerEvidence);
            const entries = [...list(target.candidateFactors), ...list(target.factors)];
            const seen = new Set();
            for (const entry of entries) {
                if (!record(entry)) continue;
                const key = text(entry.id, JSON.stringify(entry));
                if (seen.has(key)) continue;
                seen.add(key);
                const chain = fold(parent, label(entry, "因素与问答证据"), true);
                answers(chain, entry.answerEvidence);
                conditions(fold(chain, "提交前的条件判断"), entry.before?.conditionAssessments, entry.conditions);
                conditions(fold(chain, "答案如何支持或反对条件", true), entry.after?.conditionAssessments, entry.conditions);
                if (entry.after?.explanation) value(chain, entry.after.explanation);
            }
            if (!seen.size) value(parent, null, "暂无因素与实际问答的对应记录。");
            field(parent, "结论边界与证据缺口", target.evidenceGaps);
        }
        function renderResult(body, result) {
            if (result.summary !== undefined) value(body, result.summary);
            if (result.reportInterpretations !== undefined) {
                const interpretations = fold(body, "报告原有解读");
                interpretations.append(ApiLab.element("p", "hint", "这里保留报告处理阶段已有的 AI 解读；AI 解读不等于原始检测记录，也不会因缺少明确引用而绑定到某个分析目标。"));
                value(interpretations, result.reportInterpretations);
            }
            body.append(ApiLab.element("p", "hint", "以下是基于本次资料与答案的相关因素分析；医学上的关联及适用条件不等于已证明个人病因。"));
            if (!Array.isArray(result.targets)) value(body, result.targets);
            else if (!result.targets.length) value(body, null, "本次没有可展开的报告目标。请结合覆盖情况与原始结果查看结论范围。");
            for (const [index, target] of list(result.targets).entries()) {
                if (!record(target)) { value(body, target); continue; }
                const targetBody = fold(body, text(target.label || target.id, `报告目标 ${index + 1}`), true);
                const report = fold(targetBody, "1. 报告目标与风险解读", true);
                field(report, "原报告证据", target.reportEvidence);
                field(report, "AI 风险解读", target.riskInterpretation);
                factorCards(fold(targetBody, "2. 初步候选与医学来源"), target.candidateFactors, true);
                factorCards(fold(targetBody, "3. 最终相关因素及关系"), target.factors, false);
                answerChain(fold(targetBody, "4. 实际问答如何支持或反对"), target);
            }
            value(fold(body, "医学来源原文与版本"), result.sources);
            value(fold(body, "资料覆盖情况与结论范围"), result.coverage);
            value(fold(body, "完整原始结果 JSON"), result);
            body.append(download("下载因素分析 JSON", "因素分析.json", result));
        }
        function render() {
            host.replaceChildren(); host.hidden = !scope;
            if (!scope) return;
            const head = ApiLab.element("div", "panel-head");
            head.append(ApiLab.element("h2", "", "独立因素分析"));
            const retry = ApiLab.element("button", "button ghost compact", "重新查询分析"); retry.type = "button";
            retry.disabled = loading; retry.addEventListener("click", query); head.append(retry);
            const body = ApiLab.element("div", "panel-body");
            const status = ApiLab.element("p", "hint factor-status", loading ? "正在查询因素分析…" : statuses[assessment?.status] || "等待查询因素分析");
            status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite"); body.append(status);
            body.append(ApiLab.element("p", "hint", "答案已提交，可立即继续 Chat。分析独立进行；重新查询只读取结果，不触发生成。"));
            if (["queued", "running"].includes(assessment?.status)) body.append(ApiLab.element("p", "hint", `每 ${pollDelay() / 1000} 秒自动查询分析进度。`));
            if (assessment?.status === "failed") body.append(ApiLab.element("p", "hint error-text", "分析或查询未成功，未据此形成医学结论。可重新查询服务器状态。"));
            if (assessment?.status === "unavailable") body.append(ApiLab.element("p", "hint", "服务器尚无这份问卷的可用分析记录；查询不会补生成，也不表示未发现风险。"));
            if (assessment?.errorCode) body.append(ApiLab.element("p", "hint", `技术状态：${text(assessment.errorCode)}`));
            if (assessment?.status === "completed" && record(assessment.result)) renderResult(body, assessment.result);
            host.append(head, body);
        }
        function receive(data, token) {
            if (!live(token)) return;
            if (!record(data) || data.questionnaireId !== scope.questionnaireId || data.sessionId !== scope.sessionId) {
                throw new Error("响应不属于当前问卷和会话，已忽略。");
            }
            if (!Object.hasOwn(statuses, data.status) || (data.status === "completed" && !record(data.result))) {
                throw new Error("因素分析响应格式不完整。");
            }
            clearDeadline(); controller = null; loading = false; assessment = data; render();
            if (["queued", "running"].includes(data.status)) timer = setTimeout(() => { timer = null; if (live(token)) query(); }, pollDelay());
        }
        function pollDelay() {
            return Number.isFinite(assessment?.retryAfterSeconds) && assessment.retryAfterSeconds > 0
                ? Math.min(assessment.retryAfterSeconds, 60) * 1000 : 2000;
        }
        function fail(error, token) {
            if (!live(token) || error.stale) return;
            clearDeadline(); controller = null;
            loading = false; assessment = {status: "failed", result: null,
                errorCode: text(error.payload?.data?.errorCode || error.code, error.status ? `HTTP_${error.status}` : error.message || "QUERY_FAILED")};
            render();
        }
        async function query() {
            if (!live() || loading) return;
            stop(); const token = epoch; loading = true; render();
            controller = new AbortController();
            deadline = setTimeout(() => {
                if (!live(token)) return;
                stop(); fail(Object.assign(new Error("因素分析查询超时，可重新查询。"), {code: "QUERY_TIMEOUT"}), epoch);
            }, 30000);
            const endpoint = ApiLabContracts.find((item) => item.id === endpointId);
            const snapshot = ApiLab.context.get();
            try {
                const request = ApiLabCore.buildRequest(endpoint, {questionnaireId: scope.questionnaireId, userId: snapshot.userId});
                const payload = await ApiLab.request(request.path, {...request, snapshot, revision: scope.revision, signal: controller.signal});
                receive(payload.data, token);
            } catch (error) { fail(error, token); }
        }
        function follow(questionnaire) {
            if (questionnaire?.status !== "submitted") { reset(); return; }
            if (scope?.questionnaireId === questionnaire.questionnaireId && live()) return;
            reset(); scope = {questionnaireId: questionnaire.questionnaireId, sessionId: ApiLab.context.get().sessionId, revision: ApiLab.context.revision};
            ApiLab.setParams(endpointId, {questionnaireId: scope.questionnaireId}, {touch: false});
            void query();
        }
        function startManual({parameters}) {
            if (!scope || parameters.questionnaireId !== scope.questionnaireId) return;
            stop(); manualEpoch = epoch; loading = true; render();
        }
        function receiveManual(payload, {parameters}) {
            if (!scope || parameters.questionnaireId !== scope.questionnaireId || manualEpoch === null) return;
            const token = manualEpoch; manualEpoch = null;
            try { receive(payload.data, token); } catch (error) { fail(error, token); }
        }
        function failManual(error) {
            if (manualEpoch === null) return;
            const token = manualEpoch; manualEpoch = null; fail(error, token);
        }
        return {follow, reset, stop() { stop(); render(); }, resume() { if (live()) void query(); }, startManual, receiveManual, failManual};
    }
    return {mount, endpointId};
})();
