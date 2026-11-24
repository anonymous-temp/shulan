/* Run with: node --test tests/unit/api/api_lab.test.cjs
 * No real backend, browser dependency, .env, or external service is used.
 */
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "../../..");
const pages = path.join(root, "tests/api/pages");
const core = require(path.join(pages, "lab-core.js"));
const jsonViewer = require(path.join(pages, "json-viewer.js"));
const contracts = require(path.join(pages, "api-contracts.js"));
const byId = (id) => contracts.find((item) => item.id === id);
test("report progress distinguishes failed generation from confirmed delivery", () => {
    const lines = core.reportProgress({reportType: "instruction", generationStatus: "failed",
        delivery: {status: "delivered", attempts: 7}});
    assert.match(lines[0], /生成：失败/);
    assert.match(lines[0], /回调：已确认接收/);
    assert.match(lines[0], /尝试次数：7/);
});
test("report progress exposes pending retries and leaves historical delivery unknown", () => {
    const lines = core.reportProgress({generationStatus: "success", artifacts: [
        {reportType: "evaluate", delivery: {status: "retry_wait", attempts: 3,
            nextRetryAt: "2026-09-14T10:00:00+00:00", lastError: {code: "HTTP_503"}}},
        {reportType: "opinion", generationStatus: "success"},
    ]});
    assert.match(lines[0], /等待重试.*尝试次数：3.*下次重试：2026-09-14.*HTTP_503/);
    assert.match(lines[1], /回调：暂无投递记录/);
});
const ids = {backend: "http://127.0.0.1:9002", userId: "developer-001", sessionId: "session-001"};
const plain = (value) => JSON.parse(JSON.stringify(value));
function memoryStorage(initial = {}) {
    const data = new Map(Object.entries(initial));
    return {data, getItem: (key) => data.get(key) ?? null, setItem: (key, value) => data.set(key, value)};
}
function makeIds() { let i = 0; return (prefix) => `${prefix}-${++i}`; }
function draft(id) { return new core.RequestDraft(byId(id), makeIds()); }
function deferred() {
    let resolve;
    const promise = new Promise((done) => { resolve = done; });
    return {promise, resolve};
}
// A small DOM test double for text rendering and events; layout is checked in a browser.
function domDocument() {
    const doc = {copied: null, execCommand: () => true, defaultView: {navigator: {clipboard: {async writeText(value) { doc.copied = value; }}}}};
    class Node {
        constructor(tagName, text = "") {
            this.tagName = tagName; this.ownerDocument = doc; this.children = []; this.events = {};
            this.className = ""; this.attributes = {}; this._text = text; this.value = "";
            this.style = {setProperty() {}};
            this.dataset = {};
            this.classList = {
                add: (...names) => { this.className = [...new Set([...this.className.split(" ").filter(Boolean), ...names])].join(" "); },
                remove: (...names) => { this.className = this.className.split(" ").filter((name) => !names.includes(name)).join(" "); },
                toggle: (name, force) => { if (force) this.classList.add(name); else this.classList.remove(name); },
            };
        }
        get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
        get data() { return this._text; }
        set data(value) { this._text = String(value); }
        appendData(value) { this._text += value; this.appendCount = (this.appendCount || 0) + 1; }
        get lastElementChild() { return this.children.at(-1); }
        querySelector(selector) { return descendants(this, selector.startsWith(".") ? hasClass(selector.slice(1)) : (node) => `#${node.id}` === selector)[0] || null; }
        querySelectorAll(selector) { return descendants(this, (node) => node.tagName === selector); }
        get options() { return this.children; }
        set textContent(value) { this.children = []; this._text = String(value); }
        append(...children) {
            children.forEach((child) => { child.parentElement = this; this.children.push(child); });
        }
        replaceChildren(...children) { this._text = ""; this.children = []; this.append(...children); }
        prepend(...children) { this.children.unshift(...children); children.forEach((child) => { child.parentElement = this; }); }
        insertBefore(child, next) { this.children.splice(this.children.indexOf(next), 0, child); child.parentElement = this; }
        remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((child) => child !== this); }
        click() { doc.clicked = this; return this.emit("click"); }
        addEventListener(type, listener) { this.events[type] = listener; }
        setAttribute(name, value) { this.attributes[name] = value; }
        focus() { doc.activeElement = this; }
        select() { doc.selectedElement = this; }
        setSelectionRange() {}
        async emit(type, event = {}) { return this.events[type]?.({target: this, ...event}); }
    }
    doc.createElement = (name) => new Node(name);
    doc.createTextNode = (text) => new Node("#text", text);
    doc.body = new Node("body");
    return doc;
}
function descendants(node, match) {
    return [node, ...(node.children || []).flatMap((child) => descendants(child, () => true))].filter(match);
}
const hasClass = (name) => (node) => node.className.split(" ").includes(name);
const control = (host, label) => descendants(host, (node) => node.tagName === "button" && node.textContent === label)[0];
function browserHarness(fetch, config = {}, pageScript = null, mountEditor = false) {
    const document = domDocument();
    const nodes = Object.fromEntries(["#raw-log", "#request-status", "#api-result"].map((name) => [name, document.createElement("div")]));
    nodes["#clear-log"] = document.createElement("button");
    const logPanel = document.createElement("aside");
    const logHead = document.createElement("div");
    logHead.append(nodes["#clear-log"]); logPanel.append(logHead, nodes["#raw-log"]);
    nodes["#log-panel"] = logPanel;
    document.querySelector = (selector) => nodes[selector]
        || Object.values(nodes).flatMap((node) => descendants(node, (child) => `#${child.id}` === selector))[0] || null;
    document.querySelectorAll = (selector) => selector === "[data-action]"
        ? Object.values(nodes).filter((node) => node.dataset?.action)
        : selector === "#analysis-examples button" ? nodes["#analysis-examples"]?.children || [] : [];
    if (mountEditor) nodes["#api-editor"] = document.createElement("section");
    if (pageScript === "interpret.js") nodes["#interpret-preview"] = document.createElement("div");
    if (pageScript === "health-analysis.js") {
        for (const name of ["analysis-preview", "analysis-state", "analysis-examples"]) nodes[`#${name}`] = document.createElement("div");
        for (const scenario of ["empty", "history", "answer", "report", "chat"]) {
            const button = document.createElement("button"); button.dataset.scenario = scenario;
            nodes["#analysis-examples"].append(button);
        }
    }
    if (pageScript === "health-profile-view.js") {
        for (const name of ["subject-picker", "profile-stage", "profile-placeholder", "profile-dashboard",
            "subject-avatar", "profile-title", "subject-kind", "profile-user-id", "profile-subject-id", "profile-version",
            "profile-updated-at", "profile-meta", "indicator-groups", "indicator-count", "fact-groups", "fact-count",
            "evidence-list", "evidence-status", "alert-list", "alert-count", "summary-content", "pending-card",
            "pending-count", "load-profile", "clear-profile"]) nodes[`#${name}`] = document.createElement("div");
    }
    if (pageScript === "chat.js") {
        for (const name of ["conversation", "suggestion-list", "message", "edit-custom"]) nodes[`#${name}`] = document.createElement("div");
        Object.assign(nodes["#conversation"], {scrollHeight: 100, scrollTop: 0, clientHeight: 100});
    }
    if (pageScript === "report.js") {
        for (const name of ["report-list", "next-reports", "report-progress", "mock-callback-url", "copy-callback-url", "callback-url-status"]) {
            nodes[`#${name}`] = document.createElement("div");
        }
    }
    if (["record.js", "record-preview.js"].includes(pageScript)) {
        for (const name of ["question-area", "submit", "submit-stream", "no-more", "other-answer"]) nodes[`#${name}`] = document.createElement("div");
        for (const name of ["source", "value", "type", "add", "list", "value-label", "type-field", "hint", "mode", "count", "message", "edit"]) {
            const node = document.createElement("div"); node.value = ""; nodes[`#${pageScript === "record.js" ? "record" : "questionnaire"}-file-${name}`] = node;
        }
        nodes[`#${pageScript === "record.js" ? "record" : "questionnaire"}-file-source`].value = "url";
        if (pageScript === "record-preview.js") {
            for (const name of ["generate", "restore", "submit", "chat", "status", "mode", "downloads", "risk", "client", "interview", "reports", "form", "factors"]) {
                nodes[`#questionnaire-${name}`] = document.createElement("div");
            }
            nodes["#questionnaire-chat"].href = "./chat.html";
        }
        const html = fs.readFileSync(path.join(pages, pageScript === "record.js" ? "record.html" : "record-preview.html"), "utf8");
        for (const [, attributes, label] of html.matchAll(/<button\b([^>]*)>([^<]*)<\/button>/g)) {
            const action = attributes.match(/data-action="([^"]+)"/)?.[1];
            if (!action) continue;
            const id = attributes.match(/\bid="([^"]+)"/)?.[1] || `action-${action}`;
            const button = nodes[`#${id}`] || document.createElement("button");
            button.dataset.action = action; button.textContent = label;
            nodes[`#${id}`] = button;
        }
    }
    const events = {};
    const timers = new Map();
    let nextTimer = 0;
    const sandbox = vm.createContext({
        ApiLabCore: core, ApiLabContracts: contracts, ApiLabJson: jsonViewer, URL, URLSearchParams, TextDecoder, performance, fetch, Blob, AbortController,
        Option: function(text, value) { const option = document.createElement("option"); option.textContent = text; option.value = value; return option; },
        document,
        setTimeout(callback, delay) {
            // Cooperative parser yields must run without manually advancing UI timers.
            if (delay === 0) return setTimeout(callback, 0);
            timers.set(++nextTimer, {callback, delay}); return nextTimer;
        },
        clearTimeout(id) { if (typeof id === "object") clearTimeout(id); else timers.delete(id); },
        window: {localStorage: memoryStorage({[core.STORAGE_KEY]: JSON.stringify(ids)}),
            addEventListener: (name, handler) => { events[name] = handler; }, confirm: () => true},
        location: {protocol: "http:", origin: "http://127.0.0.1:9080", search: ""},
    });
    vm.runInContext(fs.readFileSync(path.join(pages, "common.js"), "utf8"), sandbox);
    const app = vm.runInContext("ApiLab", sandbox);
    if (["record.js", "record-preview.js"].includes(pageScript)) vm.runInContext(fs.readFileSync(path.join(pages, "record-files.js"), "utf8"), sandbox);
    if (pageScript === "record-preview.js") vm.runInContext(fs.readFileSync(path.join(pages, "record-factors.js"), "utf8"), sandbox);
    if (pageScript) vm.runInContext(fs.readFileSync(path.join(pages, pageScript), "utf8"), sandbox);
    else app.initialize(config);
    return {app, timers, nodes: new Proxy(nodes, {get: (target, key) => target[key] || (typeof key === "string" ? document.querySelector(key) : undefined)}), events, document: sandbox.document};
}

function tickViews(timers, delay) {
    for (const [id, timer] of [...timers]) {
        if (delay !== undefined && timer.delay !== delay) continue;
        timers.delete(id); timer.callback();
    }
}
async function exportedEntries(nodes, document) {
    await control(nodes["#log-panel"], "导出完整记录").emit("click");
    const text = await (await fetch(document.clicked.href)).text();
    URL.revokeObjectURL(document.clicked.href);
    return text.trim() ? text.trim().split("\n").map(JSON.parse) : [];
}
function controlledStream() {
    let controller;
    const response = new Response(new ReadableStream({start(value) { controller = value; }}));
    return {response, send(...events) { controller.enqueue(new TextEncoder().encode(events.map((data) => `data: ${JSON.stringify({data})}\n\n`).join(""))); }, close() { controller.close(); }};
}
async function until(condition) {
    for (let i = 0; i < 100; i++) { if (condition()) return; await new Promise((resolve) => setTimeout(resolve, 2)); }
    assert.fail("stream did not reach expected state");
}

test("stream stress keeps 10000 ordered events, bounded logs, complete exports and one final JSON tree", async () => {
    const content = "健康建议".repeat(10);
    const frames = Array.from({length: 10000}, (_, index) => ({data: {type: index % 2 ? "content" : "reasoning", content, index}}));
    const {app, nodes, document} = browserHarness(async () => new Response(frames.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("")), {}, "chat.js");
    let resultMounts = 0;
    const mount = nodes["#api-result"].replaceChildren.bind(nodes["#api-result"]);
    nodes["#api-result"].replaceChildren = (...children) => { resultMounts++; mount(...children); };
    app.setParams("chat.send.stream", {message: "stress"});
    let yielded = false;
    setTimeout(() => { yielded = true; }, 0);
    const result = await app.run("chat.send.stream");
    assert.equal(yielded, true);
    assert.equal(result.eventCount, 10000);
    assert.equal(resultMounts, 2); // Initial status, then the final JSON viewer.
    const reply = descendants(nodes["#conversation"], hasClass("message-content")).at(-1);
    const reasoning = descendants(nodes["#conversation"], hasClass("message-reasoning-text"))[0];
    assert.equal(reply.textContent, content.repeat(5000));
    assert.equal(reasoning.textContent, content.repeat(5000));
    assert.equal(reply.children[0].appendCount, 1); // EOF flush without advancing UI timers.
    assert.ok(nodes["#raw-log"].children.length <= 200);
    assert.ok(nodes["#raw-log"].textContent.length <= 256 * 1024);
    const archive = await exportedEntries(nodes, document);
    const events = archive.filter((entry) => entry.eventSequence !== null);
    assert.equal(events.length, 10000);
    assert.deepEqual(events.map((entry) => entry.payload), frames);
    assert.deepEqual(events.map((entry) => entry.eventSequence), Array.from({length: 10000}, (_, index) => index + 1));
    assert.equal(new Set(archive.map((entry) => entry.requestId)).size, 1);
    assert.ok(archive.every((entry) => !Number.isNaN(Date.parse(entry.time))));
});

test("log previews are batched and truncated while downloads stay complete; clear discards pending history", async () => {
    const {app, nodes, timers, document} = browserHarness(async () => new Response("{}"));
    const huge = "测".repeat(50000);
    for (let i = 0; i < 300; i++) app.log(`entry ${i}`, {huge, i});
    assert.equal(nodes["#raw-log"].textContent, "");
    assert.equal(timers.size, 1);
    tickViews(timers, 200);
    assert.ok(nodes["#raw-log"].children.length < 200);
    assert.ok(nodes["#raw-log"].textContent.length <= 256 * 1024);
    assert.match(nodes["#raw-log"].textContent, /预览已截断/);
    assert.equal((await exportedEntries(nodes, document))[0].payload.huge, huge);
    app.log("pending before clear", huge);
    await nodes["#clear-log"].emit("click");
    tickViews(timers);
    assert.equal(nodes["#raw-log"].textContent, "");
    assert.deepEqual(await exportedEntries(nodes, document), []);
    app.log("after clear", "new"); tickViews(timers, 200);
    assert.equal((await exportedEntries(nodes, document)).length, 1);
    assert.doesNotMatch(nodes["#raw-log"].textContent, /pending before clear/);
});

test("Chat batches incremental text and scrolls only while following the bottom, including custom cards", async () => {
    const source = controlledStream();
    const {app, nodes, timers} = browserHarness(async (_url, options) => options.headers.Accept === "text/event-stream" ? source.response
        : new Response(JSON.stringify({data: {message: {content: "医生卡片", customType: "DOCTOR_MESSAGE_CARD"}}})), {}, "chat.js");
    app.setParams("chat.send.stream", {message: "hello"});
    const pending = app.run("chat.send.stream");
    source.send({type: "reasoning", content: "思考"}, {type: "content", content: "第一段"});
    await until(() => [...timers.values()].some((timer) => timer.delay === 50));
    const content = descendants(nodes["#conversation"], hasClass("message-content")).at(-1);
    assert.equal(content.textContent, "正在生成…");
    const textNode = content.children[0];
    assert.equal(await app.run("chat.send.stream"), null);
    assert.equal(content.textContent, "正在生成…"); // A rejected duplicate must not finalize the active stream.
    const conversation = nodes["#conversation"];
    let top = 0; let scrollWrites = 0;
    Object.defineProperty(conversation, "scrollTop", {get: () => top, set: (value) => { top = value; scrollWrites++; }});
    conversation.scrollHeight = 1000;
    tickViews(timers, 50);
    assert.equal(content.textContent, "第一段"); assert.equal(scrollWrites, 0);
    app.setParams("chat.messages.custom", {content: "医生卡片"});
    await app.run("chat.messages.custom");
    assert.equal(scrollWrites, 0);
    source.send({type: "content", content: "第二段"});
    await until(() => [...timers.values()].some((timer) => timer.delay === 50));
    top = 900;
    tickViews(timers, 50);
    await until(() => scrollWrites === 1);
    assert.equal(content.children[0], textNode);
    assert.equal(content.textContent, "第一段第二段"); assert.equal(scrollWrites, 1);
    assert.match(conversation.textContent, /医生卡片/);
    source.close(); await pending;
    assert.equal(content.textContent, "第一段第二段");
});

test("Chat flushes partial text on errors and gives empty streams a final message", async () => {
    for (const error of [false, true]) {
        const events = error ? [{type: "content", content: "保留正文"}, {type: "error", error: "中断"}] : [{type: "finish"}];
        const {app, nodes, timers} = browserHarness(async () => new Response(events.map((data) => `data: ${JSON.stringify({data})}\n\n`).join("")), {}, "chat.js");
        app.setParams("chat.send.stream", {message: "hello"});
        await app.run("chat.send.stream");
        tickViews(timers);
        assert.match(nodes["#conversation"].textContent, error ? /保留正文.*请求失败：中断/s : /没有收到正文内容/);
        assert.doesNotMatch(nodes["#conversation"].textContent, /正在生成/);
    }
});

test("Chat HTTP failures finish the placeholder and an active stream can clear and restart its log archive", async () => {
    const failed = browserHarness(async () => new Response("unavailable", {status: 503}), {}, "chat.js");
    failed.app.setParams("chat.send.stream", {message: "hello"});
    await failed.app.run("chat.send.stream");
    assert.doesNotMatch(failed.nodes["#conversation"].textContent, /正在生成/);
    assert.match(failed.nodes["#conversation"].textContent, /流已中断/);

    const source = controlledStream();
    const {app, nodes, timers, document} = browserHarness(async () => source.response, {}, "chat.js");
    app.setParams("chat.send.stream", {message: "hello"});
    const pending = app.run("chat.send.stream");
    source.send({type: "content", content: "清空前"});
    await until(() => [...timers.values()].some((timer) => timer.delay === 50));
    await nodes["#clear-log"].emit("click");
    source.send({type: "content", content: "清空后"}); source.close();
    await pending;
    const entries = await exportedEntries(nodes, document);
    const events = entries.filter((entry) => entry.eventSequence !== null);
    assert.equal(events.length, 1);
    assert.equal(events[0].eventSequence, 2);
    assert.equal(events[0].payload.data.content, "清空后");
    assert.doesNotMatch(nodes["#raw-log"].textContent, /清空前/);
    assert.match(nodes["#conversation"].textContent, /清空前清空后/);
});

test("switching identity cancels queued Chat and Record views without relying on another stream event", async () => {
    for (const pageScript of ["chat.js", "record.js"]) {
        const source = controlledStream();
        const {app, nodes, timers} = browserHarness(async () => source.response, {}, pageScript);
        const id = pageScript === "chat.js" ? "chat.send.stream" : "record.answer.stream";
        app.setParams(id, {message: "hello", questionId: "q-1", other: "answer"});
        const pending = app.run(id);
        source.send({type: "reasoning", content: "旧推理"});
        await until(() => [...timers.values()].some((timer) => timer.delay === 50));
        app.context.newSession();
        assert.equal([...timers.values()].some((timer) => timer.delay === 50), false);
        tickViews(timers);
        assert.doesNotMatch(nodes["#api-result"].textContent, /正在接收/);
        const area = nodes[pageScript === "chat.js" ? "#conversation" : "#question-area"];
        assert.doesNotMatch(area.textContent, /旧推理/);
        source.close(); assert.equal(await pending, null);
        assert.doesNotMatch(area.textContent, /旧推理/);
    }
});

test("report page copies the page service callback URL independently of the selected backend", async () => {
    const {app, nodes, document} = browserHarness(() => { throw new Error("unexpected fetch"); }, {}, "report.js");
    assert.equal(nodes["#mock-callback-url"].value, "http://127.0.0.1:9080/callback");
    app.context.update({backend: "http://different-backend:9002"});
    await nodes["#copy-callback-url"].emit("click");
    assert.equal(document.copied, "http://127.0.0.1:9080/callback");
});

