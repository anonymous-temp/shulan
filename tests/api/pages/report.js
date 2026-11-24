(() => {
    const list = document.querySelector("#report-list");
    const next = document.querySelector("#next-reports");
    const progress = document.querySelector("#report-progress");
    let cursor = null;
    ApiLab.initialize({group: "report",
        onReset() { list.replaceChildren(); progress.replaceChildren(); cursor = null; next.disabled = true; },
        onResponse(payload, {id}) {
            const data = payload.data || {};
            if (id === "report.submit" && data.finalReportId) {
                ApiLab.setParams("report.get", {finalReportId: data.finalReportId}, {onlyDefault: true});
                list.replaceChildren(ApiLab.element("p", "notice", `已受理：${data.finalReportId}。可切换到详情接口查询生成结果。`));
                progress.replaceChildren();
            }
            if (id === "report.list") {
                cursor = data.nextCursor; next.disabled = !cursor;
                list.replaceChildren();
                for (const batch of data.items || []) {
                    const button = ApiLab.element("button", "data-row selectable-row");
                    button.append(ApiLab.element("strong", "", batch.finalReportId));
                    for (const line of ApiLabCore.reportProgress(batch)) button.append(ApiLab.element("p", "hint", line));
                    button.append(ApiLab.element("pre", "result-json", ApiLab.format(batch)));
                    button.addEventListener("click", () => {
                        ApiLab.draft("report.get").mode = "form";
                        ApiLab.setParams("report.get", {finalReportId: batch.finalReportId}, {touch: false}); ApiLab.select("report.get");
                    });
                    list.append(button);
                }
                if (!data.items?.length) list.append(ApiLab.element("p", "empty", "暂无符合条件的报告。"));
            }
            if (id === "report.get") {
                progress.replaceChildren(...ApiLabCore.reportProgress(data).map((line) => ApiLab.element("p", "notice", line)));
            }
        },
    });
    next.addEventListener("click", () => {
        ApiLab.draft("report.list").mode = "form";
        ApiLab.setParams("report.list", {cursor}, {touch: false}); ApiLab.select("report.list");
    });

    const callbackUrl = document.querySelector("#mock-callback-url");
    const copyCallback = document.querySelector("#copy-callback-url");
    const callbackStatus = document.querySelector("#callback-url-status");
    if (location.protocol === "http:" || location.protocol === "https:") {
        callbackUrl.value = `${location.origin}/callback`;
        copyCallback.addEventListener("click", async () => {
            if (await ApiLabJson.copyText(document, callbackUrl.value)) {
                callbackStatus.textContent = "回调地址已复制。";
                return;
            }
            callbackUrl.focus();
            callbackUrl.select();
            callbackStatus.textContent = "回调地址已选中，请按 Ctrl+C 复制。";
        });
    } else {
        copyCallback.disabled = true;
        callbackStatus.textContent = "请先启动页面服务，再通过 HTTP 地址打开本页以使用模拟回调。";
    }
})();
