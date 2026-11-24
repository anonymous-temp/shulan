/* Offline browser benchmark; all API responses come from this local fixture.
 * Requires Playwright and Chrome (or set BROWSER_CHANNEL=msedge).
 * node tests/unit/api/stream_browser_benchmark.cjs [--baseline=HEAD] [--events=10000] [--output=/tmp/shulan-stream-current]
 * Baseline reads committed page assets with git show; it never changes the checkout.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const {execFileSync} = require("node:child_process");
const root = path.resolve(__dirname, "../../..");
const pages = path.join(root, "tests/api/pages");
const option = (name) => process.argv.find((arg) => arg.startsWith(`--${name}=`))?.split("=").slice(1).join("=");
const baseline = option("baseline");
const eventCount = Number(option("events") || 10000);
assert.ok(Number.isInteger(eventCount) && eventCount > 0);
const timeoutMs = Number(option("timeout") || 120000);
function externalArtifactPath(value) {
    let candidate = path.resolve(root, value);
    const tail = [];
    let links = 0;
    for (;;) {
        try {
            const stat = fs.lstatSync(candidate);
            if (stat.isSymbolicLink()) {
                if (++links > 40) throw new Error("artifact output has a symlink loop");
                candidate = path.resolve(path.dirname(candidate), fs.readlinkSync(candidate));
                continue;
            }
            candidate = path.resolve(fs.realpathSync(candidate), ...tail);
            break;
        } catch (error) {
            if (error.code !== "ENOENT") throw error;
            const parent = path.dirname(candidate);
            if (parent === candidate) throw error;
            tail.unshift(path.basename(candidate));
            candidate = parent;
        }
    }
    const relative = path.relative(fs.realpathSync(root), candidate);
    if (!relative || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(".." + path.sep))) {
        throw new Error("artifact output must be outside the project directory");
    }
    return candidate;
}
const output = option("output") && externalArtifactPath(option("output"));
const {chromium} = require("playwright");
const fragment = "健康建议与推理片段。".repeat(2);
const question = {status: "question", questionId: "q-bench", question: "下一题", questionType: "text", round: 1, phase: "intake"};
let producerEndedAt;
const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://localhost");
    if (url.pathname === "/proxy") {
        const target = new URL(url.searchParams.get("target"));
        // No forwarding: even the configured backend is a fixture-only hostname.
        if (!target.pathname.endsWith("/stream")) {
            res.writeHead(200, {"Content-Type": "application/json"});
            res.end(JSON.stringify({data: question})); return;
        }
        const record = target.pathname.includes("/qa/");
        const events = Array.from({length: eventCount}, (_, index) => ({data: {
            type: record || index % 2 === 0 ? "reasoning" : "content", content: fragment, index,
        }}));
        if (record) events.push({data: {type: "question", ...question}});
        events.push({data: {type: "finish"}});
        res.writeHead(200, {"Content-Type": "text/event-stream", "Cache-Control": "no-store"});
        res.end(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""));
        producerEndedAt = Date.now(); return;
    }
    const relative = decodeURIComponent(url.pathname).replace(/^\/+/, "");
    const file = path.resolve(pages, relative);
    if (!file.startsWith(pages + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
        res.writeHead(404); res.end(); return;
    }
    const type = file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "text/html";
    res.writeHead(200, {"Content-Type": `${type}; charset=utf-8`, "Cache-Control": "no-store"});
    res.end(baseline ? execFileSync("git", ["show", `${baseline}:tests/api/pages/${relative}`], {cwd: root}) : fs.readFileSync(file));
});

(async () => {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const browser = await chromium.launch({headless: true, channel: process.env.BROWSER_CHANNEL || "chrome"});
    const results = [];
    try {
        if (output) fs.mkdirSync(output, {recursive: true});
        for (const group of ["chat", "record"]) {
            const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
            const errors = [];
            page.on("pageerror", (error) => errors.push(error.message));
            await page.addInitScript(() => localStorage.setItem("shulan-api-context-v1", JSON.stringify({backend: "http://fixture.invalid", userId: "benchmark", sessionId: "benchmark"})));
            await page.goto(`http://127.0.0.1:${server.address().port}/${group}.html`);
            let metrics;
            let timedOut = false;
            const watchdog = setTimeout(() => { timedOut = true; page.close().catch(() => {}); }, timeoutMs);
            try {
                metrics = await page.evaluate(async ({group, eventCount, fragment}) => {
                    if (group === "record") await ApiLab.run("record.create");
                    const id = group === "record" ? "record.answer.stream" : "chat.send.stream";
                    ApiLab.setParams(id, group === "record" ? {other: "回答"} : {message: "压力测试"});
                    ApiLab.select(id);
                    const stats = {firstEvent: null, readEnded: null, longTasks: [], timerGaps: []};
                    const started = performance.now();
                    const observer = new PerformanceObserver((list) => stats.longTasks.push(...list.getEntries().map((entry) => entry.duration)));
                    observer.observe({type: "longtask"});
                    let lastTick = started;
                    const timer = setInterval(() => { const now = performance.now(); stats.timerGaps.push(now - lastTick); lastTick = now; }, 20);
                    const originalFetch = window.fetch;
                    const originalParse = JSON.parse;
                    JSON.parse = function(...args) {
                        const value = originalParse.apply(this, args);
                        if (value?.data?.type && stats.firstEvent === null) stats.firstEvent = performance.now() - started;
                        return value;
                    };
                    window.fetch = async (...args) => {
                        const response = await originalFetch(...args);
                        const getReader = response.body.getReader.bind(response.body);
                        response.body.getReader = (...options) => {
                            const reader = getReader(...options);
                            const read = reader.read.bind(reader);
                            reader.read = async () => {
                                const result = await read();
                                if (result.done) stats.readEnded = performance.now() - started;
                                return result;
                            };
                            return reader;
                        };
                        return response;
                    };
                    const result = await ApiLab.run(id);
                    stats.domEnded = performance.now() - started;
                    stats.domEndedAt = Date.now();
                    JSON.parse = originalParse; window.fetch = originalFetch;
                    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                    stats.paintOpportunity = performance.now() - started;
                    await new Promise((resolve) => setTimeout(resolve, 50));
                    clearInterval(timer); observer.disconnect();
                    const expected = fragment.repeat(group === "record" ? eventCount : Math.floor(eventCount / 2));
                    const text = group === "record" ? document.querySelector("#question-area details .record-text")?.textContent
                        : document.querySelector("#conversation .assistant .message-content")?.textContent;
                    if (text !== expected) throw new Error(`${group}: incomplete final text`);
                    return {group, eventCount: result?.eventCount, firstEventMs: stats.firstEvent, readEndedMs: stats.readEnded,
                        finalDomMs: stats.domEnded, readToDomMs: stats.domEnded - stats.readEnded,
                        nextPaintOpportunityMs: stats.paintOpportunity, domEndedAt: stats.domEndedAt,
                        longTaskCount: stats.longTasks.length, longestTaskMs: Math.max(0, ...stats.longTasks),
                        largestTimerGapMs: Math.max(0, ...stats.timerGaps), visibleLogCharacters: document.querySelector("#raw-log").textContent.length};
                }, {group, eventCount, fragment});
            } catch (error) {
                if (!timedOut || !baseline) throw error;
                const timeout = {group, status: "timeout", eventCount, timeoutMs};
                results.push(timeout); console.log(JSON.stringify(timeout));
                continue;
            } finally { clearTimeout(watchdog); }
            metrics.mockProducerToDomMs = metrics.domEndedAt - producerEndedAt;
            delete metrics.domEndedAt;
            assert.deepEqual(errors, []);
            assert.equal(metrics.eventCount, eventCount + (group === "record" ? 2 : 1));
            if (!baseline) {
                assert.ok(metrics.visibleLogCharacters <= 256 * 1024);
                assert.ok(metrics.readToDomMs <= 100, `final DOM lag: ${metrics.readToDomMs}ms`);
                const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", {name: "导出完整记录"}).click()]);
                const archive = fs.readFileSync(await download.path(), "utf8").trim().split("\n").map(JSON.parse);
                assert.equal(archive.filter((entry) => entry.eventSequence !== null).length, metrics.eventCount);
            }
            if (output) await page.screenshot({path: path.join(output, `${group}.png`), fullPage: true});
            results.push(metrics);
            console.log(JSON.stringify(metrics));
            await page.close();
        }
        if (output) fs.writeFileSync(path.join(output, "metrics.json"), JSON.stringify({baseline: baseline || null, browser: browser.version(), results}, null, 2));
    } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