test("report callback address falls back to execCommand and then to manual copying", async () => {
    const {nodes, document} = browserHarness(() => { throw new Error("unexpected fetch"); }, {}, "report.js");
    for (const clipboard of [undefined, {async writeText() { throw new Error("permission denied"); }}]) {
        document.defaultView.navigator.clipboard = clipboard;
        await nodes["#copy-callback-url"].emit("click");
        assert.match(nodes["#callback-url-status"].textContent, /已复制/);
    }
    document.execCommand = () => false; // Both copy paths fail: select the field and ask for Ctrl+C.
    for (const clipboard of [undefined, {async writeText() { throw new Error("permission denied"); }}]) {
        document.defaultView.navigator.clipboard = clipboard;
        await nodes["#copy-callback-url"].emit("click");
        assert.equal(document.activeElement, nodes["#mock-callback-url"]);
        assert.equal(document.selectedElement, nodes["#mock-callback-url"]);
        assert.match(nodes["#callback-url-status"].textContent, /Ctrl\+C/);
    }
});

// The existing gut-barrier endpoint has never had an API Lab page. Keep this
// explicit scope exception; every other production route/method must be listed.
const nonLabRoutes = new Set(["/api/v1/gut-barrier/report"]);
const catalogPath = (route) => route.replace("([^/]+)", "{questionnaireId}");
function publicRoutes() {
    const source = fs.readFileSync(path.join(root, "web/routes.py"), "utf8");
    return [...source.matchAll(/tornado\.web\.URLSpec\(\s*r(['"])([^'"]+)\1,\s*(\w+)/g)]
        .map(([, , route, handler]) => ({path: catalogPath(route), handler}));
}
function publicHandlers() {
    const handlers = new Map();
    for (const file of fs.readdirSync(path.join(root, "web/handlers")).filter((name) => name.endsWith(".py"))) {
        const source = fs.readFileSync(path.join(root, "web/handlers", file), "utf8");
        for (const match of source.matchAll(/^class (\w+)\((\w+)\):/gm)) {
            handlers.set(match[1], {parent: match[2], block: source.slice(match.index).split(/\nclass /)[0]});
        }
    }
    return handlers;
}
test("catalog covers each managed production route and HTTP method exactly once", () => {
    const handlers = publicHandlers();
    function methods(name) {
        const handler = handlers.get(name);
        if (!handler) return [];
        return [...new Set([...handler.block.matchAll(/^    (?:async )?def (get|post)\(/gm)].map((match) => match[1].toUpperCase()).concat(methods(handler.parent)))];
    }
    const routes = publicRoutes();
    for (const route of nonLabRoutes) assert.ok(routes.some((item) => item.path === route));
    const expected = routes.filter((item) => !nonLabRoutes.has(item.path)).flatMap((item) => methods(item.handler).map((method) => `${method} ${item.path}`));
    const actual = contracts.map((item) => `${item.method} ${item.path}`);
    assert.deepEqual(actual.sort(), expected.sort());
    assert.equal(new Set(actual).size, contracts.length);
    assert.equal(new Set(contracts.map((item) => item.id)).size, contracts.length);
    for (const item of contracts) assert.equal(new Set(item.fields.map((field) => field.name)).size, item.fields.length);
});

test("pages load scripts in dependency order and every linked local asset exists", () => {
    for (const name of fs.readdirSync(pages).filter((name) => name.endsWith(".html"))) {
        const html = fs.readFileSync(path.join(pages, name), "utf8");
        for (const [, target] of html.matchAll(/(?:src|href)="\.\/([^"?#]+)"/g)) assert.ok(fs.existsSync(path.join(pages, target)), `${name}: ${target}`);
        assert.ok(html.indexOf('src="./lab-core.js"') < html.indexOf('src="./api-contracts.js"'));
        assert.ok(html.indexOf('src="./api-contracts.js"') < html.indexOf('src="./common.js"'));
        assert.ok(html.includes('src="./json-viewer.js"'));
        assert.ok(html.indexOf('src="./json-viewer.js"') < html.indexOf('src="./common.js"'));
        assert.ok(html.includes('id="lab-context"'));
        for (const [, script] of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) new vm.Script(script, {filename: name});
    }
    for (const name of fs.readdirSync(pages).filter((name) => name.endsWith(".js"))) new vm.Script(fs.readFileSync(path.join(pages, name), "utf8"), {filename: name});
});

test("JSON results highlight literal values safely and indent each level by four spaces", async () => {
    const doc = domDocument(); const host = doc.createElement("div");
    const value = {data: {"<img src=x onerror=alert(1)>": '<script>"&\n</script>', count: 0, enabled: false, missing: null, list: [], object: {}}};
    jsonViewer.render(host, value);
    const lines = descendants(host, hasClass("json-line"));
    assert.ok(lines.some((line) => line.textContent.startsWith('    "data": {')));
    assert.ok(lines.some((line) => line.textContent === '        "count": 0,\n'));
    assert.deepEqual(descendants(host, hasClass("json-string")).map((node) => node.textContent), [JSON.stringify(value.data[Object.keys(value.data)[0]])]);
    assert.equal(descendants(host, hasClass("json-number"))[0].textContent, "0");
    assert.equal(descendants(host, hasClass("json-boolean"))[0].textContent, "false");
    assert.equal(descendants(host, hasClass("json-null"))[0].textContent, "null");
    assert.equal(descendants(host, (node) => ["img", "script"].includes(node.tagName)).length, 0);
    await control(host, "折叠全部").emit("click");
    await control(host, "复制 JSON").emit("click");
    assert.equal(doc.copied, JSON.stringify(value, null, 4));
    assert.equal(doc.copied, core.format(value));
});

test("JSON folding loads nested branches, expands everything and retains toggles during SSE updates", async () => {
    const doc = domDocument(); const host = doc.createElement("div");
    const value = {data: {items: [{value: {nested: [1, 2, 3]}}]}};
    jsonViewer.render(host, value, {stateKey: "stream"});
    const initial = descendants(host, hasClass("json-node"));
    assert.deepEqual(initial.map((node) => node.open), [true, true, false]);
    const lazy = initial[2]; lazy.open = true; await lazy.emit("toggle");
    assert.ok(descendants(host, hasClass("json-node")).length > initial.length);
    await control(host, "展开全部").emit("click");
    assert.equal(descendants(host, hasClass("json-node")).length, 6);
    assert.ok(descendants(host, hasClass("json-node")).every((node) => node.open));
    await control(host, "折叠全部").emit("click");
    assert.ok(descendants(host, hasClass("json-node")).every((node) => !node.open));
    jsonViewer.render(host, value, {stateKey: "stream"});
    assert.equal(descendants(host, hasClass("json-node"))[0].open, false);
    jsonViewer.render(host, value, {stateKey: "different-endpoint"});
    assert.equal(descendants(host, hasClass("json-node"))[0].open, true);
});

test("empty and primitive JSON responses remain displayable and copyable", async () => {
    const doc = domDocument(); const host = doc.createElement("div");
    for (const value of [null, false, 0, "", "response", [], {}]) {
        jsonViewer.render(host, value);
        assert.equal(descendants(host, hasClass("json-line"))[0].textContent, JSON.stringify(value) + "\n");
        await control(host, "展开全部").emit("click");
        await control(host, "复制 JSON").emit("click");
        assert.equal(doc.copied, JSON.stringify(value, null, 4));
    }
    doc.defaultView.navigator.clipboard.writeText = async () => { throw new Error("denied"); };
    doc.execCommand = () => false; // Both copy paths fail: only then may the page ask for manual copying.
    await control(host, "复制 JSON").emit("click");
    assert.match(host.textContent, /复制失败/);
});

test("copying falls back to execCommand when the clipboard API is unavailable on plain HTTP origins", async () => {
    // navigator.clipboard does not exist outside secure contexts (https or localhost);
    // the page service is routinely reached over LAN HTTP, so copying must still work.
    const doc = domDocument(); const host = doc.createElement("div");
    delete doc.defaultView.navigator.clipboard;
    const value = {data: {reply: "局域网复制", count: 0}};
    jsonViewer.render(host, value);
    await control(host, "复制 JSON").emit("click");
    assert.equal(doc.selectedElement?.value, JSON.stringify(value, null, 4));
    assert.equal(doc.copied, null);
    assert.match(host.textContent, /已复制完整 JSON/);

    // Request JSON in the editor shares the same fallback.
    const harness = browserHarness(() => { throw new Error("unexpected fetch"); }, {group: "record"}, null, true);
    delete harness.document.defaultView.navigator.clipboard;
    assert.equal(harness.nodes["#api-copy-json"].disabled, false);
    await harness.nodes["#api-copy-json"].emit("click");
    assert.equal(harness.document.copied, null);
    assert.equal(harness.nodes["#api-json-feedback"].textContent, "已复制请求 JSON");
    assert.match(harness.nodes["#api-json"].value, /"userId"/);
});

test("copy helpers never throw and keep feedback honest when execCommand also fails", async () => {
    const doc = domDocument(); const host = doc.createElement("div");
    delete doc.defaultView.navigator.clipboard;
    doc.execCommand = () => false;
    jsonViewer.render(host, {data: 1});
    await control(host, "复制 JSON").emit("click");
    assert.match(host.textContent, /复制失败/);

    const harness = browserHarness(() => { throw new Error("unexpected fetch"); }, {group: "record"}, null, true);
    delete harness.document.defaultView.navigator.clipboard;
    harness.document.execCommand = () => false;
    await harness.nodes["#api-copy-json"].emit("click");
    assert.match(harness.nodes["#api-json-feedback"].textContent, /复制失败/);
});

test("catalog fields match current HTTP handlers and the Report request schema", () => {
    const routes = fs.readFileSync(path.join(root, "web/routes.py"), "utf8");
    const files = new Map();
    for (const file of fs.readdirSync(path.join(root, "web/handlers")).filter((name) => name.endsWith(".py"))) {
        const source = fs.readFileSync(path.join(root, "web/handlers", file), "utf8");
        for (const [, name] of source.matchAll(/^class (\w+)\(/gm)) files.set(name, file.slice(0, -3));
    }
    function bodyFields(text) {
        const fields = new Set();
        for (const match of text.matchAll(/set\(body\) != \{([^}]+)\}/g)) {
            for (const [, field] of match[1].matchAll(/['"]([^'"]+)['"]/g)) fields.add(field);
        }
        for (const match of text.matchAll(/\bbody(?:\.get\(\s*['"]([^'"]+)['"]|\[['"]([^'"]+)['"]\])/g)) fields.add(match[1] || match[2]);
        for (const match of text.matchAll(/(?:_required|_required_text|_optional_text)\(\s*body,\s*['"]([^'"]+)['"]/g)) fields.add(match[1]);
        for (const match of text.matchAll(/self\.get_(?:query_)?argument\(\s*['"]([^'"]+)['"]/g)) fields.add(match[1]);
        return fields;
    }
    for (const {path: route, handler} of publicRoutes()) {
        if (nonLabRoutes.has(route)) continue;
        const source = fs.readFileSync(path.join(root, `web/handlers/${files.get(handler)}.py`), "utf8");
        const block = source.slice(source.indexOf(`class ${handler}(`)).split(/\nclass /)[0];
        const fields = bodyFields(block);
        if (block.includes("_positive_round(body)")) fields.add("round");
        if (block.includes("reasoning_effort_from_body(body)")) {
            bodyFields(fs.readFileSync(path.join(root, "web/handlers/llm_options.py"), "utf8")).forEach((field) => fields.add(field));
        }
        if (block.includes("_report_reference(body)")) {
            const helper = source.slice(source.indexOf("def _report_reference(")).split(/\nclass /)[0];
            bodyFields(helper).forEach((field) => fields.add(field));
        }
        if (handler === "ReportGenerateHandler") {
            const schema = fs.readFileSync(path.join(root, "modules/report/schemas.py"), "utf8").split("class ReportRequest(")[1].split("    @field_validator")[0];
            for (const [, name] of schema.matchAll(/^    (\w+):/gm)) fields.add(name.replace(/_([a-z])/g, (_, char) => char.toUpperCase()));
        }
        const matches = contracts.filter((item) => item.path === route);
        for (const endpoint of matches) {
            let expected = fields;
            if (matches.length > 1) {
                const start = block.search(new RegExp(`^    (?:async )?def ${endpoint.method.toLowerCase()}\\(`, "m"));
                expected = bodyFields(block.slice(start).split(/\n    (?:async )?def /)[0]);
            }
            for (const field of endpoint.fields) if (field.location === "path") expected.add(field.name);
            assert.deepEqual(endpoint.fields.map((field) => field.name).sort(), [...expected].sort(), `${endpoint.method} ${route}`);
        }
    }
});

test("legacy backend is migrated and identities survive navigation and refresh", () => {
    const storage = memoryStorage({"shulan-api-backend": " http://dev.example:9002/// "});
    const first = core.createContextStore(storage, makeIds());
    assert.equal(first.get().backend, "http://dev.example:9002");
    first.update({userId: "arbitrary-user", sessionId: "session-a", secret: "must-not-save"});
    const second = core.createContextStore(storage, () => { throw new Error("must not regenerate IDs"); });
    assert.deepEqual(second.get(), first.get());
    assert.deepEqual(Object.keys(JSON.parse(storage.getItem(core.STORAGE_KEY))).sort(), ["backend", "sessionId", "userId"]);
});

test("intentional empty identities stay empty until explicitly regenerated", () => {
    const storage = memoryStorage({[core.STORAGE_KEY]: JSON.stringify(ids)});
    const first = core.createContextStore(storage);
    first.update({userId: "", sessionId: ""});
    assert.equal(core.createContextStore(storage).get().userId, "");
    assert.equal(core.createContextStore(storage).get().sessionId, "");
});

test("new session preserves user; new user replaces both; tabs accept stored changes", () => {
    const storage = memoryStorage({[core.STORAGE_KEY]: JSON.stringify(ids)});
    const a = core.createContextStore(storage, makeIds());
    const b = core.createContextStore(storage, makeIds());
    let notifications = 0;
    b.subscribe(() => { notifications += 1; });
    a.newSession();
    assert.equal(a.get().userId, ids.userId);
    assert.notEqual(a.get().sessionId, ids.sessionId);
    b.receive(storage.getItem(core.STORAGE_KEY));
    assert.deepEqual(a.get(), b.get());
    assert.equal(notifications, 1);
    a.newUser(); b.receive(storage.getItem(core.STORAGE_KEY));
    assert.notEqual(b.get().userId, ids.userId);
    assert.equal(notifications, 2);
    b.receive("invalid JSON");
    assert.equal(notifications, 2);
});

test("corrupt or unavailable storage does not prevent page requests", () => {
    const corrupt = core.createContextStore(memoryStorage({[core.STORAGE_KEY]: "{"}), makeIds());
    assert.match(corrupt.get().userId, /^test-user-/);
    const unavailable = core.createContextStore(null, makeIds());
    unavailable.update({userId: "ordinary-user"});
    assert.equal(unavailable.persistent, false);
    assert.equal(unavailable.get().userId, "ordinary-user");
});

test("optional values are omitted without losing false, zero, empty objects, arrays, or null", () => {
    const profile = draft("health-profile.get");
    assert.deepEqual(plain(profile.parameters(ids)), {userId: ids.userId});
    profile.set("includeEvidence", false); profile.set("indicatorLimit", 0);
    assert.deepEqual(plain(profile.parameters(ids)), {userId: ids.userId, includeEvidence: false, indicatorLimit: 0});
    const record = draft("record.create");
    record.set("hospitalData", []); record.set("smartDeviceData", {}); record.set("basicInfo", null);
    assert.deepEqual(plain(record.parameters(ids)), {userId: ids.userId, sessionId: ids.sessionId, hospitalData: [], smartDeviceData: {}, basicInfo: null});
});

test("profile scope and status filters serialize exactly as the new contract requires", () => {
    const profile = draft("health-profile.get");
    profile.set("indicatorScope", ["abnormal"]);
    profile.set("factStatuses", ["pending", "disputed"]);
    assert.deepEqual(plain(profile.parameters(ids)), {
        userId: ids.userId, indicatorScope: ["abnormal"], factStatuses: ["pending", "disputed"],
    });
    profile.set("factStatuses", ["active"]);
    assert.deepEqual(plain(profile.parameters(ids).factStatuses), ["active"]);
    profile.set("factStatuses", undefined);
    assert.equal(profile.parameters(ids).factStatuses, undefined, "an empty selection must omit the field");
    profile.set("indicatorScope", undefined);
    assert.equal("indicatorScope" in profile.parameters(ids), false);
    profile.set("indicatorScope", ["flagged", "pending"]);
    assert.deepEqual(plain(profile.parameters(ids).indicatorScope), ["flagged", "pending"]);
    const fields = byId("health-profile.get").fields;
    assert.equal(fields.some((field) => field.name === "includePending"), false);
    const field = fields.find((item) => item.name === "factStatuses");
    assert.deepEqual(field.example, ["pending", "disputed"]);
    assert.deepEqual(field.serverDefault, ["active", "pending", "disputed"]);
    assert.match(field.hint, /factGroups/);
    assert.deepEqual(
        byId("health-profile.get").fields.find((item) => item.name === "indicatorScope").choices,
        ["all", "abnormal", "flagged", "pending", "unjudged"],
    );
});

test("raw JSON supports omissions, unknown fields, and temporary identity without merging form state", () => {
    const model = draft("chat.send.complete");
    model.set("message", "form message"); model.setMode("json", ids);
    model.json = '{"userId":"temporary","newField":null,"thinking":false}';
    model.set("message", "changed form");
    assert.deepEqual(model.parameters(ids), {userId: "temporary", newField: null, thinking: false});
    model.setMode("form", ids); model.setMode("json", ids);
    assert.deepEqual(model.parameters(ids), {userId: "temporary", newField: null, thinking: false});
    model.resetForm();
    assert.equal(model.parameters(ids).userId, "temporary");
    model.json = "{}";
    assert.deepEqual(model.parameters(ids), {});
    model.regenerate(ids);
    assert.equal(model.parameters(ids).userId, ids.userId);
    assert.equal(ids.userId, "developer-001");
});

test("invalid JSON is rejected instead of sending an earlier valid draft", () => {
    const model = draft("chat.send.complete"); model.setMode("json", ids);
    for (const json of ["{", "null", "[]", '"string"']) { model.json = json; assert.throws(() => model.parameters(ids), /JSON/); }
});

test("GET encodes query and optional identity can be omitted independently", () => {
    const model = draft("chat.suggestions");
    model.set("count", 5);
    const outgoing = core.buildRequest(byId("chat.suggestions"), model.parameters({...ids, userId: "a&b 中"}));
    assert.equal(outgoing.body, undefined);
    assert.equal(new URL(outgoing.path, ids.backend).searchParams.get("userId"), "a&b 中");
    model.omitted.add("userId");
    assert.equal(core.buildRequest(byId("chat.suggestions"), model.parameters(ids)).path, "/api/v1/chat/suggestions?count=5");
    assert.deepEqual(plain(draft("health").parameters(ids)), {});
});

test("workflow defaults allow explicit manual overrides and independent requests", () => {
    const model = draft("record.answer.complete");
    model.set("questionId", "from-status", {onlyDefault: true});
    model.set("questionId", "manual-question");
    model.set("questionId", "next-status", {onlyDefault: true});
    model.set("answer", false); model.set("other", "also send this");
    assert.equal(model.parameters(ids).questionId, "manual-question");
    assert.equal(model.parameters(ids).answer, false);
    assert.equal(model.parameters(ids).other, "also send this");
});

test("interpret source is exclusive in form mode and raw mode can exercise invalid combinations", () => {
    const model = draft("analysis.health-report");
    assert.throws(() => model.parameters(ids), /必须且只能/);
    model.set("documentId", "existing-document");
    assert.equal(model.parameters(ids).fileId, undefined);
    model.set("fileId", "https://example.com/new.pdf");
    assert.throws(() => model.parameters(ids), /必须且只能/);
    model.setMode("json", ids);
    assert.equal(model.parameters(ids).documentId, "existing-document");
});

test("interpret public quality count and warnings remain visible in the result", async () => {
    const processing = {status: "partial", issueCount: 2, retryable: true, warnings: ["部分内容尚未确认，已保留可确认的结果。"]};
    const {app, nodes} = browserHarness(async () => jsonResponse({processing}), {}, "interpret.js");
    app.setParams("analysis.health-report", {documentId: "existing-document"});
    await app.run("analysis.health-report");
    await control(nodes["#api-result"], "展开全部").emit("click");
    assert.match(nodes["#api-result"].textContent, /issueCount/);
    assert.match(nodes["#api-result"].textContent, /部分内容尚未确认/);
    const html = fs.readFileSync(path.join(pages, "interpret.html"), "utf8");
    assert.match(nodes["#interpret-preview"].textContent, /处理问题2.*部分内容尚未确认/);
    assert.ok(html.indexOf('id="interpret-preview"') < html.indexOf('id="api-result"'));
});

test("interpret previews the real response, preserves scalars and keeps raw JSON and logs", async () => {
    const observations = [
        {itemName: "<img src=x onerror=alert(1)>", resultValue: 0, unit: "mmol/L", referenceRange: "0–1"},
        {itemName: "尿潜血", resultValue: "-", observedAt: "2024-11-04"},
        {itemName: "白细胞", resultValue: "阴性", flag: "偏高", specimen: "尿液", method: "镜检"},
    ];
    const payload = {code: 200, elapsed_ms: 17, data: {
        title: "合成体检报告", reportType: "checkup", reportTime: "2024-11-04", status: "abnormal",
        processing: {status: "complete"}, observations, expansion: observations,
        unresolvedItems: [{itemName: "项目甲", resultValue: null, missingFields: ["resultValue"], reason: "原文未提供结果"}],
        sections: [{title: "腹部超声", reportType: "imaging", reportTime: "2024-11-03",
            expansion: {findings: "示例所见", diagnosis: "示例诊断"}}],
    }};
    const {app, nodes, document} = browserHarness(async () => new Response(JSON.stringify(payload)), {}, "interpret.js");
    const model = app.draft("analysis.health-report");
    model.setMode("json", ids);
    model.json = JSON.stringify({userId: "another-user", documentId: "existing-document"});
    await app.run("analysis.health-report");

    const preview = nodes["#interpret-preview"];
    assert.match(preview.textContent, /合成体检报告.*处理完成.*17 ms/);
    assert.match(preview.textContent, /原文未提供结果.*腹部超声.*示例所见.*示例诊断/);
    assert.match(preview.textContent, /标本.*方法/);
    assert.equal(descendants(preview, (node) => node.tagName === "img").length, 0);
    assert.match(preview.textContent, /<img src=x onerror=alert\(1\)>/);
    assert.equal(descendants(preview, (node) => node.tagName === "table").length, 2);
    assert.deepEqual(descendants(preview, hasClass("interpret-value")).map((node) => node.textContent), ["0", "-", "阴性", "—"]);
    assert.deepEqual(descendants(preview, hasClass("interpret-flag")).map((node) => node.textContent), ["偏高"]);

    await control(nodes["#api-result"], "展开全部").emit("click");
    await control(nodes["#api-result"], "折叠全部").emit("click");
    await control(nodes["#api-result"], "复制 JSON").emit("click");
    assert.deepEqual(JSON.parse(document.copied), payload);
    assert.match(nodes["#raw-log"].textContent, /RESPONSE 200/);
    app.context.update({userId: "next-user"});
    assert.match(preview.textContent, /公共设置已改变/);
    assert.doesNotMatch(preview.textContent, /合成体检报告/);
});

test("interpret replaces preview across partial, unavailable and HTTP error responses", async () => {
    const replies = [
        jsonResponse({title: "部分报告", processing: {status: "partial", warnings: ["部分内容待核对"]},
            observations: [{itemName: "尿酸", resultValue: "100"}]}),
        jsonResponse({reportType: "unknown", processing: {status: "unavailable"}, observations: []}),
        new Response(JSON.stringify({code: 502, message: "上游不可用", data: null}), {status: 502}),
        jsonResponse(null),
    ];
    const {app, nodes} = browserHarness(async () => replies.shift(), {}, "interpret.js");
    app.setParams("analysis.health-report", {documentId: "existing-document"});
    const preview = nodes["#interpret-preview"];
    await app.run("analysis.health-report");
    assert.match(preview.textContent, /部分读取.*部分内容待核对.*尿酸/);
    await app.run("analysis.health-report");
    assert.match(preview.textContent, /无可用结果/);
    assert.doesNotMatch(preview.textContent, /尿酸/);
    await app.run("analysis.health-report");
    assert.match(preview.textContent, /HTTP 502.*上游不可用/);
    assert.match(nodes["#api-result"].textContent, /"code": 502/);
    assert.match(nodes["#raw-log"].textContent, /RESPONSE 502/);
    await app.run("analysis.health-report");
    assert.match(preview.textContent, /没有可预览/);
});

test("ordinary and SSE sends use the same draft and outgoing parameters", async () => {
    const requests = [];
    const {app} = browserHarness(async (url, options) => {
        requests.push({url, options});
        return options.headers.Accept === "text/event-stream"
            ? new Response('data: {"type":"content","content":"好"}\n\ndata: {"type":"finish"}\n\n')
            : new Response('{"data":{"reply":"好"}}');
    });
    assert.equal(app.draft("chat.send.complete"), app.draft("chat.send.stream"));
    app.setParams("chat.send.complete", {message: "hello", thinking: false, finalReportId: "batch", reportType: "evaluate"});
    await app.run("chat.send.complete"); await app.run("chat.send.stream");
    assert.equal(requests.length, 2);
    assert.equal(requests[0].options.body, requests[1].options.body);
    assert.equal(JSON.parse(requests[1].options.body).thinking, false);
    assert.match(decodeURIComponent(requests[1].url), /\/api\/v1\/chat\/send\/stream/);
});

test("actual raw transmission preserves unknown keys and skips foreign-identity workflow callbacks", async () => {
    const outgoing = []; let rendered = false;
    const {app, nodes} = browserHarness(async (_url, options) => { outgoing.push(options.body); return new Response('{"data":{}}'); }, {onResponse() { rendered = true; }});
    const model = app.draft("report.get"); model.setMode("json", ids);
    model.json = '{"userId":"another","custom":[],"limit":0}';
    await app.run("report.get");
    assert.deepEqual(JSON.parse(outgoing[0]), {userId: "another", custom: [], limit: 0});
    assert.equal(rendered, false);
    assert.equal(app.context.get().userId, ids.userId);
    assert.match(nodes["#raw-log"].textContent, /"custom": \[\]/);
});

test("late responses and failures after identity changes never update the new page", async () => {
    for (const status of [200, 503]) {
        const pending = deferred(); let rendered = false; let failed = false;
        const {app, nodes} = browserHarness(() => pending.promise, {onResponse() { rendered = true; }, onError() { failed = true; }});
        const response = app.run("health-profile.get");
        app.context.update({userId: "new-user"});
        pending.resolve(new Response('{"data":{"subjectId":"old-subject"}}', {status}));
        assert.equal(await response, null);
        assert.equal(rendered, false); assert.equal(failed, false);
        assert.match(nodes["#api-result"].textContent, /公共设置已改变/);
        assert.match(nodes["#raw-log"].textContent, /old-subject/);
        assert.equal(nodes["#request-status"].textContent, "就绪");
    }
});

test("SSE decodes split Unicode, CRLF and trailing events", async () => {
    const bytes = new TextEncoder().encode('data: {"type":"content","content":"健康"}\r\n\r\ndata: {"type":"finish"}');
    const chunks = new ReadableStream({start(controller) { for (let i = 0; i < bytes.length; i++) controller.enqueue(bytes.slice(i, i + 1)); controller.close(); }});
    const events = [];
    const {app} = browserHarness(async () => new Response(chunks), {onEvent(event) { events.push(event); }});
    app.setParams("chat.send.stream", {message: "hello"});
    const result = await app.run("chat.send.stream");
    assert.equal(result.eventCount, 2);
    assert.equal(events[0].content, "健康");
});

test("SSE error cancels its reader and keeps the full error payload", async () => {
    let cancelled = false; let rendered = false;
    const chunks = new ReadableStream({start(controller) { controller.enqueue(new TextEncoder().encode('data: {"type":"error","error":"test-error","detail":42}\n\n')); }, cancel() { cancelled = true; }});
    const {app, nodes} = browserHarness(async () => new Response(chunks), {onResponse() { rendered = true; }});
    app.setParams("chat.send.stream", {message: "hello"});
    assert.equal(await app.run("chat.send.stream"), null);
    assert.equal(cancelled, true); assert.equal(rendered, false);
    assert.match(nodes["#api-result"].textContent, /42/);
    assert.equal(nodes["#request-status"].textContent, "就绪");
});

test("HTTP error status and non-JSON response remain inspectable", async () => {
    const {app, nodes} = browserHarness(async () => new Response("upstream unavailable", {status: 503}));
    await app.run("health.ready");
    assert.match(nodes["#api-result"].textContent, /HTTP 503/);
    assert.match(nodes["#api-result"].textContent, /upstream unavailable/);
});

test("SSE from a previous identity stops callbacks and releases its reader", async () => {
    let controller; let cancelled = false; const events = [];
    const ready = deferred();
    const chunks = new ReadableStream({start(value) { controller = value; }, cancel() { cancelled = true; }});
    const {app, nodes} = browserHarness(async () => { ready.resolve(); return new Response(chunks); }, {onEvent(event) { events.push(event); }});
    app.setParams("chat.send.stream", {message: "hello"});
    const pending = app.run("chat.send.stream");
    await ready.promise;
    app.context.newSession();
    controller.enqueue(new TextEncoder().encode('data: {"type":"content","content":"old reply"}\n\n'));
    assert.equal(await pending, null);
    assert.equal(events.length, 0); assert.equal(cancelled, true);
    assert.match(nodes["#raw-log"].textContent, /old reply/);
});

test("custom cards remain available while a send is running; duplicate sends are blocked", async () => {
    const pending = deferred(); const started = deferred(); const calls = [];
    const {app} = browserHarness(async (url) => {
        const target = new URL(url).searchParams.get("target"); calls.push(target);
        if (target.endsWith("/send/complete")) { started.resolve(); return pending.promise; }
        return new Response('{"data":{"queued":true}}');
    });
    app.setParams("chat.send.complete", {message: "hello"});
    app.setParams("chat.messages.custom", {content: "doctor card", customType: "NEW_CARD_TYPE"});
    const sending = app.run("chat.send.complete"); await started.promise;
    assert.equal(await app.run("chat.send.complete"), null);
    assert.ok(await app.run("chat.messages.custom"));
    assert.equal(calls.length, 2);
    pending.resolve(new Response('{"data":{"reply":"done"}}')); await sending;
});

test("storage and back navigation events update an already initialized page", () => {
    const {app, events} = browserHarness(() => { throw new Error("unexpected fetch"); });
    app.setParams("record.answer.complete", {questionId: "old-question"});
    events.storage({key: core.STORAGE_KEY, newValue: JSON.stringify({...ids, userId: "new-user"})});
    assert.equal(app.context.get().userId, "new-user");
    assert.equal(app.draft("record.answer.complete").values.questionId, "");
    events.pageshow();
    assert.equal(app.context.get().userId, ids.userId);
});

test("external identity updates replace focused old fields and clear the focused composer", () => {
    const {app, nodes, events, document} = browserHarness(() => { throw new Error("unexpected fetch"); });
    nodes["#user-id"] = {value: ids.userId};
    document.activeElement = nodes["#user-id"];
    events.storage({key: core.STORAGE_KEY, newValue: JSON.stringify({...ids, userId: "new-user"})});
    assert.equal(nodes["#user-id"].value, "new-user");
    nodes["#message"] = {value: "old-message", addEventListener() {}};
    app.setParams("chat.send.complete", {message: "old-message"});
    app.bindInput("#message", "chat.send.complete", "message");
    document.activeElement = nodes["#message"];
    app.context.newSession();
    assert.equal(nodes["#message"].value, "");
});

test("ancillary subject-list loading preserves the displayed profile response", async () => {
    const {app, nodes} = browserHarness(async () => new Response('{"data":{"subjects":[]}}'));
    nodes["#api-result"].textContent = "profile response";
    await app.run("health-profile.subjects.list", {showResult: false});
    assert.equal(nodes["#api-result"].textContent, "profile response");
    assert.match(nodes["#raw-log"].textContent, /subjects/);
});

function recordQuestion(overrides = {}) {
    return {status: "question", phase: "intake", round: 1, interactionCount: 0, progress: "0%", questionId: "q-1",
        questionType: "text", question: "近期有哪些困扰？", options: {content: ""}, assistantMessage: "先了解您的情况。", reasoning: "", ...overrides};
}
const jsonResponse = (data, status = 200) => new Response(JSON.stringify({code: status, data}), {status});

async function renderProfile(data) {
    const subject = {subjectId: "synthetic-subject", kind: "self", displayName: "合成用户"};
    const calls = [];
    const {nodes} = browserHarness(async (url) => {
        const target = new URL(new URL(url).searchParams.get("target") || url);
        calls.push(target.pathname);
        if (target.pathname === "/api/v1/health-profile/subjects/list") return jsonResponse({subjects: [subject]});
        assert.equal(target.pathname, "/api/v1/health-profile/get");
        return jsonResponse({subject, ...data});
    }, {}, "health-profile-view.js");
    const payload = await nodes["#load-profile"].emit("click");
    assert.equal(payload?.data?.subject?.subjectId, subject.subjectId);
    assert.deepEqual(calls, ["/api/v1/health-profile/get", "/api/v1/health-profile/subjects/list"]);
    return nodes;
}

async function renderProfileFacts(items) {
    const nodes = await renderProfile({factGroups: [{name: "检验指标", items}]});
    return nodes["#fact-groups"];
}

test("profile indicator groups stay visible in fixed order with partial or empty data", async () => {
    const lab = {code: "HGB", name: "血红蛋白", current: {value: 165, unit: "g/L"}};
    const unmapped = {code: "UNMAPPED", name: "合成检测结果", current: {value: "阴性"}};
    const scenarios = [
        {indicatorGroups: [
            {code: "report_observation", name: "未标准化检测结果", indicators: [unmapped]},
            {code: "laboratory", name: "检验指标", indicators: [lab]},
        ], expected: [null, lab, null, null, unmapped]},
        {indicatorGroups: [], expected: [null, null, null, null, null]},
    ];
    const names = ["身体测量", "检验指标", "菌群检测", "报告评估", "未标准化检测结果"];
    for (const {indicatorGroups, expected} of scenarios) {
        const nodes = await renderProfile({indicatorGroups});
        const groups = descendants(nodes["#indicator-groups"], hasClass("hpv-indicator-group"));
        const headings = groups.map((group) => group.querySelector(".hpv-indicator-group-head"));
        assert.deepEqual(headings.map((heading) => heading.children[0].textContent), names);
        assert.deepEqual(headings.map((heading) => heading.children[1].textContent),
            expected.map((indicator) => indicator ? "1 项" : "0 项"));
        assert.equal(nodes["#indicator-count"].textContent, `${expected.filter(Boolean).length} 项`);
        for (const [index, group] of groups.entries()) {
            const emptyState = group.querySelector(".hpv-empty-state");
            const table = group.querySelector(".hpv-indicator-table");
            if (expected[index]) {
                assert.equal(emptyState, null);
                assert.equal(table.querySelector(".hpv-indicator-name").children[0].textContent, expected[index].name);
            } else {
                assert.match(emptyState.textContent, /暂无指标/);
                assert.equal(table, null);
            }
        }
    }
});

test("profile trend rows lead with names and disclose indicator metadata and prior observations", async () => {
    const current = {
        factId: "synthetic-hgb-current", value: 16.5, rawValue: "165.0", unit: "g/dL", rawUnit: "g/L",
        referenceRange: "130-175", interpretation: "normal", valueType: "quantity", status: "active",
        observedAt: "2026-11-11T08:00:00Z", verificationStatus: "verified_source",
        method: "示例检测方法", specimen: "示例标本",
        sources: [{sourceType: "health_report", sourceId: "synthetic-indicator-report", sourceRevision: 2}],
    };
    const previous = {
        factId: "synthetic-hgb-previous", value: 133.7, rawValue: "133.70", unit: "g/L",
        observedAt: "2026-10-11T08:00:00Z", status: "active", verificationStatus: "verified_source",
    };
    const nodes = await renderProfile({indicatorGroups: [{code: "laboratory", indicators: [
        {name: "血红蛋白", code: "HEMOGLOBIN", factType: "laboratory.hemoglobin", trend: "up",
            current, observations: [previous, {...current}]},
        {code: "CODE_ONLY", factType: "laboratory.private_fallback",
            current: {value: 1, rawValue: 0, unit: "mg/dL", rawUnit: "mg/L", status: "pending"}},
        {factType: "laboratory.private_no_name", current: {value: "阴性", status: "disputed"}},
    ]}]});
    const table = nodes["#indicator-groups"].querySelector(".hpv-indicator-table");
    assert.deepEqual(table.querySelector(".hpv-table-head").children.map((cell) => cell.textContent),
        ["指标", "结果", "参考范围", "结果判定", "趋势"]);

    const [named, codeOnly, noName] = descendants(table, hasClass("hpv-indicator-item"));
    const summary = named.querySelector(".hpv-indicator-row");
    assert.equal(named.tagName, "details");
    assert.ok(!named.open);
    assert.equal(summary.tagName, "summary");
    assert.equal(summary.children.length, 5);
    assert.equal(summary.querySelector(".hpv-indicator-name").children[0].textContent, "血红蛋白");
    assert.doesNotMatch(summary.textContent,
        /HEMOGLOBIN|laboratory\.hemoglobin|示例检测方法|示例标本|verified_source|synthetic-indicator-report/);
    assert.equal(summary.children[1].textContent, "165.0 g/L");
    assert.equal(codeOnly.querySelector(".hpv-result").textContent, "0 mg/L");
    assert.equal(summary.children[2].textContent, "130-175");
    assert.equal(summary.children[3].textContent, "正常");
    assert.equal(summary.children[3].querySelector(".hpv-judgment-basis"), null);
    assert.match(summary.children[4].textContent, /↑/);
    assert.equal(codeOnly.querySelector(".hpv-indicator-name").children[0].textContent, "CODE_ONLY");
    assert.equal(noName.querySelector(".hpv-indicator-name").children[0].textContent, "未命名指标");
    assert.doesNotMatch(noName.querySelector(".hpv-indicator-row").textContent, /private_no_name/);
    assert.equal(summary.querySelector(".hpv-fact-status"), null);
    for (const [item, status, label] of [[codeOnly, "pending", "待确认"], [noName, "disputed", "有争议"]]) {
        const row = item.querySelector(".hpv-indicator-row");
        const badge = row.querySelector(".hpv-indicator-name").querySelector(".hpv-fact-status");
        assert.ok(hasClass(status)(badge));
        assert.equal(badge.textContent, label);
        assert.equal(row.children.length, 5);
    }

    const detail = named.querySelector(".hpv-indicator-meta").textContent;
    for (const text of ["指标编码", "HEMOGLOBIN", "事实类型", "laboratory.hemoglobin", "标本", "示例标本",
        "检测方法", "示例检测方法", "事实状态", "已生效", "核验状态", "来源", "synthetic-indicator-report"]) {
        assert.ok(detail.includes(text), `missing profile indicator detail: ${text}`);
    }
    const history = named.querySelector(".hpv-indicator-history");
    assert.match(history.textContent, /133\.7/);
    assert.doesNotMatch(history.textContent, /165/);
    assert.equal(codeOnly.querySelector(".hpv-indicator-history"), null);
});

test("profile result judgments show the decision basis, unavailable reason, and range conflicts", async () => {
    const cases = [
        [{status: "high", basis: "report_flag", referenceStatus: "normal"}, "偏高", "报告标记", "按范围为正常"],
        [{status: "low", basis: "source_flag"}, "偏低", "来源传入"],
        [{status: "negative", basis: "source_result"}, "阴性", "原始结果"],
        [{status: "detected", basis: "source_result"}, "检出", "原始结果"],
        [{status: "not_detected", basis: "source_result"}, "未检出", "原始结果"],
        [{status: "positive", basis: "source_result"}, "阳性", "原始结果"],
        [{status: "normal", basis: "reference_range"}, "正常", "按参考范围"],
        [{status: "unknown", basis: null, reason: "unit_mismatch"}, "判定依据不足", "结果与参考范围单位不一致"],
        [{status: "unknown", basis: null, reason: "missing_reference_range"}, "判定依据不足", "缺少参考范围"],
    ];
    const indicators = cases.map(([resultJudgment]) => ({
        name: "示例指标",
        current: {value: 1, interpretation: "critical", resultJudgment},
    }));
    const nodes = await renderProfile({indicatorGroups: [{code: "laboratory", indicators}]});
    const cells = descendants(nodes["#indicator-groups"], hasClass("hpv-result-judgment"));
    assert.equal(cells.length, cases.length);
    cases.forEach(([, status, explanation, conflict], index) => {
        const cell = cells[index];
        assert.equal(cell.querySelector(".hpv-interpretation").textContent, status);
        assert.equal(cell.querySelector(".hpv-judgment-basis").textContent, explanation);
        assert.equal(cell.querySelector(".hpv-judgment-reference")?.textContent, conflict);
    });
});

test("profile fact summaries preserve reported quantity, reference range and source flag", async () => {
    const group = await renderProfileFacts([{
        displayName: "合成检验",
        factType: "lab.synthetic",
        valueType: "quantity",
        value: {raw: "<0.10", number: 0.1, unit: "mg/dL", comparator: "<",
            normalizedNumber: 1, normalizedUnit: "mg/L", specimen: "合成标本", method: "合成检测法"},
        referenceRange: {text: "0.2–0.8 mg/dL", low: 0.2, high: 0.8, unit: "mg/L"},
        interpretation: "low",
        status: "active",
        verificationStatus: "verified_source",
        version: 4,
        effectivePeriod: {start: null},
        sources: [{sourceType: "health_report", sourceId: "synthetic-report", sourceRevision: 2,
            recordedAt: "2026-11-16T08:00:00Z"}],
    }]);
    assert.deepEqual(group.querySelector(".hpv-fact-columns").children.map((node) => node.textContent.trim()),
        ["项目", "结果", "参考范围", "状态"]);
    const item = group.querySelector(".hpv-fact-item");
    assert.equal(item.tagName, "details");
    assert.ok(!item.open);
    const summary = item.querySelector(".hpv-fact-row");
    assert.equal(summary.tagName, "summary");
    assert.equal(summary.querySelector(".hpv-fact-name").textContent, "合成检验");
    const value = summary.querySelector(".hpv-fact-value");
    assert.match(value.textContent, /^<0\.10 mg\/dL/);
    assert.equal(value.querySelector(".hpv-interpretation").textContent, "偏低");
    assert.equal(summary.querySelector(".hpv-fact-reference").children.at(-1).textContent, "0.2–0.8 mg/dL");
    assert.equal(summary.children.at(-1), summary.querySelector(".hpv-fact-status"));
    assert.equal(summary.children.at(-1).textContent, "已生效");
    assert.doesNotMatch(summary.textContent, /已核验|verified_source|lab\.synthetic|合成标本|合成检测法/);

    const detail = item.querySelector(".hpv-fact-meta").textContent;
    for (const text of ["标本", "合成标本", "检测方法", "合成检测法", "事实编码", "lab.synthetic",
        "观察时间", "录入时间", "2026", "核验状态", "来源", "synthetic-report", "版本", "4"]) {
        assert.ok(detail.includes(text), `missing profile fact detail: ${text}`);
    }
    assert.doesNotMatch(detail, /事实状态|已生效/);
});

test("profile fact summaries retain zero and load qualitative values as text without missing flags", async () => {
    const narrative = "合成报告 <em>保留原文</em> " + "较长的定性结果段落。".repeat(20);
    const group = await renderProfileFacts([
        {displayName: "零值", valueType: "quantity", value: {number: 0, unit: "U/L"},
            interpretation: "unknown", referenceRange: null, status: "pending"},
        {displayName: "定性观察", valueType: "text", value: narrative, status: "disputed"},
    ]);
    const [zero, text] = descendants(group, hasClass("hpv-fact-item"));
    const zeroRow = zero.querySelector(".hpv-fact-row");
    assert.equal(zeroRow.querySelector(".hpv-fact-value").textContent, "0 U/L");
    for (const [item, status] of [[zero, "待确认"], [text, "有争议"]]) {
        const summary = item.querySelector(".hpv-fact-row");
        assert.match(summary.querySelector(".hpv-fact-reference").textContent, /—/);
        assert.equal(summary.querySelector(".hpv-interpretation"), null);
        assert.equal(summary.children.at(-1), summary.querySelector(".hpv-fact-status"));
        assert.equal(summary.children.at(-1).textContent, status);
        assert.doesNotMatch(item.querySelector(".hpv-fact-meta").textContent, /标本|检测方法|事实状态|待确认|有争议/);
    }
    const value = text.querySelector(".hpv-fact-row").querySelector(".hpv-fact-value");
    assert.equal(value.textContent, narrative);
    assert.equal(value.querySelectorAll("em").length, 0);
});

test("Record fixed supplement option submits the ordinary answer endpoint in one click", async () => {
    const requests = [];
    const {app, nodes} = browserHarness(async (url, options) => {
        requests.push({url, body: JSON.parse(options.body)});
        return jsonResponse(requests.length === 1
            ? recordQuestion({phase: "supplement", questionType: "radio", options: ["没有了"], assistantMessage: "已了解餐后不适及既往情况。"})
            : {status: "finished", phase: "finished", assistantMessage: "本次建档已结束。"});
    }, {}, "record.js");
    await app.run("record.status");
    assert.equal(nodes["#no-more"].hidden, true);
    await control(nodes["#question-area"], "没有了").emit("click");
    assert.deepEqual(requests[1].body.answer, ["没有了"]);
    assert.equal(requests[1].body.questionId, "q-1");
    assert.match(requests[1].url, /qa%2Fanswer%2Fcomplete/);
    assert.match(nodes["#question-area"].textContent, /本次建档已结束/);
});

test("Record advances through answers, offers a supplement reply, and displays the final assistant message", async () => {
    const replies = [recordQuestion(), recordQuestion({questionId: "q-supplement", phase: "supplement", interactionCount: 3, assistantMessage: "还有其他疑问吗？"}),
        {status: "finished", phase: "finished", round: 1, interactionCount: 3, endReason: "no_more_questions", assistantMessage: "已记录，本次建档完成。", reasoning: ""}];
    const requests = [];
    const {app, nodes} = browserHarness(async (url, options) => { requests.push({url, body: JSON.parse(options.body)}); return jsonResponse(replies.shift()); }, {}, "record.js");
    await app.run("record.create");
    assert.match(nodes["#question-area"].textContent, /先了解您的情况/);
    assert.equal(nodes["#submit"].disabled, false);
    assert.equal(nodes["#no-more"].hidden, true);
    nodes["#other-answer"].value = "饭后腹胀"; await nodes["#other-answer"].emit("input");
    await app.run("record.answer.complete");
    assert.equal(requests[1].body.questionId, "q-1");
    assert.equal(requests[1].body.other, "饭后腹胀");
    assert.equal(nodes["#other-answer"].value, "");
    assert.equal(nodes["#no-more"].hidden, false);
    await nodes["#no-more"].emit("click");
    assert.equal(app.draft("record.answer.complete").parameters(ids).other, "没有了");
    await app.run("record.answer.complete");
    assert.equal(requests[2].body.questionId, "q-supplement");
    assert.equal(requests[2].body.other, "没有了");
    assert.match(nodes["#question-area"].textContent, /已记录，本次建档完成/);
    assert.doesNotMatch(nodes["#question-area"].textContent, /进度|\/20|第.*轮|继续下一轮/);
    assert.equal(nodes["#submit"].disabled, true);
    assert.equal(nodes["#submit-stream"].disabled, true);
    assert.equal(nodes["#no-more"].hidden, true);
    assert.ok(requests.every(({url}) => !/qa%2F(?:continue|complete)(?:&|$)/.test(url)));
});

test("Record concrete multi-select keeps explicit states exclusive without changing free text", async () => {
    const options = ["便秘", "反酸", "入睡困难", "容易疲劳", "减轻体重", "增加肌肉", "痤疮", "脱发", "肩颈疼痛", "缓解压力", "暂无改善需求", "不清楚"];
    const {app, nodes} = browserHarness(async () => jsonResponse(recordQuestion({questionType: "checkbox", options})), {}, "record.js");
    await app.run("record.status");
    await inputValue(nodes, "#other-answer", "我有自己的具体描述");
    const answer = () => plain(app.draft("record.answer.complete").parameters(ids).answer);
    await control(nodes["#question-area"], "便秘").emit("click");
    await control(nodes["#question-area"], "反酸").emit("click");
    assert.deepEqual(answer(), ["便秘", "反酸"]);
    await control(nodes["#question-area"], "不清楚").emit("click");
    assert.deepEqual(answer(), ["不清楚"]);
    await control(nodes["#question-area"], "暂无改善需求").emit("click");
    assert.deepEqual(answer(), ["暂无改善需求"]);
    await control(nodes["#question-area"], "减轻体重").emit("click");
    assert.deepEqual(answer(), ["减轻体重"]);
    assert.equal(app.draft("record.answer.complete").parameters(ids).other, "我有自己的具体描述");
    await control(nodes["#question-area"], "减轻体重").emit("click");
    assert.deepEqual(answer(), []);
});

test("Record activity selection keeps absence exclusive with concrete activities", async () => {
    const options = ["步行", "跑步", "骑行", "没有主动活动"];
    const {app, nodes} = browserHarness(async () => jsonResponse(recordQuestion({questionType: "checkbox", options})), {}, "record.js");
    await app.run("record.status");
    const answer = () => plain(app.draft("record.answer.complete").parameters(ids).answer);
    await control(nodes["#question-area"], "步行").emit("click");
    await control(nodes["#question-area"], "跑步").emit("click");
    assert.deepEqual(answer(), ["步行", "跑步"]);
    await control(nodes["#question-area"], "没有主动活动").emit("click");
    assert.deepEqual(answer(), ["没有主动活动"]);
    await control(nodes["#question-area"], "骑行").emit("click");
    assert.deepEqual(answer(), ["骑行"]);
});

test("Record text templates and selection answers fill the shared draft without overwriting raw JSON", async () => {
    let response = recordQuestion({options: {symptom: "", since: ""}});
    const {app, nodes} = browserHarness(async () => jsonResponse(response), {}, "record.js");
    await app.run("record.status");
    await control(nodes["#question-area"], "填入结构化回答模板").emit("click");
    assert.deepEqual(plain(app.draft("record.answer.complete").parameters(ids).answer), {symptom: "", since: ""});
    const model = app.draft("record.answer.complete"); model.setMode("json", ids); model.json = '{"manual":false}';
    await control(nodes["#question-area"], "填入结构化回答模板").emit("click");
    assert.equal(model.json, '{"manual":false}');
    model.setMode("form", ids);
    response = recordQuestion({questionId: "q-2", questionType: "checkbox", options: ["甲", "乙"]});
    await app.run("record.status");
    await control(nodes["#question-area"], "甲").emit("click"); await control(nodes["#question-area"], "乙").emit("click");
    assert.deepEqual(plain(model.parameters(ids).answer), ["甲", "乙"]);
    await control(nodes["#question-area"], "甲").emit("click");
    assert.deepEqual(plain(model.parameters(ids).answer), ["乙"]);
    response = recordQuestion({questionId: "q-3", questionType: "radio", options: ["甲", "乙"]});
    await app.run("record.status");
    await control(nodes["#question-area"], "甲").emit("click"); await control(nodes["#question-area"], "乙").emit("click");
    assert.deepEqual(plain(model.parameters(ids).answer), ["乙"]);
});

for (const streaming of [false, true]) {
    for (const questionType of ["radio", "checkbox"]) {
        test(`Record ${questionType} opening reaches the ${streaming ? "SSE" : "complete"} API and keeps free text available`, async () => {
            let current = recordQuestion({questionType, question: "最近有哪些身体感受或变化让您在意？",
                options: ["肠胃或消化不适", "排便有变化", "体重或精力有变化", "其他情况", "暂无明显不适"]});
            const requests = [];
            const {app, nodes} = browserHarness(async (_url, options) => {
                requests.push(JSON.parse(options.body));
                if (options.headers.Accept !== "text/event-stream") return jsonResponse(current);
                return new Response([{type: "start"}, {type: "question", ...current}, {type: "finish"}]
                    .map((data) => `data: ${JSON.stringify({data})}\n\n`).join(""));
            }, {}, "record.js");
            await app.run("record.create");
            await control(nodes["#question-area"], "肠胃或消化不适").emit("click");
            await control(nodes["#question-area"], "排便有变化").emit("click");
            current = recordQuestion({questionId: "q-2", questionType: "radio", options: ["空腹", "饭后", "不确定", "其他（请补充）"]});
            await app.run(streaming ? "record.answer.stream" : "record.answer.complete");
            assert.deepEqual(requests[1].answer, questionType === "checkbox" ? ["肠胃或消化不适", "排便有变化"] : ["排便有变化"]);
            assert.equal(requests[1].questionId, "q-1");
            assert.equal(requests[1].other || "", "");
            await app.run("record.status");
            await control(nodes["#question-area"], "空腹").emit("click");
            await control(nodes["#question-area"], "饭后").emit("click");
            await inputValue(nodes, "#other-answer", "喝奶后更明显");
            await app.run(streaming ? "record.answer.stream" : "record.answer.complete");
            assert.deepEqual(requests[3].answer, ["饭后"]);
            assert.equal(requests[3].questionId, "q-2");
            assert.equal(requests[3].other, "喝奶后更明显");
        });
    }
}

test("Record SSE handles question plus empty finish, reasoning false, auto finish and retryable errors", async () => {
    let current = recordQuestion();
    let response = null;
    const {app, nodes} = browserHarness(async (_url, options) => options.headers.Accept === "text/event-stream" ? response : jsonResponse(current), {}, "record.js");
    await app.run("record.create");
    const events = (...values) => new Response(values.map((data) => `data: ${JSON.stringify({data})}\n\n`).join(""));
    app.setParams("record.answer.complete", {other: "回答", thinking: false});
    response = events({type: "start"}, {type: "question", ...recordQuestion({questionId: "q-2", assistantMessage: "无需思考开关也能看到的回应"})}, {type: "finish"});
    const streamed = await app.run("record.answer.stream");
    assert.equal(streamed.data.questionId, "q-2");
    assert.equal(streamed.data.phase, "intake");
    assert.equal(streamed.eventCount, 3);
    assert.match(nodes["#question-area"].textContent, /无需思考开关也能看到的回应/);
    assert.equal(nodes["#submit"].disabled, false);
    app.setParams("record.answer.complete", {other: "重试此回答"});
    response = events({type: "reasoning", content: "分析片段"}, {type: "error", error: "稍后重试"});
    await app.run("record.answer.stream");
    assert.equal(nodes["#submit-stream"].disabled, false);
    assert.equal(app.draft("record.answer.complete").parameters(ids).questionId, "q-2");
    assert.equal(app.draft("record.answer.complete").parameters(ids).other, "重试此回答");
    response = events({type: "reasoning", content: "不应替换服务端的明确空值"}, {type: "finish", status: "finished", phase: "finished", assistantMessage: "最终答复", reasoning: ""});
    await app.run("record.answer.stream");
    assert.match(nodes["#question-area"].textContent, /最终答复/);
    assert.doesNotMatch(nodes["#question-area"].textContent, /不应替换服务端/);
    assert.equal(nodes["#submit"].disabled, true);
});

test("Record history replays assistant messages, and status preserves a manual default and same-question answers", async () => {
    let reply = recordQuestion();
    const {app, nodes} = browserHarness(async () => jsonResponse(reply), {}, "record.js");
    await app.run("record.status");
    app.setParams("record.answer.complete", {questionId: "manual-id", other: "草稿回答"});
    await app.run("record.status");
    assert.equal(app.draft("record.answer.complete").parameters(ids).questionId, "manual-id");
    assert.equal(app.draft("record.answer.complete").parameters(ids).other, "草稿回答");
    reply = {status: "finished", assistantMessage: "历史最终答复", history: [{question: "历史问题", answer: {content: "历史答案"}, assistantMessage: "历史承接说明"}]};
    await app.run("record.history");
    assert.match(nodes["#question-area"].textContent, /历史承接说明.*历史问题.*历史答案.*历史最终答复/s);
    assert.equal(nodes["#submit"].disabled, true);
    app.context.newSession();
    assert.doesNotMatch(nodes["#question-area"].textContent, /历史/);
    assert.equal(app.draft("record.answer.complete").values.questionId, "");
});

test("Record compatibility routes stay independently callable and are absent from the normal flow buttons", async () => {
    const html = fs.readFileSync(path.join(pages, "record.html"), "utf8");
    assert.doesNotMatch(html, /data-action="record\.(?:continue|complete)"/);
    assert.match(byId("record.continue").hint, /409/);
    assert.match(byId("record.complete").hint, /已完成/);
    const calls = [];
    const {app} = browserHarness(async (url, options) => {
        calls.push({path: new URL(url).searchParams.get("target"), body: JSON.parse(options.body)});
        return new Response('{"code":409,"message":"兼容行为"}', {status:409});
    }, {}, "record.js");
    await app.run("record.continue"); await app.run("record.complete");
    assert.equal(calls.length, 2);
    assert.ok(calls[0].path.endsWith("/qa/continue")); assert.ok(calls[1].path.endsWith("/qa/complete"));
    assert.equal(calls[1].body.round, 1);
});

test("Record unavailable and partial attachments remain visible while the first question is answerable", async () => {
    const documentProcessing = [
        {fileIndex: 0, status: "partial", message: "仅使用已确认内容", retryable: true},
        {fileIndex: 1, status: "unavailable", message: "可补充报告中的重要内容", retryable: true},
    ];
    let reply = recordQuestion({documentProcessing});
    const {app, nodes, timers} = browserHarness(async () => jsonResponse(reply), {}, "record.js");
    await app.run("record.create");
    assert.match(nodes["#question-area"].textContent, /附件 1 · 部分读取.*仅使用已确认内容/s);
    assert.match(nodes["#question-area"].textContent, /附件 2 · 暂不可用.*可补充报告中的重要内容/s);
    assert.equal(nodes["#submit"].disabled, false);
    assert.equal(timers.size, 0);
    reply = {status: "question", history: [], documentProcessing};
    await app.run("record.history");
    assert.match(nodes["#question-area"].textContent, /附件 2 · 暂不可用/);
});

async function addRecordFile(nodes, value, {source = "url", type = ""} = {}) {
    if (nodes["#record-file-value"].value) await nodes["#record-file-add"].emit("click");
    if (nodes["#record-file-source"].value !== source) await inputValue(nodes, "#record-file-source", source, "change");
    await inputValue(nodes, "#record-file-value", value);
    await inputValue(nodes, "#record-file-type", type, "change");
}

test("Record attachment controls send PDF, image and document references in the canonical create request", async () => {
    const requests = [];
    const {app, nodes} = browserHarness(async (_url, options) => {
        requests.push(JSON.parse(options.body)); return jsonResponse(recordQuestion());
    }, {}, "record.js");
    await app.run("record.create");
    assert.equal(Object.hasOwn(requests[0], "fileList"), false);
    await addRecordFile(nodes, " https://example.com/report.pdf ", {type: "pdf"});
    await addRecordFile(nodes, "https://example.com/download?id=2", {type: "image"});
    await addRecordFile(nodes, "document-existing", {source: "documentId", type: "pdf"});
    await addRecordFile(nodes, "https://example.com/scan.png");
    const files = [
        {url: "https://example.com/report.pdf", fileType: "pdf"},
        {url: "https://example.com/download?id=2", fileType: "image"},
        {documentId: "document-existing"},
        {url: "https://example.com/scan.png"},
    ];
    assert.equal(requests.length, 1, "adding an attachment must not send a request");
    assert.equal(nodes["#record-file-count"].textContent, "4 / 5 份");
    assert.deepEqual(plain(app.draft("record.create").parameters(ids).fileList), files);
    await app.run("record.create");
    assert.deepEqual(requests[1], {userId: ids.userId, sessionId: ids.sessionId, fileList: files});
    assert.ok(nodes["#raw-log"].textContent.includes(core.format(requests[1])));
});

test("Record attachments synchronize with fileList edits and retain explicit or invalid JSON until edited", async () => {
    const {app, nodes} = browserHarness(() => { throw new Error("unexpected fetch"); }, {}, "record.js");
    const model = app.draft("record.create");
    const original = {url: "https://example.com/report.pdf", fileType: "pdf", custom: {count: 0, flag: false}};
    app.setParams("record.create", {fileList: [original]});
    assert.match(nodes["#record-file-list"].textContent, /report\.pdf/);
    await addRecordFile(nodes, "document-new", {source: "documentId"});
    assert.deepEqual(plain(model.parameters(ids).fileList[0]), original);
    await control(nodes["#record-file-list"], "移除").emit("click");
    assert.deepEqual(plain(model.parameters(ids).fileList), [{documentId: "document-new"}]);
    await control(nodes["#record-file-list"], "移除").emit("click");
    assert.equal(Object.hasOwn(model.parameters(ids), "fileList"), false);
    app.setParams("record.create", {fileList: []});
    assert.deepEqual(plain(model.parameters(ids).fileList), []);
    app.setParams("record.create", {fileList: null});
    assert.equal(model.parameters(ids).fileList, null);
    assert.equal(nodes["#record-file-add"].disabled, true);
    assert.match(nodes["#record-file-message"].textContent, /原值保持不变/);
    app.setParams("record.create", {fileList: '[{"url":'}, {raw: true});
    await addRecordFile(nodes, "document-do-not-overwrite", {source: "documentId"});
    assert.equal(model.values.fileList, '[{"url":');
    assert.match(nodes["#record-file-message"].textContent, /尚未完整/);
    app.setParams("record.create", {fileList: [null, false, [], {unknown: "<img src=x onerror=alert(1)>"}]});
    assert.equal(descendants(nodes["#record-file-list"], (node) => node.tagName === "img").length, 0);
    await addRecordFile(nodes, "document-new", {source: "documentId"});
    assert.deepEqual(plain(model.parameters(ids).fileList.slice(0, 4)), [null, false, [], {unknown: "<img src=x onerror=alert(1)>"}]);
});

test("Record attachment input keeps partial or invalid URLs visible with feedback and limits new files", async () => {
    const {app, nodes} = browserHarness(() => { throw new Error("unexpected fetch"); }, {}, "record.js");
    const model = app.draft("record.create");
    for (const bad of ["https:", "local.pdf", "file:///tmp/local.pdf", "data:application/pdf;base64,AA==", "https://user:pass@example.com/report.pdf", "https://example.com/report.pdf#page1"]) {
        await inputValue(nodes, "#record-file-value", bad);
        assert.deepEqual(plain(model.parameters(ids).fileList), [{url: bad}], "typed text must not silently disappear from the request");
        assert.ok(nodes["#record-file-message"].textContent.length > 0);
    }
    await inputValue(nodes, "#record-file-value", "");
    assert.equal(model.values.fileList, "");
    await addRecordFile(nodes, "https://example.com/report.pdf");
    await addRecordFile(nodes, "https://example.com/report.pdf");
    assert.match(nodes["#record-file-message"].textContent, /已在附件列表中/);
    assert.equal(model.parameters(ids).fileList.length, 2);
    for (let index = 0; index < 3; index++) await addRecordFile(nodes, `document-${index}`, {source: "documentId"});
    assert.equal(nodes["#record-file-add"].disabled, true);
    assert.equal(nodes["#record-file-value"].disabled, false, "the fifth attachment remains editable");
    await inputValue(nodes, "#record-file-value", "document-fifth-edited");
    assert.equal(model.parameters(ids).fileList[4].documentId, "document-fifth-edited");
    app.setParams("record.create", {fileList: Array.from({length: 5}, (_, index) => ({documentId: `bulk-${index}`}))});
    assert.equal(nodes["#record-file-value"].disabled, true, "bulk imports at the limit cannot append another file");
    const before = model.values.fileList;
    await inputValue(nodes, "#record-file-value", "https://example.com/six.pdf");
    assert.equal(model.values.fileList, before);
    assert.match(nodes["#record-file-message"].textContent, /最多添加 5/);
});

test("typing a Record file URL without clicking add updates JSON and both send buttons carry it", async () => {
    for (const send of ["#api-send", "#action-record.create"]) {
        const requests = [];
        const {nodes} = browserHarness(async (url, options) => {
            requests.push({target: new URL(url).searchParams.get("target"), body: JSON.parse(options.body)});
            return jsonResponse(recordQuestion());
        }, {}, "record.js", true);
        await inputValue(nodes, "#record-file-value", " https://example.com/typed.pdf ");
        assert.deepEqual(JSON.parse(nodes["#api-json"].value).fileList, [{url: "https://example.com/typed.pdf"}]);
        await inputValue(nodes, "#record-file-type", "pdf", "change");
        await inputValue(nodes, "#record-file-value", "https://example.com/replaced.pdf");
        await inputValue(nodes, "#param-name", "附件输入回归");
        assert.equal(nodes["#record-file-value"].value, "https://example.com/replaced.pdf", "unrelated form edits retain the active file");
        const shown = JSON.parse(nodes["#api-json"].value);
        assert.deepEqual(shown.fileList, [{url: "https://example.com/replaced.pdf", fileType: "pdf"}]);
        assert.equal(requests.length, 0);
        await nodes[send].emit("click");
        assert.equal(requests.length, 1);
        assert.equal(requests[0].target, `${ids.backend}/api/v1/qa/create`);
        assert.deepEqual(requests[0].body, shown);
        assert.ok(nodes["#raw-log"].textContent.includes(core.format(shown)));
    }
});

test("Record next-file, editing, source changes, removals and resets keep the correct file in sync", async () => {
    const {app, nodes} = browserHarness(() => { throw new Error("unexpected fetch"); }, {}, "record.js", true);
    const files = () => JSON.parse(nodes["#api-json"].value).fileList;
    await inputValue(nodes, "#record-file-value", "https://example.com/one.pdf");
    await nodes["#record-file-add"].emit("click");
    assert.equal(nodes["#record-file-value"].value, "");
    assert.deepEqual(files(), [{url: "https://example.com/one.pdf"}]);
    await inputValue(nodes, "#record-file-value", "https://example.com/two.png");
    await control(nodes["#record-file-list"], "移除").emit("click");
    await inputValue(nodes, "#record-file-type", "image", "change");
    assert.deepEqual(files(), [{url: "https://example.com/two.png", fileType: "image"}]);
    await inputValue(nodes, "#record-file-type", "", "change");
    assert.deepEqual(files(), [{url: "https://example.com/two.png"}]);
    await inputValue(nodes, "#record-file-source", "documentId", "change");
    assert.equal(files(), undefined);
    await inputValue(nodes, "#record-file-value", "document-reuse");
    assert.deepEqual(files(), [{documentId: "document-reuse"}]);
    const imported = [{url: "https://example.com/import.pdf", custom: {count: 0}}];
    app.setParams("record.create", {fileList: imported});
    assert.equal(nodes["#record-file-value"].value, "");
    await control(nodes["#record-file-list"], "编辑").emit("click");
    await inputValue(nodes, "#record-file-value", "https://example.com/import-edited.pdf");
    assert.deepEqual(files(), [{url: "https://example.com/import-edited.pdf", custom: {count: 0}}]);
    await inputValue(nodes, "#record-file-value", " ");
    assert.equal(files(), undefined);
    await inputValue(nodes, "#record-file-value", "https://example.com/reset.pdf");
    await nodes["#api-reset-form"].emit("click");
    assert.equal(files(), undefined);
    assert.equal(nodes["#record-file-value"].value, "");
    assert.deepEqual(plain(app.context.get()), ids);
});

test("Record full JSON displays its actual attachments and cannot be overwritten by form helpers", async () => {
    let sent;
    const {app, nodes} = browserHarness(async (_url, options) => { sent = JSON.parse(options.body); return jsonResponse({}); }, {}, "record.js");
    const model = app.draft("record.create");
    await addRecordFile(nodes, "document-form", {source: "documentId"});
    model.setMode("json", ids);
    model.json = '{"userId":"manual-user","fileList":[{"documentId":"document-json","extra":false}],"unknown":0}';
    app.setParams("record.create", {});
    assert.match(nodes["#record-file-list"].textContent, /document-json/);
    assert.doesNotMatch(nodes["#record-file-list"].textContent, /document-form/);
    assert.equal(nodes["#record-file-add"].disabled, true);
    const raw = model.json;
    await addRecordFile(nodes, "document-ignored", {source: "documentId"});
    await control(nodes["#record-file-list"], "移除").emit("click");
    assert.equal(model.json, raw);
    await app.run("record.create");
    assert.deepEqual(sent, JSON.parse(raw));
    assert.equal(app.context.get().userId, ids.userId);
    model.setMode("form", ids); app.setParams("record.create", {});
    assert.match(nodes["#record-file-list"].textContent, /document-form/);
    assert.equal(nodes["#record-file-add"].disabled, false);
});

test("Record identity changes clear attachment references and unfinished input while keeping the user for new sessions", async () => {
    const {app, nodes} = browserHarness(() => { throw new Error("unexpected fetch"); }, {}, "record.js");
    await addRecordFile(nodes, "document-old", {source: "documentId"});
    nodes["#record-file-value"].value = "document-unsubmitted";
    app.context.newSession();
    assert.equal(app.context.get().userId, ids.userId);
    assert.notEqual(app.context.get().sessionId, ids.sessionId);
    assert.equal(app.draft("record.create").values.fileList, "");
    assert.equal(nodes["#record-file-count"].textContent, "0 / 5 份");
    assert.equal(nodes["#record-file-value"].value, "");
    assert.equal(nodes["#record-file-source"].value, "url");
    assert.equal(nodes["#record-file-type-field"].hidden, false);
});

async function inputValue(nodes, selector, value, event = "input") {
    nodes[selector].value = value;
    await nodes[selector].emit(event);
}

function parameterChoice(nodes, name, type, value) {
    const group = nodes["#param-" + name];
    assert.ok(group, "missing choice group for " + name);
    const input = descendants(group, (node) => node.tagName === "input" && node.type === type && node.value === value)[0];
    assert.ok(input, "missing " + type + " choice for " + name + "=" + value);
    return input;
}

async function chooseParameter(nodes, name, type, value, checked = true) {
    const input = parameterChoice(nodes, name, type, value);
    assert.equal(Boolean(input.disabled || nodes["#param-" + name].disabled), false);
    if (type === "radio") {
        for (const radio of descendants(nodes["#param-" + name], (node) => node.type === "radio")) {
            radio.checked = radio === input;
        }
    } else input.checked = checked;
    await input.emit("change");
}

test("form defaults are visible but omitted until selected or entered explicitly", async () => {
    let sent;
    const {nodes} = browserHarness(async (_url, options) => {
        sent = JSON.parse(options.body);
        return jsonResponse({});
    }, {endpoints: ["health-profile.get"]}, null, true);
    for (const [value, label] of [
        ["all", "全部（all）"],
        ["abnormal", "临床异常（abnormal）"],
        ["flagged", "来源标记异常（flagged）"],
        ["pending", "事实待核实（pending）"],
        ["unjudged", "判定依据不足（unjudged）"],
    ]) {
        const choice = parameterChoice(nodes, "indicatorScope", "checkbox", value);
        assert.equal(choice.parentElement.textContent, label);
    }
    assert.equal(nodes["#param-indicatorLimit"].value, "");
    assert.equal(nodes["#param-indicatorLimit"].placeholder, "后端默认 12");
    assert.match(nodes["#api-form-controls"].textContent, /后端默认[^a-z]*all/);
    assert.deepEqual(JSON.parse(nodes["#api-json"].value), {userId: ids.userId});

    await inputValue(nodes, "#param-indicatorLimit", "12");
    await chooseParameter(nodes, "includeEvidence", "radio", "false");
    await chooseParameter(nodes, "documentObservationView", "radio", "standard");
    await chooseParameter(nodes, "indicatorScope", "checkbox", "all");
    const expected = {userId: ids.userId, indicatorLimit: 12, includeEvidence: false,
        documentObservationView: "standard", indicatorScope: ["all"]};
    assert.deepEqual(JSON.parse(nodes["#api-json"].value), expected);
    await nodes["#api-send"].emit("click");
    assert.deepEqual(sent, expected);
});

test("visual fact filters omit an empty selection and reset preserves manual JSON", async () => {
    let sent;
    const {app, nodes} = browserHarness(async (_url, options) => {
        sent = JSON.parse(options.body);
        return jsonResponse({});
    }, {endpoints: ["health-profile.get"]}, null, true);
    const preview = () => JSON.parse(nodes["#api-json"].value);
    await chooseParameter(nodes, "indicatorScope", "checkbox", "abnormal");
    await chooseParameter(nodes, "factStatuses", "checkbox", "active");
    await chooseParameter(nodes, "factStatuses", "checkbox", "disputed");
    assert.deepEqual(preview(), {
        userId: ids.userId, factStatuses: ["active", "disputed"], indicatorScope: ["abnormal"],
    });
    await chooseParameter(nodes, "factStatuses", "checkbox", "active", false);
    await chooseParameter(nodes, "factStatuses", "checkbox", "disputed", false);
    assert.deepEqual(preview(), {userId: ids.userId, indicatorScope: ["abnormal"]});

    const raw = {userId: ids.userId, factStatuses: ["disputed"], indicatorLimit: -6};
    await inputValue(nodes, "#api-json", JSON.stringify(raw));
    await nodes["#api-send"].emit("click");
    assert.deepEqual(sent, raw);
    await inputValue(nodes, "#api-mode", "form", "change");
    await chooseParameter(nodes, "factStatuses", "checkbox", "active");
    await inputValue(nodes, "#param-indicatorLimit", "48");
    assert.equal(nodes["#api-reset-form"].textContent, "重置参数");
    await nodes["#api-reset-form"].emit("click");
    assert.deepEqual(preview(), {userId: ids.userId});
    assert.equal(parameterChoice(nodes, "factStatuses", "checkbox", "active").checked, false);
    assert.deepEqual(plain(app.context.get()), ids);
    await inputValue(nodes, "#api-mode", "json", "change");
    assert.deepEqual(preview(), raw);
});

test("Report fileList accepts a pasted URL and sends the exact adjacent JSON", async () => {
    let sent;
    const {nodes} = browserHarness(async (url, options) => {
        sent = {target: new URL(url).searchParams.get("target"), body: JSON.parse(options.body)};
        return jsonResponse({});
    }, {group: "report"}, null, true);
    await inputValue(nodes, "#param-fileList", " https://example.com/report.pdf?signature=a%2Bb%3D&v=1 ");
    const shown = JSON.parse(nodes["#api-json"].value);
    assert.deepEqual(shown, {userId: ids.userId, sessionId: ids.sessionId,
        fileList: [{url: "https://example.com/report.pdf?signature=a%2Bb%3D&v=1", fileType: "pdf"}]});
    assert.equal(nodes["#api-send"].disabled, false);
    await nodes["#api-send"].emit("click");
    assert.deepEqual(sent, {target: `${ids.backend}/api/v1/report`, body: shown});
    assert.ok(nodes["#raw-log"].textContent.includes(core.format(shown)));
});

test("Report URL shorthand recognizes mixed formats and file-service query names without rewriting URLs", () => {
    const model = draft("report.submit");
    const files = [
        {url: "https://example.com/report.PDF", fileType: "pdf"},
        ...["jpg", "jpeg", "png", "bmp", "gif"].map((extension) => ({url: `https://example.com/scan.${extension}`, fileType: "image"})),
        ...["objectName", "fileName", "filename", "name"].map((key) => ({
            url: `https://example.com/preview?${key}=%E6%8A%A5%E5%91%8A.PDF&signature=a%2Bb`, fileType: "pdf",
        })),
    ];
    model.set("fileList", files.map((file) => file.url).join("\r\n\n"), {raw: true});
    assert.deepEqual(plain(model.parameters(ids).fileList), files);
    model.set("fileList", "https://example.com/fallback.pdf?objectName=scan.png", {raw: true});
    assert.equal(model.parameters(ids).fileList[0].fileType, "image");
});

test("Report URLs with unknown types can use the type selector and resets clear the choice", async () => {
    const {app, nodes} = browserHarness(async () => jsonResponse({}), {group: "report"}, null, true);
    await inputValue(nodes, "#param-fileList", "https://example.com/download/pdf");
    assert.equal(nodes["#api-send"].disabled, true);
    assert.match(nodes["#api-json-status"].textContent, /URL 文件类型/);
    for (const fileType of ["image", "pdf"]) {
        await inputValue(nodes, "#param-fileList-url-type", fileType, "change");
        assert.equal(nodes["#api-send"].disabled, false);
        assert.deepEqual(JSON.parse(nodes["#api-json"].value).fileList, [{url: "https://example.com/download/pdf", fileType}]);
    }
    await inputValue(nodes, "#param-fileList", "");
    assert.equal(Object.hasOwn(JSON.parse(nodes["#api-json"].value), "fileList"), false);
    await nodes["#api-reset-form"].emit("click");
    assert.equal(nodes["#param-fileList-url-type"].value, "");
    assert.deepEqual(plain(app.context.get()), ids);
});

test("invalid Report URL shorthand blocks sending and never sends the previous valid preview", async () => {
    let calls = 0;
    const {nodes} = browserHarness(async () => { calls++; return jsonResponse({}); }, {group: "report"}, null, true);
    const invalid = [
        ["https://", /不完整/],
        ["http://example.com/report.pdf", /HTTPS/],
        ["ftp://example.com/report.pdf", /HTTPS/],
        ["https://example.com/report.pdf#page=1", /片段/],
        ["https://user:password@example.com/report.pdf", /账号/],
        ["https://example.com/report with spaces.pdf", /HTTPS/],
        ["https://example.com/report.pdf\nnot-a-url", /第 2 个 URL/],
        [Array.from({length: 11}, (_, index) => `https://example.com/${index}.pdf`).join("\n"), /最多 10/],
    ];
    for (const [value, message] of invalid) {
        await inputValue(nodes, "#param-fileList", "https://example.com/good.pdf");
        await inputValue(nodes, "#param-fileList", value);
        assert.equal(nodes["#param-fileList"].value, value);
        assert.equal(nodes["#api-send"].disabled, true);
        assert.equal(nodes["#api-json"].value, "");
        assert.match(nodes["#api-json-status"].textContent, message);
        await nodes["#api-send"].emit("click");
    }
    assert.equal(calls, 0);
});

test("Report URL convenience leaves explicit arrays, nulls and manual JSON untouched", async () => {
    let sent;
    const {nodes} = browserHarness(async (_url, options) => { sent = JSON.parse(options.body); return jsonResponse({}); }, {group: "report"}, null, true);
    await inputValue(nodes, "#param-fileList-url-type", "pdf", "change");
    for (const files of [null, [], [{documentId: "document-existing", extra: false}, {url: "https://example.com/file.png", fileType: "image", n: 0}]]) {
        await inputValue(nodes, "#param-fileList", JSON.stringify(files));
        assert.deepEqual(JSON.parse(nodes["#api-json"].value).fileList, files);
    }
    await inputValue(nodes, "#param-fileList", '[{"url":');
    assert.match(nodes["#api-json-status"].textContent, /fileList.*JSON/);
    await inputValue(nodes, "#param-fileList", "[]");
    const manual = {fileList: "https://example.com/raw.pdf", userId: "manual", extra: null};
    await inputValue(nodes, "#api-json", JSON.stringify(manual));
    assert.equal(nodes["#api-form-controls"].disabled, true);
    await nodes["#api-send"].emit("click");
    assert.deepEqual(sent, manual);
});

test("mounted parameter forms update the adjacent editable JSON and send the exact displayed values", async () => {
    let sent;
    const {nodes, document} = browserHarness(async (_url, options) => { sent = JSON.parse(options.body); return jsonResponse({}); }, {group: "record"}, null, true);
    assert.notEqual(nodes["#api-json"].hidden, true);
    assert.equal(nodes["#api-form-controls"].disabled, false);
    await inputValue(nodes, "#param-name", "调试建档");
    await inputValue(nodes, "#param-basicInfo", '{"age":0,"confirmed":false,"extra":null}');
    await inputValue(nodes, "#param-fileList", "[]");
    const displayed = JSON.parse(nodes["#api-json"].value);
    assert.deepEqual(displayed, {userId: ids.userId, sessionId: ids.sessionId, name: "调试建档", basicInfo: {age: 0, confirmed: false, extra: null}, fileList: []});
    assert.equal(nodes["#api-preview"].textContent, `POST ${ids.backend}/api/v1/qa/create`);
    assert.equal(nodes["#api-mode"].value, "form");
    await nodes["#api-copy-json"].emit("click");
    assert.equal(document.copied, core.format(displayed));
    await nodes["#api-send"].emit("click");
    assert.deepEqual(sent, displayed);
    assert.ok(nodes["#raw-log"].textContent.includes(core.format(displayed)));
});

test("typing directly into live JSON switches the source and preserves the manual draft across form changes", async () => {
    let sent;
    const {app, nodes} = browserHarness(async (_url, options) => { sent = JSON.parse(options.body); return jsonResponse({}); }, {group: "record"}, null, true);
    const manual = '{"unknown":false,"userId":"manual-user","fileList":null,"n":0}';
    await inputValue(nodes, "#api-json", manual);
    assert.equal(nodes["#api-mode"].value, "json");
    assert.equal(nodes["#api-form-controls"].disabled, true);
    assert.equal(nodes["#api-send"].disabled, false, "raw JSON may omit required fields for backend validation");
    assert.equal(nodes["#api-json"].value, manual);
    await nodes["#api-send"].emit("click");
    assert.deepEqual(sent, JSON.parse(manual));
    assert.equal(app.context.get().userId, ids.userId);
    await inputValue(nodes, "#api-mode", "form", "change");
    await inputValue(nodes, "#param-name", "保留表单");
    assert.equal(JSON.parse(nodes["#api-json"].value).name, "保留表单");
    assert.equal(app.draft("record.create").json, manual);
    await inputValue(nodes, "#api-mode", "json", "change");
    assert.equal(nodes["#api-json"].value, manual);
    await nodes["#api-rebuild"].emit("click");
    assert.deepEqual(JSON.parse(nodes["#api-json"].value), {userId: ids.userId, sessionId: ids.sessionId, name: "保留表单"});
});

test("invalid form or raw JSON is shown next to the editor and never sends a previous valid preview", async () => {
    let calls = 0;
    const {nodes} = browserHarness(async () => { calls++; return jsonResponse({}); }, {group: "record"}, null, true);
    await inputValue(nodes, "#param-fileList", '[{"url":');
    assert.equal(nodes["#api-json"].value, "");
    assert.equal(nodes["#api-json"].disabled, true);
    assert.equal(nodes["#api-send"].disabled, true);
    assert.match(nodes["#api-json-status"].textContent, /fileList.*JSON/);
    await nodes["#api-send"].emit("click");
    assert.equal(calls, 0);
    await inputValue(nodes, "#param-fileList", "[]");
    assert.equal(nodes["#api-json"].disabled, false);
    assert.equal(nodes["#api-send"].disabled, false);
    await inputValue(nodes, "#api-json", "{");
    assert.equal(nodes["#api-json"].value, "{");
    assert.equal(nodes["#api-json"].disabled, false);
    assert.equal(nodes["#api-send"].disabled, true);
    assert.equal(nodes["#api-copy-json"].disabled, true);
    await nodes["#api-send"].emit("click");
    assert.equal(calls, 0);
    await inputValue(nodes, "#api-json", "{}");
    assert.equal(nodes["#api-send"].disabled, false);
    await nodes["#api-send"].emit("click");
    assert.equal(calls, 1);
});

test("GET JSON, URL preview and transmitted query stay in sync without a request body", async () => {
    let sent;
    const {nodes} = browserHarness(async (url, options) => { sent = {url: new URL(url).searchParams.get("target"), options}; return jsonResponse({}); }, {group: "chat"}, null, true);
    await inputValue(nodes, "#param-count", "3");
    const omit = descendants(nodes["#api-fields"], (node) => node.dataset.omit === "userId")[0];
    omit.checked = true; await omit.emit("change");
    assert.deepEqual(JSON.parse(nodes["#api-json"].value), {count: 3});
    const params = {count: 0, tags: ["空 格", "中文"], enabled: false, extra: null};
    await inputValue(nodes, "#api-json", JSON.stringify(params));
    const query = new URLSearchParams({count: "0", tags: JSON.stringify(params.tags), enabled: "false", extra: "null"});
    const target = `${ids.backend}/api/v1/chat/suggestions?${query}`;
    assert.equal(nodes["#api-preview"].textContent, `GET ${target}`);
    assert.match(nodes["#api-json-label"].textContent, /查询参数/);
    await nodes["#api-send"].emit("click");
    assert.equal(sent.url, target);
    assert.equal(sent.options.body, undefined);
});

test("formatting, interface switches and form resets preserve raw JSON values and shared identity", async () => {
    const {app, nodes} = browserHarness(() => { throw new Error("unexpected fetch"); }, {group: "record"}, null, true);
    const raw = {userId: "manual", fileList: [], unknown: {value: false, count: 0, data: null}};
    await inputValue(nodes, "#api-json", JSON.stringify(raw));
    await nodes["#api-format-json"].emit("click");
    assert.equal(nodes["#api-json"].value, core.format(raw));
    await inputValue(nodes, "#api-select", "record.answer.complete", "change");
    assert.equal(nodes["#api-send"].disabled, true);
    assert.match(nodes["#api-json-status"].textContent, /questionId/);
    await inputValue(nodes, "#param-questionId", "q-1");
    await inputValue(nodes, "#param-other", "饭后腹胀");
    await chooseParameter(nodes, "thinking", "radio", "false");
    assert.equal(nodes["#api-send"].disabled, false);
    const expected = nodes["#api-json"].value;
    await inputValue(nodes, "#api-select", "record.answer.stream", "change");
    assert.equal(nodes["#api-json"].value, expected);
    await inputValue(nodes, "#api-select", "record.create", "change");
    assert.equal(nodes["#api-json"].value, core.format(raw));
    await inputValue(nodes, "#api-mode", "form", "change");
    await nodes["#api-reset-form"].emit("click");
    assert.deepEqual(JSON.parse(nodes["#api-json"].value), {userId: ids.userId, sessionId: ids.sessionId});
    assert.deepEqual(plain(app.context.get()), ids);
    assert.equal(app.draft("record.create").json, core.format(raw));
});

test("Record attachment controls immediately update the mounted JSON editor and manual edits update the file display", async () => {
    const {nodes} = browserHarness(() => { throw new Error("unexpected fetch"); }, {}, "record.js", true);
    await addRecordFile(nodes, "https://example.com/report.pdf", {type: "pdf"});
    assert.deepEqual(JSON.parse(nodes["#api-json"].value).fileList, [{url: "https://example.com/report.pdf", fileType: "pdf"}]);
    await inputValue(nodes, "#api-json", '{"fileList":[{"documentId":"manual-reference"}]}');
    assert.match(nodes["#record-file-list"].textContent, /manual-reference/);
    assert.equal(nodes["#record-file-add"].disabled, true);
    await inputValue(nodes, "#api-mode", "form", "change");
    assert.match(nodes["#record-file-list"].textContent, /report\.pdf/);
    await control(nodes["#record-file-list"], "移除").emit("click");
    assert.equal(Object.hasOwn(JSON.parse(nodes["#api-json"].value), "fileList"), false);
});


test("Record creation keeps 202 visible until status is queried manually", async () => {
    const requests = [];
    const {app, nodes, timers} = browserHarness(async (_url, options) => {
        requests.push(JSON.parse(options.body));
        return requests.length < 3
            ? jsonResponse({status: "processing", stage: "processing_reports", retryAfterSeconds: 3}, requests.length === 1 ? 202 : 200)
            : jsonResponse(recordQuestion());
    }, {}, "record.js");
    await app.run("record.create");
    assert.match(nodes["#question-area"].textContent, /正在准备建档/);
    assert.equal(nodes["#submit"].disabled, true);
    assert.match(nodes["#question-area"].textContent, /不会自动刷新.*恢复当前问题.*手动查询/);
    assert.equal(timers.size, 0);
    assert.equal(requests.length, 1);
    await app.run("record.status");
    assert.deepEqual(requests[1], {userId: ids.userId, sessionId: ids.sessionId});
    assert.match(nodes["#question-area"].textContent, /正在准备建档/);
    assert.equal(timers.size, 0);
    assert.equal(requests.length, 2);
    await app.run("record.status");
    assert.deepEqual(requests[2], {userId: ids.userId, sessionId: ids.sessionId});
    assert.equal(nodes["#submit"].disabled, false);
    assert.equal(timers.size, 0);
});

test("Record retryable and terminal creation failures never poll automatically", async () => {
    const {app, nodes, timers} = browserHarness(async () => jsonResponse({
        status: "failed", errorCode: "DOCUMENT_SUBMISSION_UNCONFIRMED", retryable: true, retryAfterSeconds: 300,
    }, 409), {}, "record.js");
    await app.run("record.create");
    assert.match(nodes["#question-area"].textContent, /DOCUMENT_SUBMISSION_UNCONFIRMED/);
    assert.match(nodes["#question-area"].textContent, /手动查询状态/);
    assert.equal(timers.size, 0);
    app.context.update({sessionId: "new-session"});
    assert.equal(timers.size, 0);
    const stopped = browserHarness(async () => jsonResponse({
        status: "failed", errorCode: "DOCUMENT_SUBMISSION_RETRY_EXHAUSTED", retryable: false,
    }, 409), {}, "record.js");
    await stopped.app.run("record.create");
    assert.equal(stopped.timers.size, 0);
    assert.match(stopped.nodes["#question-area"].textContent, /DOCUMENT_SUBMISSION_RETRY_EXHAUSTED/);
});


const questionnaireFixture = (extra = {}) => ({
    questionnaireId: "survey-001", sessionId: ids.sessionId, status: "ready", expiresAt: "2027-01-01T00:00:00Z",
    questionnaire: {title: "测试整卷", pages: [{name: "daily_habits", elements: [
        {type: "text", name: "note", title: "补充说明", description: "先看承接"},
        {type: "radiogroup", name: "exercise", title: "运动情况", choices: [{value: "yes", text: "运动"}, {value: "unknown", text: "不清楚"}]},
        {type: "checkbox", name: "diet", title: "饮食", showNoneItem: true, noneText: "以上均无", choices: [{value: "veg", text: "蔬菜"}, {value: "fish", text: "鱼"}]},
    ]}]},
    reports: [{filename: "报告.pdf", interpretation: {title: "报告解读", processing: {status: "partial"}, observations: []}}],
    guidance: {riskAssessment: [{priority: "常规跟进", assessment: "核实背景", evidence: ["结果1"]}], riskNote: "",
        clientCheckupMessage: "请与医生讨论", managerInterview: [{question: "是否有其他问题？", reason: "补充背景"}]},
    ...extra,
});
function surveyInput(nodes, name, value) {
    return descendants(nodes["#questionnaire-form"], (node) => node.tagName === "input" && node.name === name && (value === undefined || node.value === value))[0];
}
async function surveyPick(nodes, name, value, checked = true) {
    const input = surveyInput(nodes, name, value); assert.ok(input, `${name}:${value}`);
    input.checked = checked; await input.emit("change");
}

test("questionnaire request locations separate encoded identity from the schema answer body", () => {
    const endpoint = byId("questionnaire.submit");
    const request = core.buildRequest(endpoint, {questionnaireId: "卷 /?#", userId: "用户 +&", answers: {}});
    assert.equal(request.path, "/api/v1/record/questionnaires/%E5%8D%B7%20%2F%3F%23/answers?userId=%E7%94%A8%E6%88%B7+%2B%26");
    assert.deepEqual(request.body, {answers: {}});
    assert.throws(() => core.buildRequest(endpoint, {answers: {}}), /questionnaireId/);
    const status = core.buildRequest(byId("questionnaire.status"), {userId: ids.userId, sessionId: ids.sessionId});
    assert.equal(status.method, "GET"); assert.equal(Object.hasOwn(status, "body"), false);
    assert.equal(new URL(status.path, ids.backend).searchParams.get("sessionId"), ids.sessionId);
    const extra = core.buildRequest(endpoint, {questionnaireId: "q", userId: "u", answers: {}, unexpected: false});
    assert.deepEqual(extra.body, {answers: {}, unexpected: false}, "manual invalid payloads must not be silently repaired");
});

test("questionnaire navigation keeps legacy Record and every page uses the shared API Lab shell", () => {
    for (const name of fs.readdirSync(pages).filter((name) => name.endsWith(".html"))) {
        const html = fs.readFileSync(path.join(pages, name), "utf8");
        assert.match(html, /href="\.\/record-preview\.html"/, name);
        assert.match(html, /href="\.\/record\.html"/, name);
    }
    const html = fs.readFileSync(path.join(pages, "record-preview.html"), "utf8");
    assert.doesNotMatch(html, /type="file"|drop-zone/);
    assert.match(html, /id="api-editor"/); assert.match(html, /href="\.\/chat\.html"/);
    assert.match(fs.readFileSync(path.join(pages, "index.html"), "utf8"), /ApiLabContracts\.length/);
});

test("API Lab home groups interface cards by workflow in the requested order", () => {
    const home = fs.readFileSync(path.join(pages, "index.html"), "utf8");
    const groups = Array.from(home.matchAll(/<section class="api-entry-group">\s*<h3>([^<]+)<\/h3>([\s\S]*?)<\/section>/g),
        ([, category, contents]) => [category, Array.from(contents.matchAll(/<a class="api-card" href="\.\/([^"]+)"><strong>([^<]+)<\/strong>/g),
            ([, href, title]) => [href, title])]);
    assert.deepEqual(groups, [
        ["服务检查", [["health.html", "健康检查"]]],
        ["信息采集与分析", [
            ["interpret.html", "报告解读"],
            ["record.html", "建档问答"],
            ["chat.html", "智能对话"],
            ["record-preview.html", "整卷问卷"],
            ["health-analysis.html", "健康分析"],
        ]],
        ["最终报告", [["report.html", "最终报告"]]],
        ["健康画像", [["health-profile.html", "健康画像"], ["health-profile-view.html", "画像展示"]]],
    ]);
});

test("API Lab navigation keeps the same bilingual labels and order on every page", () => {
    const expected = [
        ["index.html", "全部接口", undefined],
        ["health.html", "健康检查", "Health Check"],
        ["interpret.html", "报告解读", "Analysis"],
        ["record.html", "建档问答", "Record"],
        ["chat.html", "智能对话", "Chat"],
        ["record-preview.html", "整卷问卷", "Questionnaire"],
        ["health-analysis.html", "健康分析", "Health Analysis"],
        ["report.html", "最终报告", "Report"],
        ["health-profile.html", "健康画像", "Profile"],
        ["health-profile-view.html", "画像展示", "Profile View"],
    ];
    for (const [name] of expected) {
        const html = fs.readFileSync(path.join(pages, name), "utf8");
        const navigation = html.match(/<nav class="nav-links lab-nav"[^>]*>([\s\S]*?)<\/nav>/)?.[1];
        assert.ok(navigation, name);
        const items = Array.from(navigation.matchAll(/<a\b[^>]* href="\.\/([^"]+)"><span>([^<]+)<\/span>(?:<small lang="en">([^<]+)<\/small>)?<\/a>/g),
            ([, href, primary, secondary]) => [href, primary, secondary]);
        assert.deepEqual(items, expected, name);
    }
});

test("API Lab pages expose valid section targets and keep primary responses visible", () => {
    const foldedResponses = new Set([
        "health-analysis.html", "health-profile-view.html", "interpret.html", "record-preview.html",
    ]);
    const readId = (attributes) => attributes.match(/\sid="([^"]+)"/)?.[1];
    const hasSection = (attributes) => /\sdata-lab-section(?:\s|=|$)/.test(attributes);
    const hasAttribute = (attributes, name) => new RegExp("\\s" + name + "(?:\\s|=|$)").test(attributes);
    for (const name of fs.readdirSync(pages).filter((name) => name.endsWith(".html"))) {
        const html = fs.readFileSync(path.join(pages, name), "utf8");
        const tags = Array.from(html.matchAll(/<([a-z][\w-]*)\b([^<>]*)>/gi), ([, tag, attributes]) => ({
            tag, attributes, id: readId(attributes),
        }));
        const active = tags.filter(({tag, attributes}) => tag === "a"
            && attributes.match(/\sclass="([^"]+)"/)?.[1].split(/\s+/).includes("active"));
        assert.equal(active.length, 1, name + " active navigation link");
        assert.match(active[0].attributes, /\saria-current="page"/, name);
        assert.ok(active[0].attributes.includes('href="./' + name + '"'), name);
        const sections = tags.filter(({attributes}) => hasSection(attributes));
        assert.ok(sections.length >= 2, name + " page directory");
        for (const {id} of sections) {
            assert.match(id ?? "", /^[a-z][\w-]*$/, name + " section ID");
            assert.equal(tags.filter((tag) => tag.id === id).length, 1, name + " duplicate section " + id);
        }
        if (name === "index.html") continue;
        const response = sections.find(({id}) => id === "lab-response");
        const log = sections.find(({id}) => id === "lab-network-log");
        assert.equal(response?.tag, foldedResponses.has(name) ? "details" : "section", name + " response");
        assert.ok(!hasAttribute(response.attributes, "open"), name + " response expanded");
        assert.ok(!hasAttribute(response.attributes, "hidden"), name + " response hidden");
        assert.equal(log?.tag, "details", name + " network log");
        assert.ok(!hasAttribute(log.attributes, "open"), name + " network log expanded");
    }
});

test("questionnaire ready state renders all three types with no selected unknown or none", async () => {
    const {app, nodes} = browserHarness(async () => jsonResponse(questionnaireFixture()), {}, "record-preview.js");
    await app.run("questionnaire.status");
    const inputs = descendants(nodes["#questionnaire-form"], (node) => node.tagName === "input");
    assert.equal(inputs.length, 6); assert.ok(inputs.every((input) => !input.checked));
    assert.deepEqual(plain(app.draft("questionnaire.submit").parameters(ids).answers), {});
    assert.equal(nodes["#questionnaire-submit"].disabled, false);
    assert.equal(nodes["#questionnaire-chat"].hidden, true);
    assert.match(nodes["#questionnaire-form"].textContent, /先看承接.*补充说明/);
    assert.match(nodes["#questionnaire-risk"].textContent, /核实背景/);
    assert.match(nodes["#questionnaire-interview"].textContent, /其他问题/);
});

test("questionnaire choices, clear, text trimming and none remain synchronized with canonical answers", async () => {
    const {app, nodes} = browserHarness(async () => jsonResponse(questionnaireFixture()), {}, "record-preview.js", true);
    await app.run("questionnaire.status");
    const note = surveyInput(nodes, "note"); note.value = "  补充  "; await note.emit("input");
    await surveyPick(nodes, "exercise", "yes");
    await surveyPick(nodes, "diet", "fish"); await surveyPick(nodes, "diet", "veg");
    let answers = plain(app.draft("questionnaire.submit").parameters(ids).answers);
    assert.deepEqual(answers, {note: "补充", exercise: "yes", diet: ["veg", "fish"]});
    await surveyPick(nodes, "diet", "none");
    assert.deepEqual(plain(app.draft("questionnaire.submit").parameters(ids).answers.diet), ["none"]);
    assert.equal(surveyInput(nodes, "diet", "veg").checked, false);
    await surveyPick(nodes, "diet", "fish");
    assert.equal(surveyInput(nodes, "diet", "none").checked, false);
    await surveyPick(nodes, "diet", "fish", false);
    await control(nodes["#questionnaire-form"], "清除此题").emit("click");
    note.value = "   "; await note.emit("input");
    assert.deepEqual(plain(app.draft("questionnaire.submit").parameters(ids).answers), {});
});

test("questionnaire empty answers submit only answers and retain the same identity for Chat", async () => {
    const requests = [];
    const {app, nodes} = browserHarness(async (url, options) => {
        requests.push({target: new URL(url).searchParams.get("target"), options});
        return jsonResponse(questionnaireFixture(requests.length > 1 ? {status: "submitted", answers: {}} : {}));
    }, {}, "record-preview.js", true);
    await app.run("questionnaire.status"); await nodes["#questionnaire-submit"].emit("click");
    assert.deepEqual(JSON.parse(requests[1].options.body), {answers: {}});
    assert.equal(requests[1].target, `${ids.backend}/api/v1/record/questionnaires/survey-001/answers?userId=${ids.userId}`);
    assert.deepEqual(plain(app.context.get()), ids);
    assert.equal(nodes["#questionnaire-chat"].hidden, false); assert.equal(nodes["#questionnaire-chat"].href, "./chat.html");
    assert.equal(nodes["#questionnaire-submit"].disabled, true);
    assert.ok(descendants(nodes["#questionnaire-form"], (node) => node.tagName === "input").every((node) => node.disabled));
});

test("questionnaire generation sends references once and polls the production GET until ready", async () => {
    const requests = [];
    const {app, nodes, timers} = browserHarness(async (url, options) => {
        requests.push({url, options});
        return jsonResponse(requests.length === 1 ? questionnaireFixture({status: "queued", questionnaire: undefined}) : questionnaireFixture(), requests.length === 1 ? 202 : 200);
    }, {}, "record-preview.js", true);
    await inputValue(nodes, "#questionnaire-file-value", "https://example.com/report.pdf");
    await inputValue(nodes, "#questionnaire-file-type", "pdf", "change");
    await nodes["#questionnaire-generate"].emit("click");
    assert.deepEqual(JSON.parse(requests[0].options.body), {userId: ids.userId, sessionId: ids.sessionId, fileList: [{url: "https://example.com/report.pdf", fileType: "pdf"}]});
    assert.equal(nodes["#questionnaire-submit"].disabled, true);
    tickViews(timers, 2000); await until(() => nodes["#questionnaire-submit"].disabled === false);
    assert.equal(requests.length, 2); assert.equal(requests[1].options.method, "GET");
    assert.equal(Object.hasOwn(requests[1].options, "body"), false);
    assert.ok(requests.every((request) => !request.url.includes("interpret") && !request.url.includes("%2Fpreview")));
    assert.equal([...timers.values()].filter((timer) => timer.delay === 2000).length, 0);
});

test("questionnaire identity changes stop polling, clear attachments and reject late responses", async () => {
    const wait = deferred(); let calls = 0;
    const {app, nodes, timers} = browserHarness(async () => {
        calls++; return calls === 1 ? jsonResponse(questionnaireFixture({status: "generating", questionnaire: undefined})) : wait.promise;
    }, {}, "record-preview.js");
    app.setParams("questionnaire.create", {fileList: [{documentId: "doc-owned"}]});
    await app.run("questionnaire.create"); tickViews(timers, 2000); await until(() => calls === 2);
    app.context.newSession(); wait.resolve(jsonResponse(questionnaireFixture()));
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.match(nodes["#questionnaire-status"].textContent, /已切换/);
    assert.doesNotMatch(nodes["#questionnaire-form"].textContent, /测试整卷/);
    assert.equal(app.draft("questionnaire.create").values.fileList, "");
    assert.equal(nodes["#questionnaire-chat"].hidden, true);
    assert.equal([...timers.values()].filter((timer) => timer.delay === 2000).length, 0);
});

test("questionnaire submitted recovery restores server answers and downloads raw schema artifacts", async () => {
    const saved = questionnaireFixture({status: "submitted", answers: {note: "已保存", diet: ["none"]}});
    const {app, nodes, document} = browserHarness(async () => jsonResponse(saved), {}, "record-preview.js");
    await app.run("questionnaire.status");
    assert.equal(surveyInput(nodes, "note").value, "已保存"); assert.equal(surveyInput(nodes, "diet", "none").checked, true);
    const downloads = [["下载问卷 JSON", saved.questionnaire], ["下载答案 JSON", {answers: saved.answers}], ["下载指导 JSON", saved.guidance], ["下载全部报告 JSON", saved.reports]];
    for (const [label, expected] of downloads) {
        await control(nodes["#questionnaire-downloads"], label).emit("click");
        const actual = await (await fetch(document.clicked.href)).json(); URL.revokeObjectURL(document.clicked.href);
        assert.deepEqual(actual, expected); assert.equal(Object.hasOwn(actual, "code"), false);
    }
});

test("questionnaire manual JSON stays authoritative and failed submissions retain form answers", async () => {
    let reply = jsonResponse(questionnaireFixture()); const requests = [];
    const {app, nodes} = browserHarness(async (_url, options) => { requests.push(options); return reply; }, {}, "record-preview.js");
    await app.run("questionnaire.status"); await surveyPick(nodes, "exercise", "yes");
    reply = new Response(JSON.stringify({code: 409, message: "conflict"}), {status: 409});
    await app.run("questionnaire.submit");
    assert.deepEqual(plain(app.draft("questionnaire.submit").parameters(ids).answers), {exercise: "yes"});
    assert.match(nodes["#questionnaire-status"].textContent, /答案已保留/);
    const model = app.draft("questionnaire.submit"); model.setMode("json", ids);
    model.json = JSON.stringify({userId: ids.userId, questionnaireId: "survey-001", answers: {exercise: "unknown"}, extra: false});
    app.setParams("questionnaire.submit", {});
    assert.equal(surveyInput(nodes, "exercise", "yes").disabled, true);
    await surveyPick(nodes, "exercise", "yes");
    await app.run("questionnaire.submit");
    assert.deepEqual(JSON.parse(requests.at(-1).body), {answers: {exercise: "unknown"}, extra: false});
    model.json = "{"; app.setParams("questionnaire.submit", {});
    assert.equal(nodes["#questionnaire-submit"].disabled, true);
    const count = requests.length; await app.run("questionnaire.submit"); assert.equal(requests.length, count);
});

test("questionnaire expired and failed states stop polling and never enable Chat", async () => {
    for (const status of ["failed", "expired"]) {
        const {app, nodes, timers} = browserHarness(async () => jsonResponse(questionnaireFixture({status})), {}, "record-preview.js");
        await app.run("questionnaire.status");
        assert.equal(nodes["#questionnaire-submit"].disabled, true); assert.equal(nodes["#questionnaire-chat"].hidden, true);
        assert.equal([...timers.values()].filter((timer) => timer.delay === 2000).length, 0);
    }
});


test("questionnaire automatic recovery never follows a manually overridden status identity", async () => {
    const requests = [];
    const {app, nodes, timers} = browserHarness(async (url, options) => {
        requests.push({url, options});
        return jsonResponse(requests.length === 1 ? questionnaireFixture({status: "generating", questionnaire: undefined}) : questionnaireFixture());
    }, {}, "record-preview.js");
    await app.run("questionnaire.create");
    const model = app.draft("questionnaire.status"); model.setMode("json", ids);
    model.json = '{"userId":"other-user","sessionId":"other-session"}';
    tickViews(timers, 2000); await until(() => nodes["#questionnaire-submit"].disabled === false);
    const target = new URL(new URL(requests[1].url).searchParams.get("target"));
    assert.equal(target.searchParams.get("userId"), ids.userId);
    assert.equal(target.searchParams.get("sessionId"), ids.sessionId);
    assert.equal(model.json, '{"userId":"other-user","sessionId":"other-session"}');
});


test("questionnaire retryable failures follow server retry delay until ready", async () => {
    let calls = 0;
    const {app, nodes, timers} = browserHarness(async () => jsonResponse(questionnaireFixture(++calls === 1
        ? {status: "failed", retryable: true, retryAfterSeconds: 15, questionnaire: undefined} : {})), {}, "record-preview.js");
    await app.run("questionnaire.create");
    assert.match(nodes["#questionnaire-status"].textContent, /15 秒.*后台重试/);
    assert.equal([...timers.values()].filter((timer) => timer.delay === 15000).length, 1);
    tickViews(timers, 15000); await until(() => nodes["#questionnaire-submit"].disabled === false);
    assert.equal(calls, 2);
    assert.equal([...timers.values()].filter((timer) => timer.delay === 15000).length, 0);
});

test("questionnaire accepted submissions lock answers and poll to the saved receipt", async () => {
    let calls = 0;
    const {app, nodes, timers} = browserHarness(async () => jsonResponse(questionnaireFixture(++calls === 1
        ? {submissionPending: true, retryAfterSeconds: 3}
        : {status: "submitted", submissionPending: false, answers: {exercise: "yes"}})), {}, "record-preview.js");
    await app.run("questionnaire.status");
    assert.equal(nodes["#questionnaire-submit"].disabled, true);
    assert.equal(nodes["#questionnaire-chat"].hidden, true);
    assert.ok(descendants(nodes["#questionnaire-form"], (node) => node.tagName === "input").every((node) => node.disabled));
    await surveyPick(nodes, "exercise", "unknown");
    assert.deepEqual(plain(app.draft("questionnaire.submit").parameters(ids).answers), {});
    assert.match(nodes["#questionnaire-mode"].textContent, /服务器受理/);
    await app.run("questionnaire.submit"); assert.equal(calls, 1, "accepted submission cannot be sent again");
    tickViews(timers, 3000); await until(() => nodes["#questionnaire-chat"].hidden === false);
    assert.equal(surveyInput(nodes, "exercise", "yes").checked, true);
    assert.equal(calls, 3, "saved receipt independently starts the factor assessment read");
});


test("questionnaire terminal snapshot removes previously visible expired material", async () => {
    let reply = questionnaireFixture();
    const {app, nodes} = browserHarness(async () => jsonResponse(reply), {}, "record-preview.js");
    await app.run("questionnaire.status");
    assert.match(nodes["#questionnaire-form"].textContent, /测试整卷/);
    reply = {questionnaireId: "survey-001", sessionId: ids.sessionId, status: "expired"};
    await app.run("questionnaire.status");
    assert.doesNotMatch(nodes["#questionnaire-form"].textContent, /测试整卷/);
    assert.equal(nodes["#questionnaire-downloads"].textContent, "");
    assert.equal(nodes["#questionnaire-risk"].hidden, true);
    assert.doesNotMatch(nodes["#questionnaire-status"].textContent, /有效期/);
});


test("questionnaire report tables preserve root and section results, dates and raw downloads", async () => {
    const first = {itemName: "空腹血糖", resultValue: 6.4, unit: "mmol/L", referenceRange: "3.9–6.1", flag: "偏高", observedAt: "2026-11-01"};
    const result = {title: "年度体检", reportTime: "2026-11-03", observations: [first], sections: [
        {title: "血液检查", reportTime: "2026-11-02", observations: [first,
            {itemName: "未检出指标", resultValue: 0, unit: "mg/L", referenceRange: "0–1"}]},
    ]};
    const saved = questionnaireFixture({reports: [{fileName: "体检.pdf", interpretation: result}]});
    const {app, nodes, document} = browserHarness(async () => jsonResponse(saved), {}, "record-preview.js");
    await app.run("questionnaire.status");
    const host = nodes["#questionnaire-reports"];
    const tables = descendants(host, (node) => node.tagName === "table");
    assert.equal(tables.length, 1);
    const body = descendants(tables[0], (node) => node.tagName === "tbody")[0];
    assert.deepEqual(body.children.map((row) => row.children.map((cell) => cell.textContent)), [
        ["2026-11-01", "空腹血糖", "6.4 mmol/L", "3.9–6.1", "偏高"],
        ["2026-11-02", "未检出指标", "0 mg/L", "0–1", "—"],
    ]);
    assert.equal(descendants(body, hasClass("survey-report-abnormal")).length, 1);
    assert.match(host.textContent, /报告时间：2026-11-03/);
    const raw = descendants(host, hasClass("survey-report-raw"))[0];
    assert.equal(raw.tagName, "details"); assert.ok(!raw.open);
    assert.match(raw.textContent, /查看原始报告 JSON/);
    await control(host, "下载报告 JSON").emit("click");
    assert.deepEqual(await (await fetch(document.clicked.href)).json(), result);
    URL.revokeObjectURL(document.clicked.href);
});

test("questionnaire report presentation includes source imaging text from root and sections safely", async () => {
    const report = {interpretation: {title: "影像检查", reportType: "imaging", reportTime: "2026-11-02",
        expansion: {findings: "胆囊壁毛糙"}, sections: [
            {title: "腹部超声", reportType: "imaging", expansion: {findings: "胆囊壁毛糙", diagnosis: "原文 <img src=x onerror=alert(1)> 待复核"}},
        ]}};
    const {app, nodes} = browserHarness(async () => jsonResponse(questionnaireFixture({reports: [report]})), {}, "record-preview.js");
    await app.run("questionnaire.status");
    const host = nodes["#questionnaire-reports"];
    const blocks = descendants(host, hasClass("survey-imaging"));
    assert.equal(blocks.length, 2);
    assert.match(blocks[0].textContent, /影像所见.*检查时间：2026-11-02.*胆囊壁毛糙/);
    assert.match(blocks[1].textContent, /腹部超声.*影像诊断.*原文 <img src=x onerror=alert\(1\)> 待复核/);
    assert.equal(descendants(host, (node) => node.tagName === "img").length, 0);
    assert.doesNotMatch(host.textContent, /暂未获得可展示/);
});

test("questionnaire risk basis is shown only for distinct explicit source text", async () => {
    let risk = {priority: "常规跟进", assessment: "核实背景"};
    const {app, nodes} = browserHarness(async () => jsonResponse(questionnaireFixture({guidance: {riskAssessment: [risk]}})), {}, "record-preview.js");
    await app.run("questionnaire.status");
    assert.doesNotMatch(nodes["#questionnaire-risk"].textContent, /报告依据/);
    risk = {...risk, evidence: [" 核实背景 ", ""], basis: "核实 背景"};
    await app.run("questionnaire.status");
    assert.doesNotMatch(nodes["#questionnaire-risk"].textContent, /报告依据/);
    risk = {...risk, basis: "报告血糖 6.4 mmol/L"};
    await app.run("questionnaire.status");
    assert.match(nodes["#questionnaire-risk"].textContent, /报告依据：报告血糖 6.4 mmol\/L/);
});


test("questionnaire risk notes hide only repeated assessment text and retain separate guidance unchanged", async () => {
    const risks = [{priority: "常规跟进", assessment: "血糖变化需要核实"}, {priority: "优先跟进", assessment: "复查时间待确认"}];
    let riskNote = "常规跟进：血糖变化需要核实；优先跟进：复查时间待确认";
    const {app, nodes, document} = browserHarness(async () => jsonResponse(questionnaireFixture({guidance: {riskAssessment: risks, riskNote}})), {}, "record-preview.js");
    for (const repeated of [riskNote, "血糖变化需要核实；复查时间待确认", "血糖变化需要核实", " 常规跟进: 血糖变化需要核实 "]) {
        riskNote = repeated;
        await app.run("questionnaire.status");
        const hints = descendants(nodes["#questionnaire-risk"], (node) => node.tagName === "p" && hasClass("hint")(node));
        assert.equal(hints.length, 0, `no duplicate note for ${repeated}`);
    }
    riskNote = "如有新出现的不适，请及时咨询医生。";
    await app.run("questionnaire.status");
    assert.match(nodes["#questionnaire-risk"].textContent, /如有新出现的不适，请及时咨询医生/);
    await control(nodes["#questionnaire-downloads"], "下载指导 JSON").emit("click");
    assert.deepEqual(await (await fetch(document.clicked.href)).json(), {riskAssessment: risks, riskNote});
    URL.revokeObjectURL(document.clicked.href);
    risks.length = 0;
    await app.run("questionnaire.status");
    assert.match(nodes["#questionnaire-risk"].textContent, /如有新出现的不适，请及时咨询医生/);
});

const factorFixture = (extra = {}) => ({questionnaireId: "survey-001", sessionId: ids.sessionId,
    status: "completed", result: {version: "synthetic-v1", summary: "合成资料下的有限关联", targets: [{id: "target-1", label: "合成报告目标",
        reportEvidence: [{id: "report-1", text: "原报告记录 <img src=x onerror=alert(1)>"}],
        riskInterpretation: [{text: "AI 风险解释", evidenceIds: ["report-1"], phase: "post_answer"}],
        candidateFactors: [{id: "factor-1", concept: {id: "synthetic-factor", label: "合成候选"}, relationType: "risk_factor",
            knowledgeEvidence: [{sourceId: "source-1", quote: "合成医学来源原文", locator: "条款 1"}],
            conditions: [{id: "condition-1", label: "合成适用条件", required: true}],
            before: {presence: "unknown", conditionAssessments: []},
            after: {presence: "present", applicability: "matched", conditionAssessments: [{conditionId: "condition-1", status: "met", answerRefs: ["answer-1"],
                support: [{evidenceId: "answer-1", quote: "合成实际回答", kind: "answer"}], contradicts: [], explanation: "实际回答提供支持"}]},
            answerEvidence: [{id: "answer-1", questionName: "note", question: {name: "note", title: "合成实际问题？", type: "text"},
                rawAnswer: "合成实际回答", answer: "合成实际回答", answerText: "合成实际回答", status: "answered"}],
            conclusion: "关联成立范围有限"}],
        factors: [{id: "factor-1", concept: {label: "合成最终因素"}, relationType: "risk_factor", conclusion: "不证明个人因果"}],
        evidenceGaps: ["尚缺时间信息"]}], sources: [{id: "source-1", title: "合成来源", version: "v1", passages: [{quote: "合成医学来源原文"}]}],
        coverage: {limitations: ["仅合成数据"]}}, ...extra});
const requestTarget = (url) => new URL(new URL(url).searchParams.get("target"));

test("factor assessment GET encodes identity, sends no body and documents read-only semantics", () => {
    const endpoint = byId("questionnaire.factor-assessment");
    const request = core.buildRequest(endpoint, {questionnaireId: "卷 /?#", userId: "用户 +&"});
    assert.equal(request.method, "GET"); assert.equal(Object.hasOwn(request, "body"), false);
    assert.equal(request.path, "/api/v1/record/questionnaires/%E5%8D%B7%20%2F%3F%23/factor-assessment?userId=%E7%94%A8%E6%88%B7+%2B%26");
    assert.match(endpoint.hint, /不触发生成/);
});

test("questionnaire submission unlocks Chat before an independent factor read completes", async () => {
    const waiting = deferred(); const requests = [];
    const {app, nodes} = browserHarness(async (url, options) => {
        const target = requestTarget(url); requests.push({target, options});
        if (target.pathname.endsWith("/factor-assessment")) return waiting.promise;
        return jsonResponse(questionnaireFixture(options.method === "POST" ? {status: "submitted", answers: {}} : {}));
    }, {}, "record-preview.js");
    await app.run("questionnaire.status"); await nodes["#questionnaire-submit"].emit("click");
    assert.equal(nodes["#questionnaire-chat"].hidden, false);
    assert.equal(nodes["#questionnaire-submit"].disabled, true);
    assert.match(nodes["#questionnaire-status"].textContent, /答案已提交/);
    assert.match(nodes["#questionnaire-factors"].textContent, /正在查询/);
    assert.equal(requests.at(-1).options.method, "GET");
    assert.equal(Object.hasOwn(requests.at(-1).options, "body"), false);
    waiting.resolve(jsonResponse(factorFixture()));
    await until(() => nodes["#questionnaire-factors"].textContent.includes("因素分析已完成"));
});

test("restored submitted questionnaire polls factor lifecycle and downloads only the raw result", async () => {
    const final = factorFixture(); const requests = []; let factors = 0;
    const {app, nodes, timers, document} = browserHarness(async (url, options) => {
        requests.push({target: requestTarget(url), options});
        if (!requestTarget(url).pathname.endsWith("/factor-assessment")) return jsonResponse(questionnaireFixture({status: "submitted", answers: {note: "合成实际回答"}}));
        factors++;
        return jsonResponse(factors < 3 ? factorFixture({status: factors === 1 ? "queued" : "running", result: null}) : final, factors < 3 ? 202 : 200);
    }, {}, "record-preview.js");
    await app.run("questionnaire.status");
    await until(() => nodes["#questionnaire-factors"].textContent.includes("已排队"));
    tickViews(timers, 2000); await until(() => nodes["#questionnaire-factors"].textContent.includes("正在进行"));
    tickViews(timers, 2000); await until(() => nodes["#questionnaire-factors"].textContent.includes("已完成"));
    const host = nodes["#questionnaire-factors"];
    for (const content of ["1. 报告目标与风险解读", "原报告证据", "AI 风险解读", "2. 初步候选与医学来源", "3. 最终相关因素及关系", "4. 实际问答如何支持或反对", "合成实际问题？", "合成实际回答", "回答状态：已回答", "支持证据", "反对证据", "医学来源原文与版本"]) assert.ok(host.textContent.includes(content), content);
    assert.equal(descendants(host, (node) => ["img", "script"].includes(node.tagName)).length, 0);
    assert.doesNotMatch(host.textContent, /待审核|待确认/);
    assert.equal([...timers.values()].filter((timer) => timer.delay === 2000).length, 0);
    await control(host, "下载因素分析 JSON").emit("click");
    const result = await (await fetch(document.clicked.href)).json(); URL.revokeObjectURL(document.clicked.href);
    assert.deepEqual(result, final.result); assert.equal(Object.hasOwn(result, "data"), false);
    assert.ok(requests.every(({options}) => options.method === "GET"));
});

test("factor technical failures and unavailable results stop polling and allow read-only requery", async () => {
    for (const first of [new Response("unavailable", {status: 503}), jsonResponse(factorFixture({status: "failed", result: null, errorCode: "SYNTHETIC_GENERATION_FAILED"})),
        jsonResponse(factorFixture({status: "unavailable", result: null}))]) {
        let calls = 0; const requests = [];
        const {app, nodes, timers} = browserHarness(async (url, options) => {
            if (!requestTarget(url).pathname.endsWith("/factor-assessment")) return jsonResponse(questionnaireFixture({status: "submitted"}));
            requests.push(options); return ++calls === 1 ? first : jsonResponse(factorFixture());
        }, {}, "record-preview.js");
        await app.run("questionnaire.status");
        await until(() => /技术失败|暂无可用/.test(nodes["#questionnaire-factors"].textContent));
        assert.equal(nodes["#questionnaire-chat"].hidden, false);
        assert.match(nodes["#questionnaire-status"].textContent, /答案已提交/);
        assert.equal([...timers.values()].filter((timer) => timer.delay === 2000).length, 0);
        await control(nodes["#questionnaire-factors"], "重新查询分析").emit("click");
        assert.match(nodes["#questionnaire-factors"].textContent, /因素分析已完成/);
        assert.equal(calls, 2); assert.ok(requests.every((request) => request.method === "GET"));
    }
});

test("factor polling and late responses are discarded for user, session and backend changes", async () => {
    for (const change of [{userId: "other-user"}, {sessionId: "other-session"}, {backend: "https://other.example.invalid"}]) {
        const waiting = deferred(); let calls = 0;
        const {app, nodes, timers} = browserHarness(async (url) => {
            if (!requestTarget(url).pathname.endsWith("/factor-assessment")) return jsonResponse(questionnaireFixture({status: "submitted"}));
            return ++calls === 1 ? jsonResponse(factorFixture({status: "queued", result: null}), 202) : waiting.promise;
        }, {}, "record-preview.js");
        await app.run("questionnaire.status"); await until(() => nodes["#questionnaire-factors"].textContent.includes("已排队"));
        tickViews(timers, 2000); await until(() => calls === 2);
        app.context.update(change); waiting.resolve(jsonResponse(factorFixture()));
        await new Promise((resolve) => setTimeout(resolve, 10));
        assert.equal(nodes["#questionnaire-factors"].hidden, true);
        assert.equal(nodes["#questionnaire-factors"].textContent, "");
        assert.equal([...timers.values()].filter((timer) => timer.delay === 2000).length, 0);
        assert.equal(nodes["#questionnaire-chat"].hidden, true);
    }
});

test("factor response for an earlier questionnaire cannot overwrite the current same-session view", async () => {
    const waiting = deferred(); let questionnaireId = "survey-001";
    const {app, nodes} = browserHarness(async (url) => {
        const target = requestTarget(url);
        if (!target.pathname.endsWith("/factor-assessment")) return jsonResponse(questionnaireFixture({questionnaireId, status: "submitted"}));
        return target.pathname.includes("survey-001") ? waiting.promise : jsonResponse(factorFixture({questionnaireId: "survey-002", status: "unavailable", result: null}));
    }, {}, "record-preview.js");
    await app.run("questionnaire.status"); questionnaireId = "survey-002"; await app.run("questionnaire.status");
    await until(() => nodes["#questionnaire-factors"].textContent.includes("暂无可用"));
    waiting.resolve(jsonResponse(factorFixture())); await new Promise((resolve) => setTimeout(resolve, 10));
    assert.match(nodes["#questionnaire-factors"].textContent, /暂无可用/);
    assert.doesNotMatch(nodes["#questionnaire-factors"].textContent, /合成报告目标/);
});

test("factor identity mismatches and invalid completed payloads never become medical conclusions", async () => {
    for (const result of [factorFixture({sessionId: "wrong-session"}), factorFixture({questionnaireId: "wrong-questionnaire"}), factorFixture({result: null})]) {
        const {app, nodes, timers} = browserHarness(async (url) => jsonResponse(requestTarget(url).pathname.endsWith("/factor-assessment")
            ? result : questionnaireFixture({status: "submitted"})), {}, "record-preview.js");
        await app.run("questionnaire.status"); await until(() => nodes["#questionnaire-factors"].textContent.includes("技术失败"));
        assert.doesNotMatch(nodes["#questionnaire-factors"].textContent, /合成报告目标/);
        assert.equal(control(nodes["#questionnaire-factors"], "下载因素分析 JSON"), undefined);
        assert.equal(nodes["#questionnaire-chat"].hidden, false);
        assert.equal([...timers.values()].filter((timer) => timer.delay === 2000).length, 0);
    }
});

test("factor automatic queries ignore manual identity drafts and editor queries do not replace questionnaire status", async () => {
    const requests = []; let reads = 0;
    const {app, nodes, timers} = browserHarness(async (url) => {
        const target = requestTarget(url); requests.push(target);
        if (!target.pathname.endsWith("/factor-assessment")) return jsonResponse(questionnaireFixture({status: "submitted"}));
        return jsonResponse(++reads === 1 ? factorFixture({status: "queued", result: null}) : factorFixture());
    }, {}, "record-preview.js");
    await app.run("questionnaire.status"); await until(() => nodes["#questionnaire-factors"].textContent.includes("已排队"));
    const model = app.draft("questionnaire.factor-assessment"); model.setMode("json", ids);
    model.json = '{"questionnaireId":"other-questionnaire","userId":"other-user"}';
    tickViews(timers, 2000); await until(() => nodes["#questionnaire-factors"].textContent.includes("已完成"));
    assert.equal(requests.at(-1).searchParams.get("userId"), ids.userId);
    assert.ok(requests.at(-1).pathname.includes("survey-001"));
    assert.equal(model.json, '{"questionnaireId":"other-questionnaire","userId":"other-user"}');
    model.setMode("form", ids); await app.run("questionnaire.factor-assessment");
    assert.match(nodes["#questionnaire-status"].textContent, /答案已提交/);
    assert.equal(nodes["#questionnaire-chat"].hidden, false);
    assert.match(nodes["#questionnaire-factors"].textContent, /因素分析已完成/);
});

test("leaving questionnaire page cancels factor polling and ignores its pending response", async () => {
    const waiting = deferred(); let calls = 0;
    const {app, nodes, timers, events} = browserHarness(async (url) => {
        if (!requestTarget(url).pathname.endsWith("/factor-assessment")) return jsonResponse(questionnaireFixture({status: "submitted"}));
        return ++calls === 1 ? jsonResponse(factorFixture({status: "running", result: null}), 202) : waiting.promise;
    }, {}, "record-preview.js");
    await app.run("questionnaire.status"); await until(() => nodes["#questionnaire-factors"].textContent.includes("正在进行"));
    tickViews(timers, 2000); await until(() => calls === 2); events.pagehide();
    waiting.resolve(jsonResponse(factorFixture())); await new Promise((resolve) => setTimeout(resolve, 10));
    assert.doesNotMatch(nodes["#questionnaire-factors"].textContent, /合成报告目标/);
    assert.equal([...timers.values()].filter((timer) => timer.delay === 2000).length, 0);
});

test("actual factor shape keeps question labels, both evidence directions, target answers and unsafe URLs as text", async () => {
    const completed = factorFixture();
    completed.result.reportInterpretations = [{id: "re-summary", reportIndex: 0, pointer: "/aiSummary", kind: "ai_interpretation",
        label: "aiSummary", record: "合成报告已有 AI 总结", observedAt: "2026-01-01", provenance: "ai_interpretation", origin: "report_interpretation"}];
    const target = completed.result.targets[0], candidate = target.candidateFactors[0];
    candidate.relationType = "possible_contributor";
    candidate.after.presence = "conflicting";
    candidate.after.conditionAssessments[0].status = "conflicting";
    candidate.after.conditionAssessments[0].contradicts = [{evidenceId: "report-1", quote: "合成反对证据", kind: "report_record"}];
    target.answerEvidence = [{id: "answer-target", kind: "answer", questionName: "exercise", question: {type: "radiogroup", title: "合成目标来源问题？"},
        rawAnswer: "yes", answer: "yes", answerText: "目标来源选项中文", status: "answered", informative: true}];
    target.reportEvidence = [{id: "report-1", kind: "report_record", pointer: "reports/0/observations/0", record: {itemName: "合成指标", resultValue: "0"}}];
    completed.result.sources = [{id: "source-1", title: "合成来源", url: 'javascript:alert("<script>")', version: "source-version-1",
        passages: [{id: "passage-1", text: "合成医学来源完整原文", locator: "第十八条"}]}];
    const {app, nodes} = browserHarness(async (url) => jsonResponse(requestTarget(url).pathname.endsWith("/factor-assessment")
        ? completed : questionnaireFixture({status: "submitted"})), {}, "record-preview.js");
    await app.run("questionnaire.status"); await until(() => nodes["#questionnaire-factors"].textContent.includes("已完成"));
    const host = nodes["#questionnaire-factors"];
    for (const button of descendants(host, (node) => node.tagName === "button" && node.textContent === "展开全部")) await button.emit("click");
    for (const content of ["合成指标", "AI 风险解释", "合成候选", "可能相关因素", "合成最终因素", "合成实际问题？", "合成实际回答", "合成目标来源问题？",
        "目标来源选项中文", "回答状态：已回答", "判断：存在冲突", "合成反对证据", "合成医学来源完整原文", "第十八条", "source-version-1", "javascript:alert",
        "报告原有解读", "合成报告已有 AI 总结", "AI 解读不等于原始检测记录"]) {
        assert.ok(host.textContent.includes(content), content);
    }
    assert.equal(descendants(host, (node) => ["a", "img", "script"].includes(node.tagName)).length, 0);
    assert.doesNotMatch(host.textContent, /证据方向：支持|待确认|待审核/);
    for (const title of ["1. 报告目标与风险解读", "2. 初步候选与医学来源", "3. 最终相关因素及关系", "4. 实际问答如何支持或反对"]) {
        const summary = descendants(host, (node) => node.tagName === "summary" && node.textContent === title)[0];
        assert.equal(summary.parentElement.tagName, "details");
    }
});

test("factor query timeout aborts, releases timers and ignores a late response after a successful requery", async () => {
    const waiting = deferred(); let reads = 0, signal;
    const {app, nodes, timers} = browserHarness(async (url, options) => {
        if (!requestTarget(url).pathname.endsWith("/factor-assessment")) return jsonResponse(questionnaireFixture({status: "submitted"}));
        if (++reads === 1) { signal = options.signal; return waiting.promise; }
        return jsonResponse(factorFixture({status: "unavailable", result: null}));
    }, {}, "record-preview.js");
    await app.run("questionnaire.status"); tickViews(timers, 30000);
    assert.equal(signal.aborted, true);
    assert.match(nodes["#questionnaire-factors"].textContent, /技术失败.*QUERY_TIMEOUT/s);
    assert.equal(nodes["#questionnaire-chat"].hidden, false);
    assert.equal([...timers.values()].filter((timer) => [2000, 30000].includes(timer.delay)).length, 0);
    await control(nodes["#questionnaire-factors"], "重新查询分析").emit("click");
    waiting.resolve(jsonResponse(factorFixture())); await new Promise((resolve) => setTimeout(resolve, 10));
    assert.match(nodes["#questionnaire-factors"].textContent, /暂无可用/);
    assert.doesNotMatch(nodes["#questionnaire-factors"].textContent, /合成报告目标/);
    assert.equal([...timers.values()].filter((timer) => [2000, 30000].includes(timer.delay)).length, 0);
});

test("replayed submission does not duplicate factor polls and server retry delay is respected", async () => {
    let reads = 0;
    const {app, nodes, timers} = browserHarness(async (url) => {
        if (!requestTarget(url).pathname.endsWith("/factor-assessment")) return jsonResponse(questionnaireFixture({status: "submitted", answers: {}}));
        reads++;
        return jsonResponse(factorFixture({status: reads === 1 ? "queued" : "failed", result: null, retryAfterSeconds: 3, errorCode: reads === 1 ? undefined : "SYNTHETIC_FAILED"}));
    }, {}, "record-preview.js");
    await app.run("questionnaire.status"); await until(() => nodes["#questionnaire-factors"].textContent.includes("已排队"));
    await app.run("questionnaire.submit"); await app.run("questionnaire.status");
    assert.equal(reads, 1);
    assert.equal([...timers.values()].filter((timer) => timer.delay === 3000).length, 1);
    assert.match(nodes["#questionnaire-factors"].textContent, /每 3 秒/);
    tickViews(timers, 3000); await until(() => nodes["#questionnaire-factors"].textContent.includes("技术失败"));
    assert.equal([...timers.values()].filter((timer) => [2000, 3000, 30000].includes(timer.delay)).length, 0);
});

test("returning from Chat resumes a paused factor view without a disabled query button", async () => {
    let reads = 0;
    const {app, nodes, timers, events} = browserHarness(async (url) => {
        if (!requestTarget(url).pathname.endsWith("/factor-assessment")) return jsonResponse(questionnaireFixture({status: "submitted"}));
        return jsonResponse(++reads === 1 ? factorFixture({status: "running", result: null}) : factorFixture());
    }, {}, "record-preview.js");
    await app.run("questionnaire.status"); await until(() => nodes["#questionnaire-factors"].textContent.includes("正在进行"));
    events.pagehide();
    assert.equal(control(nodes["#questionnaire-factors"], "重新查询分析").disabled, false);
    assert.equal([...timers.values()].filter((timer) => [2000, 30000].includes(timer.delay)).length, 0);
    events.pageshow(); await until(() => nodes["#questionnaire-factors"].textContent.includes("因素分析已完成"));
    assert.equal(reads, 2);
});

function healthAnalysisFixture(schema = {title: "合成生活补充卷", description: "仅用于合成测试", pages: [{name: "daily", elements: [
    {type: "radiogroup", name: "activity", title: "每次活动多久？", choices: [{value: "short", text: "不足15分钟"}, {value: "long", text: "15分钟及以上"}]},
    {type: "checkbox", name: "drinks", title: "平时喝哪些饮品？", showNoneItem: true, choices: [{value: "water", text: "白水"}, {value: "tea", text: "茶"}]},
]}]}) {
    return {healthReview: "合成健康评价", suggestedMaterials: "合成检查建议", advancedQuestions: "合成深入问题", advancedSurveySchema: schema};
}
test("health analysis contract is independent, synchronous and uses flat multi-survey input", () => {
    const endpoint = byId("health-analysis.generate");
    assert.equal(endpoint.path, "/api/v1/health-analysis");
    assert.equal(endpoint.method, "POST");
    assert.equal(endpoint.timeoutMs, 180000);
    assert.deepEqual(endpoint.fields.map((field) => field.name), ["userId", "name", "age", "gender", "groupId", "surveyList"]);
    const model = draft(endpoint.id); model.set("groupId", "test-group"); model.set("name", "合成用户"); model.set("age", 42); model.set("gender", "男");
    assert.deepEqual(plain(model.parameters(ids)), {userId: ids.userId, name: "合成用户", age: 42, gender: "男", groupId: "test-group", surveyList: []});
    assert.equal(endpoint.stream, undefined);
});
test("health analysis page sends once and renders four distinct results without task polling", async () => {
    const calls = [];
    const {app, nodes, timers} = browserHarness(async (url, options) => {
        calls.push([requestTarget(url).pathname, JSON.parse(options.body)]);
        return jsonResponse(healthAnalysisFixture());
    }, {}, "health-analysis.js");
    app.setParams("health-analysis.generate", {name: "合成用户", age: 42, gender: "男", groupId: "test-group"});
    await app.run("health-analysis.generate");
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], "/api/v1/health-analysis");
    assert.deepEqual(calls[0][1], {userId: ids.userId, name: "合成用户", age: 42, gender: "男", groupId: "test-group", surveyList: []});
    for (const text of ["合成健康评价", "合成检查建议", "合成深入问题", "合成生活补充卷", "每次活动多久？"]) assert.match(nodes["#analysis-preview"].textContent, new RegExp(text.replace("？", ".")));
    assert.equal(nodes["#analysis-state"].textContent, "已完成");
    assert.equal([...timers.values()].filter((timer) => [2000, 30000, 180000].includes(timer.delay)).length, 0);
});
test("health analysis preview answers stay local until explicitly appended to the next request", async () => {
    const {app, nodes} = browserHarness(async () => jsonResponse(healthAnalysisFixture()), {}, "health-analysis.js");
    app.setParams("health-analysis.generate", {name: "合成用户", age: 42, gender: "男", groupId: "test-group"});
    await app.run("health-analysis.generate");
    const inputs = descendants(nodes["#analysis-preview"], (node) => node.tagName === "input");
    inputs[0].checked = true; await inputs[0].emit("change");
    inputs[2].checked = true; await inputs[2].emit("change");
    inputs.at(-1).checked = true; await inputs.at(-1).emit("change");
    assert.equal(inputs[2].checked, false);
    assert.deepEqual(plain(app.draft("health-analysis.generate").parameters(ids).surveyList), []);
    await control(nodes["#analysis-preview"], "追加到历史问卷").emit("click");
    const list = plain(app.draft("health-analysis.generate").parameters(ids).surveyList);
    assert.equal(list.length, 1);
    assert.deepEqual(list[0].answer, {activity: "short", drinks: ["none"]});
    assert.equal(list[0].schema.title, "合成生活补充卷");
});
test("health analysis keeps empty review with a base survey distinct from exhausted null survey", async () => {
    let exhausted = false;
    const {app, nodes} = browserHarness(async () => jsonResponse({...healthAnalysisFixture(exhausted ? null : undefined), healthReview: "", suggestedMaterials: "", advancedQuestions: ""}), {}, "health-analysis.js");
    app.setParams("health-analysis.generate", {name: "合成用户", age: 42, gender: "男", groupId: "test-group"});
    await app.run("health-analysis.generate");
    assert.match(nodes["#analysis-preview"].textContent, /合成生活补充卷/);
    assert.doesNotMatch(nodes["#analysis-preview"].textContent, /没有可发布的新题/);
    exhausted = true; await app.run("health-analysis.generate");
    assert.match(nodes["#analysis-preview"].textContent, /没有可发布的新题/);
    assert.equal(descendants(nodes["#analysis-preview"], (node) => node.tagName === "input").length, 0);
});
test("health analysis request waiting and timeout cancel the connection and clear the timer", async () => {
    let signal;
    const {app, nodes, timers} = browserHarness(async (url, options) => {
        signal = options.signal;
        return new Promise((resolve, reject) => signal.addEventListener("abort", () => reject(new Error("cancelled")), {once: true}));
    }, {}, "health-analysis.js");
    app.setParams("health-analysis.generate", {name: "合成用户", age: 42, gender: "男", groupId: "test-group"});
    const pending = app.run("health-analysis.generate");
    assert.equal(nodes["#analysis-state"].textContent, "正在等待完整结果…");
    tickViews(timers, 180000); await pending;
    assert.equal(signal.aborted, true);
    assert.match(nodes["#analysis-preview"].textContent, /等待完整响应超时/);
    assert.equal(nodes["#analysis-state"].textContent, "请求失败");
    assert.equal([...timers.values()].filter((timer) => timer.delay === 180000).length, 0);
});
test("health analysis synthetic presets only populate drafts and never send automatically", async () => {
    let calls = 0;
    const {app, nodes} = browserHarness(async () => { calls++; return jsonResponse(healthAnalysisFixture()); }, {}, "health-analysis.js");
    const buttons = nodes["#analysis-examples"].children;
    await buttons[1].emit("click");
    let payload = plain(app.draft("health-analysis.generate").parameters(app.context.get()));
    assert.equal(payload.userId, "test-demo-empty");
    assert.equal(payload.surveyList.length, 1);
    assert.deepEqual(payload.surveyList[0].answer, {});
    await buttons[2].emit("click");
    payload = plain(app.draft("health-analysis.generate").parameters(app.context.get()));
    assert.deepEqual(payload.surveyList[0].answer, {activity_days: "days_1_2"});
    assert.equal(calls, 0);
});
