/* Pure request/state logic, shared by the browser and Node's offline tests. */
const ApiLabCore = (() => {
    const STORAGE_KEY = "shulan-api-context-v1";
    const LEGACY_KEY = "shulan-api-backend";
    const DEFAULT_BACKEND = "http://127.0.0.1:9002";
    const identityNames = new Set(["userId", "sessionId"]);
    const normalizeBackend = (value) => String(value ?? "").trim().replace(/\/+$/, "");
    const format = (value) => JSON.stringify(value, null, 4);

    function randomId(prefix) {
        const value = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
        return `${prefix}-${value.slice(0, 12)}`;
    }

    function parseContext(raw) {
        try {
            const value = JSON.parse(raw);
            if (value && ["backend", "userId", "sessionId"].every((key) => typeof value[key] === "string")) {
                return {backend: normalizeBackend(value.backend), userId: value.userId, sessionId: value.sessionId};
            }
        } catch { /* A corrupt cache is treated as a first visit. */ }
        return null;
    }

    function createContextStore(storage, makeId = randomId) {
        let persistent = true;
        function read(key) {
            try { return storage.getItem(key); } catch { persistent = false; return null; }
        }
        let value = parseContext(read(STORAGE_KEY)) || {
            backend: normalizeBackend(read(LEGACY_KEY)) || DEFAULT_BACKEND,
            userId: makeId("test-user"), sessionId: makeId("test-session"),
        };
        let revision = 0;
        const listeners = new Set();
        function save() {
            try { storage.setItem(STORAGE_KEY, JSON.stringify(value)); } catch { persistent = false; }
        }
        save();
        function update(patch, persist = true) {
            const next = {...value};
            for (const key of ["backend", "userId", "sessionId"]) {
                if (typeof patch[key] === "string") {
                    next[key] = key === "backend" ? normalizeBackend(patch[key]) : patch[key].trim();
                }
            }
            if (JSON.stringify(next) === JSON.stringify(value)) return;
            const previous = value;
            value = next;
            revision += 1;
            if (persist) save();
            listeners.forEach((listener) => listener({...value}, {...previous}));
        }
        return {
            get: () => ({...value}),
            get revision() { return revision; },
            get persistent() { return persistent; },
            update,
            subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
            receive(raw) { const next = parseContext(raw); if (next) update(next, false); },
            newSession() { update({sessionId: makeId("test-session")}); },
            newUser() { update({userId: makeId("test-user"), sessionId: makeId("test-session")}); },
        };
    }

    function parseFileUrls(field, text, fileType) {
        const lines = text.split(/\r\n?|\n/).map((line) => line.trim()).filter(Boolean);
        if (lines.length > field.maxItems) throw new Error(`${field.name} 最多 ${field.maxItems} 份文件`);
        return lines.map((value, index) => {
            const label = `${field.name} 第 ${index + 1} 个 URL`;
            let url;
            try { url = new URL(value); } catch { throw new Error(`${label} 不完整，请填写 HTTPS 文件地址`); }
            if (url.protocol !== "https:" || !url.hostname || /\s/.test(value)) {
                throw new Error(`${label} 必须为完整的 HTTPS 文件地址`);
            }
            if (url.username || url.password || url.hash) throw new Error(`${label} 不能包含账号、密码或 # 片段`);
            // Match the file service's filename precedence without rewriting signed download URLs.
            let name = ["objectName", "fileName", "filename", "name"].map((key) => url.searchParams.get(key)).find(Boolean)
                || url.pathname.split("/").pop();
            try { name = decodeURIComponent(name); } catch { /* Keep the original filename for manual type selection. */ }
            const extension = name?.includes(".") ? name.split(".").pop().toLowerCase() : "";
            const inferred = extension === "pdf" ? "pdf" : ["jpg", "jpeg", "png", "bmp", "gif"].includes(extension) ? "image" : "";
            const type = fileType || inferred;
            if (!type) throw new Error(`${label} 无法识别文件类型，请在“URL 文件类型”中选择 PDF 或图片，或填写 JSON 数组`);
            return {url: value, fileType: type};
        });
    }

    function parseField(field, raw, urlFileType) {
        if (raw === "" || raw === undefined) return undefined;
        const text = String(raw).trim();
        if (!text) return undefined;
        if (field.urlInput && /^[a-z][a-z\d+.-]*:/i.test(text)) return parseFileUrls(field, text, urlFileType);
        if (field.type === "boolean") {
            if (text !== "true" && text !== "false") throw new Error(`${field.name} 必须为 true 或 false`);
            return text === "true";
        }
        if (field.type === "integer" || field.type === "number") {
            const value = Number(text);
            if (!Number.isFinite(value) || (field.type === "integer" && !Number.isInteger(value))) {
                throw new Error(`${field.name} 必须为${field.type === "integer" ? "整数" : "数字"}`);
            }
            return value;
        }
        if (["json", "object", "array"].includes(field.type)) {
            let value;
            try { value = JSON.parse(text); } catch (error) { throw new Error(`${field.name} JSON 格式错误：${error.message}`); }
            if (value !== null && field.type === "array" && !Array.isArray(value)) throw new Error(`${field.name} 必须为 JSON 数组`);
            if (value !== null && field.type === "object" && (typeof value !== "object" || Array.isArray(value))) {
                throw new Error(`${field.name} 必须为 JSON 对象`);
            }
            return value;
        }
        return text;
    }

    class RequestDraft {
        constructor(endpoint, makeId = randomId) {
            this.endpoint = endpoint;
            this.makeId = makeId;
            this.mode = "form";
            this.json = null;
            this.resetForm();
        }
        resetForm() {
            this.values = Object.create(null);
            this.urlFileTypes = Object.create(null);
            this.omitted = new Set();
            this.touched = new Set();
            for (const field of this.endpoint.fields) {
                this.values[field.name] = field.generate ? this.makeId(field.generate) : String(field.preset ?? "");
            }
        }
        set(name, value, {raw = false, onlyDefault = false, touch = true} = {}) {
            const field = this.endpoint.fields.find((item) => item.name === name);
            if (!field || identityNames.has(name) || (onlyDefault && this.touched.has(name))) return;
            this.values[name] = value === undefined ? "" : raw ? String(value)
                : ["json", "object", "array"].includes(field.type) ? format(value) : String(value ?? "");
            if (!onlyDefault && touch) this.touched.add(name);
        }
        form(context, validate = true) {
            const result = Object.create(null);
            for (const field of this.endpoint.fields) {
                const raw = identityNames.has(field.name) ? context[field.name] : this.values[field.name];
                const value = this.omitted.has(field.name) ? undefined : parseField(field, raw, this.urlFileTypes[field.name]);
                if (value !== undefined) result[field.name] = value;
                else if (validate && field.required) throw new Error(`请填写 ${field.name}`);
            }
            if (validate) {
                for (const names of this.endpoint.exactlyOne || []) {
                    if (names.filter((name) => result[name] != null && result[name] !== "").length !== 1) {
                        throw new Error(`${names.join(" 与 ")} 必须且只能提供一个`);
                    }
                }
                for (const names of this.endpoint.atLeastOne || []) {
                    if (!names.some((name) => result[name] != null)) throw new Error(`${names.join(" / ")} 至少填写一个`);
                }
                for (const [name, dependency] of this.endpoint.requires || []) {
                    if (result[name] != null && result[dependency] == null) throw new Error(`${name} 需要同时填写 ${dependency}`);
                }
                for (const rule of this.endpoint.when || []) {
                    if (result[rule.field] === rule.value && rule.required.some((name) => result[name] == null)) {
                        throw new Error(`${rule.field}=${rule.value} 时请填写 ${rule.required.join("、")}`);
                    }
                }
            }
            return result;
        }
        regenerate(context) { this.json = format(this.form(context, false)); }
        setMode(mode, context) {
            if (mode === "json" && this.json === null) this.regenerate(context);
            this.mode = mode;
        }
        parameters(context, validate = true) {
            if (this.mode === "form") return this.form(context, validate);
            let value;
            try { value = JSON.parse(this.json); } catch (error) { throw new Error(`请求 JSON 格式错误：${error.message}`); }
            if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("请求 JSON 必须为对象");
            return value;
        }
    }

    function buildRequest(endpoint, parameters) {
        let path = endpoint.path;
        const body = {...parameters};
        const query = new URLSearchParams();
        for (const field of endpoint.fields) {
            if (field.location === "path") {
                const value = body[field.name];
                if (typeof value !== "string" || !value.trim()) throw new Error(`请填写路径参数 ${field.name}`);
                path = path.replace(`{${field.name}}`, encodeURIComponent(value));
                delete body[field.name];
            } else if (field.location === "query" && Object.hasOwn(body, field.name)) {
                const value = body[field.name];
                query.set(field.name, typeof value === "object" ? JSON.stringify(value) : String(value));
                delete body[field.name];
            }
        }
        if (endpoint.method === "GET") {
            for (const [name, value] of Object.entries(body)) {
                query.set(name, typeof value === "object" ? JSON.stringify(value) : String(value));
            }
        }
        if (query.size) path += `${path.includes("?") ? "&" : "?"}${query}`;
        return endpoint.method === "GET" ? {method: "GET", path} : {method: endpoint.method, path, body};
    }
    function reportProgress(data) {
        const generation = {queued: "等待生成", running: "生成中", success: "成功", retried_success: "成功",
            partial_success: "部分成功", failed: "失败", fallback_empty: "失败", unknown: "历史状态未知"};
        const delivery = {pending: "等待投递", sending: "投递中", retry_wait: "等待重试", delivered: "已确认接收", unknown: "暂无投递记录"};
        const rows = data.reportType ? [data] : data.artifacts || [];
        return rows.map((row) => {
            const sent = row.delivery || {};
            const state = row.generationStatus || data.generationStatus || "unknown";
            return `${row.reportType} · 生成：${generation[state] || state} · 回调：${delivery[sent.status] || delivery.unknown}`
                + ` · 尝试次数：${sent.attempts || 0}`
                + (sent.nextRetryAt ? ` · 下次重试：${sent.nextRetryAt}` : "")
                + (sent.lastError?.code ? ` · 最近投递错误：${sent.lastError.code}` : "");
        });
    }
    return {STORAGE_KEY, DEFAULT_BACKEND, identityNames, normalizeBackend, format, randomId, createContextStore, RequestDraft, buildRequest, reportProgress};
})();
if (typeof module !== "undefined") module.exports = ApiLabCore;
