(() => {
    const questionArea = document.querySelector("#question-area");
    const submitButton = document.querySelector("#submit");
    const streamButton = document.querySelector("#submit-stream");
    const noMoreButton = document.querySelector("#no-more");
    let currentQuestion = null;
    let selectedOptions = [];
    const exclusiveAnswers = new Set(["没有", "不清楚", "暂无改善需求", "没有需要补充的内容", "目前记不清", "没有留意", "记不清", "没有主动活动", "没有运动"]);
    let pending = false;
    let reasoning = "";
    let reasoningNode = null;
    let reasoningChunks = [];
    const reasoningBatch = ApiLab.batch(() => {
        if (!reasoningChunks.length) return;
        if (!reasoningNode) {
            const block = ApiLab.element("div", "reasoning record-text");
            reasoningNode = document.createTextNode("");
            block.append(reasoningNode); questionArea.append(block);
        }
        reasoningNode.appendData(reasoningChunks.join("")); reasoningChunks = [];
    });
    function cancelReasoning() { reasoningBatch.cancel(); reasoningChunks = []; }

    function updateButtons() {
        submitButton.disabled = streamButton.disabled = pending || !currentQuestion;
        noMoreButton.hidden = currentQuestion?.phase !== "supplement" || Array.isArray(currentQuestion?.options);
        noMoreButton.disabled = pending;
    }
    function showMessage(message) {
        if (!message) return;
        const card = ApiLab.element("div", "question-card record-assistant");
        card.append(ApiLab.element("strong", "", "AI 回应"), ApiLab.element("p", "record-text", message));
        questionArea.append(card);
    }
    function showReasoning(parent, value) {
        if (!value) return;
        const detail = ApiLab.element("details", "reasoning");
        detail.append(ApiLab.element("summary", "", "查看 reasoning"), ApiLab.element("div", "record-text", value));
        parent.append(detail);
    }
    function showDocumentProcessing(data) {
        const entries = Array.isArray(data.documentProcessing) ? data.documentProcessing : [];
        if (!entries.length) return;
        const card = ApiLab.element("div", "question-card record-documents");
        card.append(ApiLab.element("strong", "", "本次附件"));
        const labels = {complete: "已读取", partial: "部分读取", unavailable: "暂不可用"};
        for (const entry of entries) {
            card.append(ApiLab.element("p", "record-text", `附件 ${entry.fileIndex + 1} · ${labels[entry.status] || entry.status}：${entry.message || ""}`));
        }
        questionArea.append(card);
    }
    function renderQuestion(data) {
        cancelReasoning();
        const next = data.status === "question" ? data : null;
        const changed = next?.questionId !== currentQuestion?.questionId;
        if (changed || !next) {
            selectedOptions = [];
            ApiLab.setParams("record.answer.complete", {answer: undefined, other: undefined}, {touch: false});
        }
        currentQuestion = next;
        reasoningNode = null;
        questionArea.replaceChildren();
        ApiLab.setParams("record.answer.complete", {questionId: next?.questionId}, {onlyDefault: true});
        ApiLab.setParams(["record.continue", "record.complete"], {round: data.round ?? 1}, {onlyDefault: true});
        showMessage(data.assistantMessage);
        showDocumentProcessing(data);

        if (data.status === "finished" || data.status === "completed") {
            const card = ApiLab.element("div", "question-card");
            card.append(ApiLab.element("strong", "", "建档已完成"), ApiLab.element("p", "", "可以继续使用当前会话进行 Chat 对话。"));
            showReasoning(card, data.reasoning);
            questionArea.append(card);
            updateButtons();
            return;
        }
        if (data.status === "processing" || data.status === "failed") {
            const card = ApiLab.element("div", "question-card record-creation");
            card.append(ApiLab.element("strong", "", data.status === "processing" ? "正在准备建档" : "建档准备未完成"));
            card.append(ApiLab.element("p", "", data.message || "请查询当前会话状态。"));
            card.append(ApiLab.element("p", "", "页面不会自动刷新，请点击“恢复当前问题”手动查询状态。"));
            if (data.stage) card.append(ApiLab.element("p", "", `处理阶段：${data.stage}`));
            if (data.errorCode) card.append(ApiLab.element("p", "", `错误代码：${data.errorCode}`));
            if (data.retryable) card.append(ApiLab.element("p", "", "后台将按间隔尝试恢复，请保留当前会话和原始资料。"));
            if (data.expiresAt && Date.parse(data.expiresAt) <= Date.now()) card.append(ApiLab.element("p", "", "临时创建记录已到期，请查询状态确认后重新建档。"));
            questionArea.append(card);
            updateButtons();
            return;
        }
        if (!next) {
            questionArea.append(ApiLab.element("p", "empty", "当前响应没有待答问题，请查询当前会话状态。"));
            updateButtons();
            return;
        }
        const card = ApiLab.element("div", "question-card");
        const meta = ApiLab.element("div", "question-meta");
        meta.append(ApiLab.element("span", "", data.questionId), ApiLab.element("span", "", data.questionType));
        card.append(meta, ApiLab.element("strong", "record-text", data.question));
        showReasoning(card, data.reasoning);

        if (Array.isArray(data.options)) {
            const optionBox = ApiLab.element("div", "options");
            data.options.forEach((option) => {
                const button = ApiLab.element("button", "option", typeof option === "string" ? option : JSON.stringify(option));
                button.type = "button";
                if (selectedOptions.includes(option)) button.classList.add("selected");
                button.addEventListener("click", () => {
                    if (pending) return;
                    if (data.questionType === "checkbox") {
                        if (selectedOptions.includes(option)) selectedOptions = selectedOptions.filter((item) => item !== option);
                        else if (exclusiveAnswers.has(option)) selectedOptions = [option];
                        else selectedOptions = [...selectedOptions.filter((item) => !exclusiveAnswers.has(item)), option];
                    } else selectedOptions = [option];
                    Array.from(optionBox.children).forEach((item, index) => item.classList.toggle("selected", selectedOptions.includes(data.options[index])));
                    ApiLab.setParams("record.answer.complete", {answer: selectedOptions}, {touch: false});
                    if (data.phase === "supplement" && data.questionType === "radio" && data.options.length === 1 && option === "没有了") {
                        ApiLab.select("record.answer.complete");
                        return ApiLab.run("record.answer.complete");
                    }
                });
                optionBox.append(button);
            });
            card.append(optionBox);
        } else if (data.options && typeof data.options === "object") {
            const template = ApiLab.element("button", "button ghost", "填入结构化回答模板");
            template.type = "button";
            template.addEventListener("click", () => {
                if (pending) return;
                ApiLab.setParams("record.answer.complete", {answer: data.options, other: undefined}, {touch: false});
                ApiLab.select("record.answer.complete");
            });
            const tools = ApiLab.element("div", "actions"); tools.append(template); card.append(tools);
        }
        questionArea.append(card);
        updateButtons();
    }
    function formatHistoryAnswer(value) {
        if (value == null || value === "") return "—";
        if (Array.isArray(value) && value.every((item) => item === null || typeof item !== "object")) {
            return value.map((item) => item == null || item === "" ? "—" : String(item)).join("\n") || "—";
        }
        return typeof value === "object" ? ApiLab.format(value) : String(value);
    }
    function historySection(label, content, className) {
        const section = ApiLab.element("div", `record-history-section ${className}`);
        section.append(ApiLab.element("strong", "record-history-label", label), ApiLab.element("p", "record-history-content record-text", content));
        return section;
    }
    function renderHistory(data) {
        if (data.status === "processing" || data.status === "failed") { renderQuestion(data); return; }
        currentQuestion = null; selectedOptions = []; reasoningNode = null;
        questionArea.replaceChildren();
        showDocumentProcessing(data);
        const history = ApiLab.element("div", "record-history");
        for (const [index, item] of (data.history || []).entries()) {
            const card = ApiLab.element("article", "record-history-turn");
            card.append(ApiLab.element("h3", "record-history-heading", `问答 ${index + 1}`),
                historySection("AI 说明", item.assistantMessage || "—", "record-history-assistant"),
                historySection("AI 问题", item.question || "—", "record-history-question"),
                historySection("用户回答", formatHistoryAnswer(item.answer), "record-history-answer"));
            history.append(card);
        }
        if (history.children.length) questionArea.append(history);
        showMessage(data.assistantMessage);
        if (data.status === "finished" || data.status === "completed") questionArea.append(ApiLab.element("p", "empty", "建档已完成。"));
        else questionArea.append(ApiLab.element("p", "empty", "当前为历史回放，点击“恢复当前问题”继续回答。"));
        updateButtons();
    }
    function reset() {
        cancelReasoning();
        currentQuestion = null; selectedOptions = []; pending = false; reasoning = ""; reasoningNode = null;
        questionArea.replaceChildren(ApiLab.element("p", "empty", "已切换公共身份，请创建或恢复当前会话。"));
        updateButtons();
    }
    ApiLab.initialize({group: "record", formPanels: {"record.create": "#record-attachments"}, onReset: reset,
        onStart() { cancelReasoning(); pending = true; reasoning = ""; reasoningNode = null; updateButtons(); },
        onEvent(payload) {
            const data = payload.data || payload;
            if (data.type === "reasoning") {
                reasoning += data.content || "";
                if (data.content) { reasoningChunks.push(data.content); reasoningBatch.schedule(); }
            } else if (data.type === "question" || (data.type === "finish" && data.status === "finished")) {
                renderQuestion({...data, reasoning: data.reasoning ?? reasoning});
            }
        },
        onResponse(payload, {id}) {
            reasoningBatch.flush();
            pending = false;
            const data = payload.data || {};
            if (id === "record.history") renderHistory(data);
            else if (id !== "record.answer.stream") renderQuestion(data);
            updateButtons();
        },
        onError(error, {id}) {
            reasoningBatch.flush();
            pending = false;
            if (error.payload?.data?.status === "failed") renderQuestion(error.payload.data);
            else if (id === "record.create" && (!error.status || error.status >= 500)) {
                showMessage("本次请求未确认完成，请使用当前会话查询状态，重试创建时保留原始资料。");
            }
            updateButtons();
        },
    });
    ApiLab.bindInput("#other-answer", "record.answer.complete", "other");
    noMoreButton.addEventListener("click", () => {
        ApiLab.setParams("record.answer.complete", {answer: undefined, other: "没有了"}, {touch: false});
        ApiLab.select("record.answer.complete");
    });
    document.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => {
        ApiLab.select(button.dataset.action); ApiLab.run(button.dataset.action);
    }));
    updateButtons();
})();
