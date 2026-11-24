(() => {
    const conversation = document.querySelector("#conversation");
    const nearBottom = () => conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight <= 48;
    function addMessage(
        role,
        content,
        reasoning = "",
        payload = {},
        displayType = "CARD",
        customType = "DOCTOR_MESSAGE_CARD",
        customTitle = "",
    ) {
        const follow = nearBottom();
        conversation.querySelector(".empty")?.remove();
        if (role === "custom" && displayType === "CARD") {
            const typeLabels = {
                DOCTOR_MESSAGE_CARD: "医生消息",
                HEALTH_REPORT_CARD: "医生已签发",
                INTERVESE_CARD: "干预方案",
                DIAGNOSIS_CARD: "诊断意见",
            };
            const defaultTitles = {
                DOCTOR_MESSAGE_CARD: "医生消息",
                HEALTH_REPORT_CARD: "医生签发报告",
                INTERVESE_CARD: "健康干预方案",
                DIAGNOSIS_CARD: "诊断意见",
            };
            const card = document.createElement("article");
            card.className = "message custom-card";

            const badge = document.createElement("span");
            badge.className = "custom-card-badge";
            badge.textContent = typeLabels[customType] || customType;

            const title = document.createElement("strong");
            title.className = "custom-card-title";
            title.textContent = customTitle || payload.title || defaultTitles[customType] || "业务消息";

            const contentText = document.createElement("div");
            contentText.className = "custom-card-content";
            contentText.textContent = content;

            card.append(badge, title, contentText);
            if (payload.reportId) {
                const reference = document.createElement("span");
                reference.className = "custom-card-reference";
                reference.textContent = `报告引用：${payload.reportId}`;
                card.append(reference);
            }
            conversation.append(card);
            if (follow) conversation.scrollTop = conversation.scrollHeight;
            return card;
        }

        const item = document.createElement("div");
        item.className = `message ${role}`;
        if (reasoning) {
            const reasoningBlock = document.createElement("div");
            reasoningBlock.className = "message-reasoning";
            const reasoningLabel = document.createElement("div");
            reasoningLabel.className = "message-reasoning-label";
            reasoningLabel.textContent = "模型推理过程";
            const reasoningText = document.createElement("div");
            reasoningText.className = "message-reasoning-text";
            reasoningText.textContent = reasoning;
            reasoningBlock.append(reasoningLabel, reasoningText);
            item.append(reasoningBlock);
        }
        const contentText = document.createElement("div");
        contentText.className = "message-content";
        contentText.textContent = content;
        item.append(contentText);
        conversation.append(item);
        if (follow) conversation.scrollTop = conversation.scrollHeight;
        return item;
    }

    const suggestions = document.querySelector("#suggestion-list");
    let streamCard = null;
    let replyText = null;
    let reasoningText = null;
    let reasoningBlock = null;
    let hasReply = false;
    let replyChunks = [];
    let reasoningChunks = [];
    let scrollFrom = 0;
    const scrollBatch = ApiLab.batch(() => {
        // Read the new height in a separate task, not immediately after writing
        // a long text node. A user scroll between tasks takes precedence.
        if (streamCard && conversation.scrollTop === scrollFrom) conversation.scrollTop = conversation.scrollHeight;
    }, 0);
    const streamBatch = ApiLab.batch(renderStream);
    function clearStream() {
        streamBatch.cancel(); scrollBatch.cancel();
        streamCard = replyText = reasoningText = reasoningBlock = null;
        hasReply = false; replyChunks = []; reasoningChunks = [];
    }
    function reset() {
        conversation.innerHTML = '<p class="empty">已切换公共身份，可发送消息或加载此会话历史。</p>';
        suggestions.replaceChildren();
        clearStream();
    }
    function startStream() {
        // Reserve this card before custom messages can arrive; never identify it
        // by lastElementChild after another request has inserted a card.
        streamCard = addMessage("assistant", "");
        replyText = document.createTextNode("正在生成…");
        streamCard.querySelector(".message-content").append(replyText);
        reasoningBlock = ApiLab.element("div", "message-reasoning");
        reasoningBlock.hidden = true;
        const content = ApiLab.element("div", "message-reasoning-text");
        reasoningText = document.createTextNode(""); content.append(reasoningText);
        reasoningBlock.append(ApiLab.element("div", "message-reasoning-label", "模型推理过程"), content);
        streamCard.prepend(reasoningBlock);
    }
    function renderStream() {
        if (!streamCard || (!replyChunks.length && !reasoningChunks.length)) return;
        const follow = nearBottom();
        const previousTop = conversation.scrollTop;
        if (replyChunks.length) {
            if (!hasReply) { replyText.data = ""; hasReply = true; }
            replyText.appendData(replyChunks.join("")); replyChunks = [];
        }
        if (reasoningChunks.length) {
            reasoningBlock.hidden = false;
            reasoningText.appendData(reasoningChunks.join("")); reasoningChunks = [];
        }
        if (follow) { scrollFrom = previousTop; scrollBatch.schedule(); }
    }
    function renderMessage(message) {
        addMessage(message.role === "custom" ? "custom" : message.role === "user" ? "user" : "assistant",
            message.content, message.reasoning || "", message.payload || {}, message.displayType || "CARD",
            message.customType || message.payload?.customType || "DOCTOR_MESSAGE_CARD", message.title || "");
    }
    ApiLab.initialize({group: "chat", initialEndpoint: "chat.send.complete", onReset: reset,
        onStart({id, parameters}) {
            if (id.startsWith("chat.send.")) {
                addMessage("user", parameters.message ?? "（未提供 message）");
                clearStream();
                if (id === "chat.send.stream") startStream();
            }
        },
        onEvent(payload) {
            const data = payload.data || payload;
            if (!data.content) return;
            if (data.type === "reasoning") reasoningChunks.push(data.content);
            else if (data.type === "content") replyChunks.push(data.content);
            else return;
            streamBatch.schedule();
        },
        onResponse(payload, {id}) {
            const data = payload.data || {};
            if (id === "chat.send.complete") addMessage("assistant", data.reply || "", data.reasoning || "");
            if (id === "chat.send.stream") {
                streamBatch.flush();
                if (streamCard && !hasReply) replyText.data = "流已结束，但没有收到正文内容。";
            }
            if (id === "chat.history") {
                conversation.replaceChildren();
                (data.messages || []).forEach(renderMessage);
                if (!data.messages?.length) conversation.append(ApiLab.element("p", "empty", "该会话暂无业务历史。"));
            }
            if (id === "chat.suggestions") {
                suggestions.replaceChildren();
                (data.suggestions || []).forEach((text) => {
                    const button = ApiLab.element("button", "chip", text);
                    button.addEventListener("click", () => {
                        ApiLab.draft("chat.send.complete").mode = "form";
                        ApiLab.setParams("chat.send.complete", {message: text});
                        ApiLab.select("chat.send.complete");
                    });
                    suggestions.append(button);
                });
            }
            if (id === "chat.messages.custom" && data.message && !data.queued) renderMessage({...data.message, role: "custom"});
            if (id === "chat.messages.custom" && data.queued) ApiLab.log("卡片已排队", "AI 完成后可加载历史查看持久化顺序。");
        },
        onError(error, {id}) {
            if (id === "chat.send.stream") {
                streamBatch.flush();
                if (streamCard && !hasReply) replyText.data = "流已中断，未收到正文内容。";
            }
            if (id.startsWith("chat.send.")) addMessage("assistant", `请求失败：${error.message}`);
        },
    });
    ApiLab.bindInput("#message", "chat.send.complete", "message");
    document.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => {
        ApiLab.select(button.dataset.action); ApiLab.run(button.dataset.action);
    }));
    document.querySelector("#edit-custom").addEventListener("click", () => {
        ApiLab.select("chat.messages.custom"); document.querySelector("#api-editor").scrollIntoView({block: "start"});
    });
    document.querySelector("#message").addEventListener("keydown", (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); ApiLab.select("chat.send.complete"); ApiLab.run("chat.send.complete"); }
    });
})();
