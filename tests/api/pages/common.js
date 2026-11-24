const ApiLab = (() => {
    const core = ApiLabCore;
    let storage;
    try { storage = window.localStorage; } catch { storage = null; }
    const context = core.createContextStore(storage);
    const drafts = new Map();
    const endpoints = new Map(ApiLabContracts.map((item) => [item.id, item]));
    const listeners = new Set();
    const active = new Set();
    const progressBatches = new Set();
    let config = {};
    let selected = null;
    let busy = 0;
    let mounted = false;
    let editorProblem = "";
    const formPanels = [];
    const $ = (selector) => document.querySelector(selector);

    // One pending task per view, never one task per SSE event. flush() also works
    // in background tabs, where animation frame callbacks can be suspended.
    function batch(render, delay = 50) {
        let timer = null;
        function cancel() { if (timer !== null) clearTimeout(timer); timer = null; }
        function flush() { cancel(); render(); }
        return {schedule() { if (timer === null) timer = setTimeout(flush, delay); }, flush, cancel};
    }
    const logEntries = [];
    let requestSequence = 0;
    let logPreviews = new Map();
    let logDirty = true;
    const logBatch = batch(renderLog, 200);
    function renderLog() {
        if (!logDirty) return;
        const target = $("#raw-log");
        if (!target) return;
        logDirty = false;
        const previews = new Map();
        let size = 0;
        for (let index = logEntries.length - 1; index >= Math.max(0, logEntries.length - 200); index--) {
            const entry = logEntries[index];
            let preview = logPreviews.get(entry);
            if (!preview) {
                const rendered = typeof entry.payload === "string" ? entry.payload : core.format(entry.payload);
                const full = `[${entry.time}] ${entry.title}\n${rendered}\n\n`;
                preview = document.createTextNode(full.length > 8192
                    ? full.slice(0, 8100) + "\n…（预览已截断，请导出完整记录）\n\n" : full);
            }
            if (size + preview.textContent.length > 256 * 1024) break;
            size += preview.textContent.length;
            previews.set(entry, preview);
        }
        // Only the bounded window is mounted; the archive never holds DOM nodes.
        target.replaceChildren(...previews.values());
        logPreviews = previews;
        const status = $("#log-summary");
        if (status) status.textContent = `显示最近 ${previews.size} / ${logEntries.length} 条；完整记录可导出，清空或刷新页面后释放。`;
    }
    function clearLog() {
        logBatch.cancel(); logEntries.length = 0; logPreviews.clear(); logDirty = true; renderLog();
    }
    function exportLog() {
        const blob = new Blob(logEntries.map((entry) => JSON.stringify(entry) + "\n"), {type: "application/x-ndjson;charset=utf-8"});
        const url = URL.createObjectURL(blob);
        const link = element("a");
        link.href = url; link.download = `api-lab-${new Date().toISOString().replace(/[:.]/g, "-")}.jsonl`;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    class StaleResponseError extends Error {
        constructor() { super("公共身份或后端已改变，旧请求仅保留网络记录"); this.stale = true; }
    }
    function guard(revision) { if (revision !== context.revision) throw new StaleResponseError(); }
    function log(title, value, metadata = {}) {
        logEntries.push({time: new Date().toISOString(), requestId: null, eventSequence: null, ...metadata, title, payload: value});
        logDirty = true;
        logBatch.schedule();
    }
    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }
    function targetUrl(path, backend) {
        if (!backend) throw new Error("请填写后端地址");
        const url = new URL(backend);
        if (!["http:", "https:"].includes(url.protocol)) throw new Error("后端地址必须使用 HTTP(S)");
        const target = backend + path;
        return location.protocol === "file:" ? target : `${location.origin}/proxy?target=${encodeURIComponent(target)}`;
    }
    function updateBusy(delta = 0) {
        busy += delta;
        const badge = $("#request-status");
        if (badge) badge.textContent = busy ? `请求处理中（${busy}）` : "就绪";
        const button = $("#api-send");
        if (button) button.disabled = !!editorProblem || (busy > 0 && selected !== "chat.messages.custom");
    }
    async function request(path, {method = "POST", body, snapshot = context.get(), revision = context.revision, signal, timeoutMs} = {}) {
        const options = {method, headers: {Accept: "application/json"}};
        let timer = null;
        let timedOut = false;
        const controller = timeoutMs ? new AbortController() : null;
        const forwardAbort = () => controller?.abort(signal?.reason);
        if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
        const url = targetUrl(path, snapshot.backend);
        const metadata = {requestId: ++requestSequence, path};
        log(`REQUEST ${method} ${snapshot.backend}${path}`, body ?? null, metadata);
        logBatch.flush();
        const started = performance.now();
        updateBusy(1);
        try {
            if (controller) {
                if (signal?.aborted) forwardAbort();
                else signal?.addEventListener("abort", forwardAbort, {once: true});
                timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
                options.signal = controller.signal;
            } else if (signal) options.signal = signal;
            const response = await fetch(url, options);
            const text = await response.text();
            let payload;
            try { payload = JSON.parse(text); } catch { payload = {raw: text}; }
            log(`RESPONSE ${response.status} · ${Math.round(performance.now() - started)}ms · ${path}`, payload, metadata);
            guard(revision);
            if (!response.ok || payload?.code >= 400) {
                const error = new Error(payload?.message || `HTTP ${response.status}`);
                error.payload = payload; error.status = response.status;
                throw error;
            }
            return payload;
        } catch (error) {
            guard(revision);
            if (timedOut) throw new Error("等待完整响应超时，连接已取消；请检查响应记录后再显式重试。");
            throw error;
        }
        finally {
            if (timer !== null) clearTimeout(timer);
            signal?.removeEventListener("abort", forwardAbort);
            updateBusy(-1); logBatch.flush();
        }
    }
    async function stream(path, {body, onEvent, snapshot = context.get(), revision = context.revision} = {}) {
        const url = targetUrl(path, snapshot.backend);
        const metadata = {requestId: ++requestSequence, path};
        log(`REQUEST STREAM POST ${snapshot.backend}${path}`, body, metadata);
        logBatch.flush();
        let reader;
        let count = 0;
        updateBusy(1);
        try {
            const response = await fetch(url, {method: "POST", headers: {Accept: "text/event-stream", "Content-Type": "application/json"}, body: JSON.stringify(body)});
            if (!response.ok) {
                const text = await response.text();
                let payload;
                try { payload = JSON.parse(text); } catch { payload = {raw: text}; }
                log(`RESPONSE ${response.status}`, payload, metadata);
                guard(revision);
                const error = new Error(payload?.message || `HTTP ${response.status}`);
                error.payload = payload; error.status = response.status;
                throw error;
            }
            if (!response.body) throw new Error("浏览器未提供可读的流式响应体");
            reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";
            let sliceStarted = performance.now();
            function consume(block) {
                const text = block.split(/\r?\n/).filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
                if (!text) return;
                let payload;
                try { payload = JSON.parse(text); } catch { payload = {raw: text}; }
                count += 1;
                log(`STREAM EVENT ${count} · ${path}`, payload, {...metadata, eventSequence: count});
                guard(revision);
                const data = payload?.data || payload;
                if (data?.type === "error" || payload?.code >= 400) {
                    const error = new Error(data.error || payload.message || "流式请求失败");
                    error.payload = payload; throw error;
                }
                onEvent?.(payload);
            }
            while (true) {
                const {value, done} = await reader.read();
                buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
                const blocks = buffer.split(/\r?\n\r?\n/);
                buffer = blocks.pop() || "";
                for (const block of blocks) {
                    consume(block);
                    if (performance.now() - sliceStarted >= 8) {
                        await new Promise((resolve) => setTimeout(resolve, 0));
                        guard(revision);
                        sliceStarted = performance.now();
                    }
                }
                guard(revision);
                if (done) break;
                if (performance.now() - sliceStarted >= 8) {
                    await new Promise((resolve) => setTimeout(resolve, 0));
                    guard(revision);
                    sliceStarted = performance.now();
                }
            }
            if (buffer.trim()) consume(buffer);
            log("STREAM COMPLETE", {path, eventCount: count}, metadata);
            return count;
        } catch (error) {
            log("STREAM ERROR", {message: error.message, response: error.payload}, metadata);
            if (reader) { try { await reader.cancel(); } catch { /* Connection may already be closed. */ } }
            guard(revision); throw error;
        } finally { reader?.releaseLock(); updateBusy(-1); logBatch.flush(); }
    }
    function draft(id) {
        const endpoint = endpoints.get(id);
        if (!endpoint) throw new Error(`未知接口 ${id}`);
        const key = endpoint.draftKey || id;
        if (!drafts.has(key)) drafts.set(key, new core.RequestDraft(endpoint));
        return drafts.get(key);
    }
    function notifyDrafts(force = false) { listeners.forEach((listener) => listener(force)); }
    function subscribeDrafts(listener) {
        listeners.add(listener); listener();
        return () => listeners.delete(listener);
    }
    function setParams(ids, values, options = {}) {
        for (const id of Array.isArray(ids) ? ids : [ids]) {
            for (const [name, value] of Object.entries(values)) draft(id).set(name, value, options);
        }
        notifyDrafts();
        if (selected) renderFields();
    }
    function preview() {
        const target = $("#api-preview");
        if (!target || !selected) return;
        const endpoint = endpoints.get(selected);
        const model = draft(selected);
        const json = $("#api-json");
        const status = $("#api-json-status");
        editorProblem = "";
        let parameters;
        try {
            parameters = model.parameters(context.get(), false);
            const outgoing = core.buildRequest(endpoint, parameters);
            target.textContent = `${outgoing.method} ${context.get().backend}${outgoing.path}`;
            const text = model.mode === "json" ? model.json : core.format(parameters);
            if (json.value !== text) json.value = text;
            json.disabled = false;
            try { model.parameters(context.get()); } catch (error) { editorProblem = error.message; }
        } catch (error) {
            editorProblem = error.message;
            target.textContent = `${endpoint.method} ${context.get().backend}${endpoint.path}`;
            const text = model.mode === "json" ? model.json : "";
            if (json.value !== text) json.value = text;
            json.disabled = model.mode === "form";
        }
        $("#api-json-label").textContent = endpoint.fields.some((field) => field.location) ? "请求参数 JSON（路径、查询参数和正文分开发送）" : endpoint.method === "GET" ? "查询参数 JSON（可直接编辑）" : "请求体 JSON（可直接编辑）";
        $("#api-request-type").textContent = endpoint.fields.some((field) => field.location) ? "按字段声明编码到路径或查询参数，其余字段作为 JSON 正文" : endpoint.method === "GET" ? "参数编码到上方 URL，不发送请求体" : "Content-Type: application/json";
        status.textContent = editorProblem || (model.mode === "json" ? (endpoint.fields.some((field) => field.location) ? "将按此参数 JSON 编码路径、查询参数和正文" : "将按此 JSON 原样发送") : "与左侧表单实时同步；直接修改即切换为手工 JSON");
        status.className = editorProblem ? "hint error-text" : "hint";
        json.setAttribute("aria-invalid", String(!!editorProblem));
        json.placeholder = editorProblem || "{}";
        $("#api-format-json").disabled = $("#api-copy-json").disabled = parameters === undefined;
        updateBusy();
    }
    function syncEditorMode() {
        const model = draft(selected);
        const jsonMode = model.mode === "json";
        $("#api-mode").value = model.mode;
        $("#api-form-controls").disabled = jsonMode;
        $("#api-form-title").textContent = jsonMode ? "参数表单（已暂停）" : "可视化参数表单";
        $("#api-reset-form").disabled = jsonMode;
        $("#api-source-badge").textContent = jsonMode ? "手工 JSON" : "表单实时同步";
        $("#api-hint").textContent = jsonMode
            ? "当前按右侧 JSON 原样发送。可删除必填字段、添加字段或临时覆盖身份；切回参数表单可继续填写，JSON 草稿会保留。"
            : endpoints.get(selected).hint || "可选参数留空即不发送；对象、数组可载入示例。完整响应与 SSE 共用参数。";
    }
    function defaultLabel(field) {
        return field.serverDefault === undefined ? "未设置" : "后端默认 " + String(field.serverDefault);
    }
    function renderChoices(field, label, model, apply) {
        const multiple = field.type === "array";
        const group = element("fieldset", "request-choices");
        group.setAttribute("aria-labelledby", label.id);
        const choices = field.choices || ["true", "false"];
        const labels = field.choiceLabels || (field.type === "boolean" ? {true: "是（true）", false: "否（false）"} : {});
        const buttons = (multiple || field.required ? choices : ["", ...choices]).map((choice) => {
            const option = element("label", "request-choice");
            const button = element("input");
            button.type = multiple ? "checkbox" : "radio";
            button.name = "param-" + field.name;
            button.value = choice;
            const text = choice === "" ? defaultLabel(field) : labels[choice] || choice;
            option.append(button, element("span", "", text));
            group.append(option);
            return button;
        });
        if (multiple) {
            let values;
            try { values = JSON.parse(model.values[field.name] || "[]"); } catch { values = []; }
            if (!Array.isArray(values)) values = [];
            buttons.forEach((button) => {
                button.checked = values.includes(button.value);
                button.addEventListener("change", () => {
                    const picks = buttons.filter((item) => item.checked).map((item) => item.value);
                    apply(picks.length ? picks : undefined);
                });
            });
        } else {
            buttons.forEach((button) => {
                button.checked = model.values[field.name] === button.value;
                button.addEventListener("change", () => {
                    if (button.checked) apply(button.value);
                });
            });
        }
        return group;
    }
    function renderFields() {
        const host = $("#api-fields");
        if (!host || !selected) return;
        const endpoint = endpoints.get(selected);
        const model = draft(selected);
        host.replaceChildren();
        syncEditorMode();
        formPanels.forEach(({id, node}) => { node.hidden = id !== selected; });
        $("#api-json-feedback").textContent = "";
        if (endpoint.fields.length) {
            const heading = element("div", "request-field-head");
            heading.setAttribute("aria-hidden", "true");
            ["字段", "含义", "要求", "参数"].forEach((text) => heading.append(element("span", "", text)));
            host.append(heading);
        }
        for (const field of endpoint.fields) {
            const row = element("div", "request-field-row");
            const hasChoices = field.type === "boolean" || field.choices;
            const identity = core.identityNames.has(field.name);
            const inputId = "param-" + field.name;
            const label = element(hasChoices ? "span" : "label", "request-field-name", field.name);
            label.id = inputId + "-label";
            if (!hasChoices) label.htmlFor = inputId;
            const meaning = element("div", "request-field-meaning", field.description || field.hint || "");
            meaning.id = inputId + "-description";
            if (field.description && field.hint) meaning.append(element("small", "request-field-note", field.hint));
            if (identity) meaning.append(element("small", "request-field-note", "与公共设置同步"));
            const requirement = element("span", "request-field-requirement" + (field.required ? " is-required" : ""), field.required ? "必填" : "可选");
            requirement.id = inputId + "-requirement";
            const control = element("div", "request-field-control");
            function apply(value, {raw = false, rebuild = false} = {}) {
                model.set(field.name, value, {raw});
                notifyDrafts();
                if (rebuild) renderFields();
                else preview();
            }
            let input;
            if (hasChoices) {
                input = renderChoices(field, label, model, apply);
                control.append(input);
                if (field.type === "array" && !field.required) {
                    control.append(element("small", "request-field-note", "未勾选时 · " + defaultLabel(field)));
                }
            } else {
                if (["json", "object", "array"].includes(field.type) || field.multiline) {
                    input = element("textarea");
                    input.spellcheck = false;
                } else {
                    input = element("input");
                    input.type = ["integer", "number"].includes(field.type) ? "number" : "text";
                    if (input.type === "number") input.step = field.type === "integer" ? "1" : "any";
                }
                input.readOnly = identity;
                input.value = identity ? context.get()[field.name] : model.values[field.name];
                input.disabled = model.omitted.has(field.name);
                input.placeholder = field.placeholder || (field.required || identity ? "" : defaultLabel(field));
                input.addEventListener("input", () => {
                    if (identity) context.update({[field.name]: input.value});
                    else apply(input.value, {raw: true});
                });
                control.append(input);
            }
            input.id = inputId;
            input.dataset.param = field.name;
            input.setAttribute("aria-describedby", meaning.id + " " + requirement.id);
            if (field.urlInput) {
                const typeLabel = element("label", "", "URL 文件类型（仅作用于直接粘贴的 URL）");
                const type = element("select");
                type.id = inputId + "-url-type";
                typeLabel.htmlFor = type.id;
                type.append(new Option("自动识别 PDF／图片", ""), new Option("全部按 PDF", "pdf"), new Option("全部按图片", "image"));
                type.value = model.urlFileTypes[field.name] || "";
                type.addEventListener("change", () => {
                    model.urlFileTypes[field.name] = type.value;
                    notifyDrafts(); preview();
                });
                control.append(typeLabel, type);
            }
            if (identity && !field.required) {
                const toggle = element("label", "request-choice");
                const omit = element("input");
                omit.type = "checkbox";
                omit.checked = model.omitted.has(field.name);
                omit.dataset.omit = field.name;
                toggle.append(omit, element("span", "", "此接口不发送此参数"));
                omit.addEventListener("change", () => {
                    if (omit.checked) model.omitted.add(field.name);
                    else model.omitted.delete(field.name);
                    input.disabled = omit.checked;
                    preview();
                });
                control.append(toggle);
            }
            if (field.example !== undefined) {
                const button = element("button", "button ghost compact", "载入示例");
                button.type = "button";
                button.addEventListener("click", () => apply(field.example, {rebuild: true}));
                control.append(button);
            }
            row.append(label, meaning, requirement, control);
            host.append(row);
        }
        if (!endpoint.fields.length) host.append(element("p", "hint", "此接口没有请求参数，可以直接发送。"));
        preview();
    }
    function select(id) {
        if (!$("#api-select") || !Array.from($("#api-select").options).some((option) => option.value === id)) return;
        selected = id; $("#api-select").value = id; renderFields();
    }
    function showError(error, showResult = true) {
        log(error.stale ? "忽略旧请求" : "请求失败", String(error));
        if (error.stale || !showResult) return;
        const responsePanel = $("#lab-response");
        if (responsePanel?.tagName === "DETAILS") responsePanel.open = true;
        const result = $("#api-result");
        if (result) {
            result.replaceChildren(element("p", "json-error", `${error.status ? `HTTP ${error.status} · ` : ""}${error.message}`));
            if (error.payload !== undefined) {
                const details = element("div");
                result.append(details);
                ApiLabJson.render(details, error.payload, {label: "错误响应 JSON"});
            }
        }
    }
    async function run(id = selected, {showResult = true} = {}) {
        const endpoint = endpoints.get(id);
        const key = endpoint.draftKey || id;
        const revision = context.revision;
        let locked = false;
        let admitted = false;
        let sameIdentity = true;
        let progressBatch;
        try {
            if (active.has(key) || (active.size && id !== "chat.messages.custom")) throw new Error("请等待当前请求完成；Chat 生成期间仍可插入医生卡片");
            const snapshot = context.get();
            const parameters = draft(id).parameters(snapshot);
            const outgoing = core.buildRequest(endpoint, parameters);
            if (endpoint.confirm && !window.confirm(`${endpoint.confirm}\n\n${core.format(parameters)}`)) return null;
            const meta = {id, endpoint, parameters, snapshot, revision};
            sameIdentity = ["userId", "sessionId"].every((name) => parameters[name] === undefined || parameters[name] === snapshot[name]);
            active.add(key); locked = true; admitted = true;
            if (sameIdentity) config.onStart?.(meta);
            let payload;
            if (endpoint.stream) {
                let last = null;
                let businessResult = null;
                let eventCount = 0;
                const progress = element("p", "hint", "正在接收流式响应…已收到 0 个事件");
                const target = $("#api-result");
                if (target && showResult) target.replaceChildren(progress);
                progressBatch = batch(() => {
                    if (revision === context.revision) progress.textContent = `正在接收流式响应…已收到 ${eventCount} 个事件`;
                }, 200);
                progressBatches.add(progressBatch);
                const count = await stream(outgoing.path, {...outgoing, snapshot, revision, onEvent(event) {
                    eventCount++;
                    last = event;
                    if (endpoint.resultStatuses?.includes((event?.data || event)?.status)) businessResult = event;
                    if (sameIdentity) config.onEvent?.(event, meta);
                    if (showResult) progressBatch.schedule();
                }});
                const result = businessResult || last;
                payload = {data: result?.data || result, eventCount: count};
            } else payload = await request(outgoing.path, {...outgoing, snapshot, revision, timeoutMs: endpoint.timeoutMs});
            guard(revision);
            const target = $("#api-result"); if (target && showResult) ApiLabJson.render(target, payload, {label: "接口结果 JSON", stateKey: id});
            if (showResult) config.onResult?.({payload});
            active.delete(key); locked = false;
            if (sameIdentity) await config.onResponse?.(payload, meta);
            guard(revision);
            return payload;
        } catch (error) {
            if (revision !== context.revision && !error.stale) error = new StaleResponseError();
            showError(error, showResult);
            if (showResult && !error.stale) config.onResult?.({error});
            if (!error.stale && sameIdentity && admitted) config.onError?.(error, {id});
            return null;
        } finally {
            progressBatch?.cancel(); progressBatches.delete(progressBatch);
            if (locked) active.delete(key);
            updateBusy(); logBatch.flush();
        }
    }
    function bindInput(selector, id, name) {
        const input = $(selector); if (!input) return;
        const sync = (force = false) => { if (force || document.activeElement !== input) input.value = draft(id).values[name] || ""; };
        listeners.add(sync);
        input.addEventListener("input", () => setParams(id, {[name]: input.value}, {raw: true}));
        sync();
    }
    async function checkHealth() {
        const badge = $("#health-status"); if (badge) badge.textContent = "连接中";
        try {
            await request("/health", {method: "GET"});
            if (badge) { badge.className = "status ok"; badge.textContent = "后端可用"; }
        } catch (error) {
            if (!error.stale && badge) { badge.className = "status bad"; badge.textContent = error.message; }
            showError(error);
        }
    }
    function mountEditor(editor) {
        function node(tag, id, className, text) {
            const result = element(tag, className, text);
            if (id) result.id = id;
            return result;
        }
        function button(id, text, className = "button ghost") {
            const result = node("button", id, className, text); result.type = "button"; return result;
        }
        function field(id, title, className) {
            const box = element("div", `field ${className}`);
            const label = element("label", "", title); label.htmlFor = id;
            const select = node("select", id); box.append(label, select); return box;
        }
        const head = element("div", "panel-head");
        head.append(element("h2", "", "请求参数与 JSON"), node("span", "api-source-badge", "status"));
        const body = element("div", "panel-body");
        const toolbar = element("div", "request-toolbar");
        const selector = field("api-select", "选择接口", "request-select-field");
        const mode = field("api-mode", "编辑方式", "request-mode-field");
        const modeSelect = mode.children[1];
        modeSelect.append(new Option("参数表单（实时同步）", "form"), new Option("完整 JSON（手工编辑）", "json"));
        toolbar.append(selector, mode);
        const tools = element("div", "actions editor-tools");
        tools.append(button("api-rebuild", "从表单重新生成 JSON"));
        const grid = element("div", "request-editor-grid");
        const form = element("div", "request-form-pane");
        const controls = node("fieldset", "api-form-controls", "request-form-controls");
        controls.setAttribute("aria-labelledby", "api-form-title");
        controls.append(node("div", "api-form-extras"), node("div", "api-fields", "request-fields"));
        const formHead = element("div", "request-form-head");
        formHead.append(node("h3", "api-form-title", "request-pane-title", "可视化参数表单"),
            button("api-reset-form", "重置参数"));
        form.append(formHead, controls);
        const jsonPane = element("div", "request-json-pane");
        const label = node("label", "api-json-label"); label.htmlFor = "api-json";
        const json = node("textarea", "api-json", "json-editor");
        json.spellcheck = false; json.setAttribute("aria-describedby", "api-json-status");
        const status = node("p", "api-json-status", "hint"); status.setAttribute("role", "status");
        const feedback = node("p", "api-json-feedback", "hint"); feedback.setAttribute("role", "status");
        const actions = element("div", "actions request-json-actions");
        actions.append(button("api-format-json", "格式化 JSON"), button("api-copy-json", "复制 JSON"), button("api-send", "发送当前接口", "button"));
        jsonPane.append(element("h3", "request-pane-title", "实际请求"), node("pre", "api-preview", "request-preview"),
            node("p", "api-request-type", "hint"), label, json, status, actions, feedback);
        grid.append(form, jsonPane);
        body.append(toolbar, tools, node("p", "api-hint", "hint"), grid);
        editor.replaceChildren(head, body);
        $(".lab-workspace")?.prepend(editor);
    }
    function mountDirectory() {
        const shell = $(".lab-shell");
        const settings = $("#lab-context");
        if (!shell || !settings) return;
        const directory = element("nav", "lab-directory");
        directory.setAttribute("aria-label", "本页目录");
        directory.append(element("span", "lab-directory-title", "本页目录"));
        const links = new Map();
        const visibility = new MutationObserver((entries) => {
            for (const {target} of entries) links.get(target).hidden = target.hidden;
        });
        for (const target of shell.querySelectorAll("[data-lab-section]")) {
            const link = element("a", "", target.dataset.labSection);
            link.href = "#" + target.id;
            link.hidden = target.hidden;
            target.tabIndex = -1;
            link.addEventListener("click", () => {
                if (target.tagName === "DETAILS") target.open = true;
            });
            if (target.tagName === "DETAILS" && location.hash === link.getAttribute("href")) target.open = true;
            links.set(target, link);
            visibility.observe(target, {attributeFilter: ["hidden"]});
            directory.append(link);
        }
        settings.before(directory);
    }
    function initialize(options = {}) {
        if (mounted) return;
        mounted = true; config = options;
        const query = new URLSearchParams(location.search);
        if (options.urlIdentity && query.has("userId")) context.update({userId: query.get("userId")});
        const host = $("#lab-context");
        if (host) {
            host.innerHTML = '<h2 class="lab-context-title">连接与身份</h2><div class="form-grid context-fields"></div><div class="actions"><button id="lab-new-user" class="button ghost">生成新用户</button><button id="lab-new-session" class="button ghost">生成新会话</button><button id="check-health" class="button secondary">检查连接</button><span id="health-status" class="status">尚未检查</span><span id="request-status" class="status">就绪</span></div><p class="hint context-note"></p>';
            for (const [name, id, title] of [["backend", "backend", "真实后端地址"], ["userId", "user-id", "公共 userId"], ["sessionId", "session-id", "公共 sessionId"]]) {
                const field = element("div", "field");
                const label = element("label", "", title); label.htmlFor = id;
                const input = element("input"); input.id = id; input.value = context.get()[name]; input.autocomplete = "off";
                input.addEventListener("input", () => context.update({[name]: input.value}));
                field.append(label, input); host.querySelector(".context-fields").append(field);
            }
            host.querySelector(".context-note").textContent = context.persistent
                ? "同源页面共享以上设置，刷新后保留。新会话保留用户；新用户同时生成新会话。业务参数仅留在当前页面。"
                : "浏览器不允许保存设置，当前页仍可调用接口。请通过 HTTP 页面服务访问并允许本地存储。";
            $("#lab-new-user").addEventListener("click", () => context.newUser());
            $("#lab-new-session").addEventListener("click", () => context.newSession());
            $("#check-health").addEventListener("click", checkHealth);
        }
        const editor = $("#api-editor");
        if (editor) {
            mountEditor(editor);
            for (const [id, selector] of Object.entries(options.formPanels || {})) {
                const node = $(selector);
                if (node) { formPanels.push({id, node}); $("#api-form-extras").append(node); }
            }
            const available = options.endpoints ? options.endpoints.map((id) => endpoints.get(id)) : ApiLabContracts.filter((item) => item.group === options.group);
            available.forEach((item) => { draft(item.id); $("#api-select").append(new Option(`${item.title} · ${item.method} ${item.path}`, item.id)); });
            $("#api-select").addEventListener("change", (event) => select(event.target.value));
            $("#api-mode").addEventListener("change", (event) => {
                try { draft(selected).setMode(event.target.value, context.get()); notifyDrafts(); renderFields(); } catch (error) { showError(error); event.target.value = draft(selected).mode; }
            });
            $("#api-json").addEventListener("input", (event) => {
                const model = draft(selected);
                model.json = event.target.value; model.mode = "json";
                notifyDrafts(); syncEditorMode(); preview();
            });
            $("#api-format-json").addEventListener("click", () => {
                try {
                    const model = draft(selected);
                    if (model.mode === "json") model.json = core.format(model.parameters(context.get(), false));
                    notifyDrafts(); preview();
                    $("#api-json-feedback").textContent = "已按 4 个空格缩进";
                } catch (error) { $("#api-json-feedback").textContent = error.message; }
            });
            $("#api-copy-json").addEventListener("click", async () => {
                $("#api-json-feedback").textContent = await ApiLabJson.copyText(document, $("#api-json").value)
                    ? "已复制请求 JSON" : "复制失败，请在 JSON 输入框中手动复制";
            });
            $("#api-rebuild").addEventListener("click", () => {
                try { draft(selected).regenerate(context.get()); draft(selected).setMode("json", context.get()); notifyDrafts(); renderFields(); } catch (error) { showError(error); }
            });
            $("#api-reset-form").addEventListener("click", () => { draft(selected).resetForm(); notifyDrafts(); renderFields(); });
            $("#api-send").addEventListener("click", () => run());
            select(options.initialEndpoint || available[0].id);
        }
        mountDirectory();
        context.subscribe(() => {
            progressBatches.forEach((pending) => pending.cancel());
            for (const [name, id] of [["backend", "backend"], ["userId", "user-id"], ["sessionId", "session-id"]]) {
                const input = $(`#${id}`);
                const value = context.get()[name];
                if (input && (document.activeElement !== input || input.value.trim() !== value)) input.value = value;
            }
            drafts.forEach((model) => model.resetForm());
            notifyDrafts(true);
            const result = $("#api-result"); if (result) result.textContent = "公共设置已改变，请重新发送请求。";
            const badge = $("#health-status"); if (badge) { badge.className = "status"; badge.textContent = "尚未检查"; }
            config.onReset?.(); renderFields();
        });
        window.addEventListener("storage", (event) => { if (event.key === core.STORAGE_KEY) context.receive(event.newValue); });
        window.addEventListener("pageshow", () => { try { context.receive(storage.getItem(core.STORAGE_KEY)); } catch { /* Storage is optional. */ } });
        $("#clear-log")?.addEventListener("click", clearLog);
        const logHead = $("#clear-log")?.parentElement;
        if (logHead) {
            const button = element("button", "button ghost", "导出完整记录");
            button.id = "export-log"; button.type = "button";
            button.addEventListener("click", exportLog); logHead.append(button);
            const summary = element("p", "log-summary"); summary.id = "log-summary";
            logHead.parentElement.insertBefore(summary, $("#raw-log"));
            renderLog();
        }
    }
    return {initialize, run, request, stream, select, setParams, subscribeDrafts, bindInput, draft, context, showError, log, element, batch,
        format: core.format, renderJson: ApiLabJson.render, randomId: core.randomId};
})();
