/* Convenience attachment controls share the create request's canonical draft. */
const ApiLabFiles = (() => {
    function mount({endpoint = "record.create", prefix = "record-file", action = "创建建档"} = {}) {
        const $ = (id) => document.querySelector(`#${prefix}-${id}`);
        const source = $("source"), value = $("value"), type = $("type"), add = $("add"), list = $("list");
        const model = () => ApiLab.draft(endpoint);
        const limit = model().endpoint.fields.find((field) => field.name === "fileList").maxItems;
        let editingIndex = null, writing = false, lastSignature;

        function clearInput() {
            editingIndex = null;
            value.value = ""; type.value = "";
        }
        function writeFiles(files) {
            writing = true;
            try { ApiLab.setParams(endpoint, {fileList: files.length ? files : undefined}); }
            finally { writing = false; }
        }

        function readFiles() {
            let files;
            if (model().mode === "json") files = model().parameters(ApiLab.context.get(), false).fileList;
            else {
                const raw = model().values.fileList.trim();
                if (!raw) return [];
                try { files = JSON.parse(raw); }
                catch { throw new Error("fileList JSON 尚未完整，请先在接口参数区完成编辑。"); }
            }
            if (files === undefined) return [];
            if (!Array.isArray(files)) throw new Error("fileList 当前不是数组，原值保持不变；请在接口参数区编辑。");
            return files;
        }
        function feedback(message, error = false) {
            $("message").textContent = message;
            $("message").className = error ? "hint error-text" : "hint";
        }
        function sync(force = false) {
            const signature = JSON.stringify([model().mode, model().values.fileList, model().mode === "json" ? model().json : null]);
            // Bulk edits, JSON mode and identity changes invalidate the input's array index.
            if (force || (!writing && lastSignature !== undefined && signature !== lastSignature)) {
                clearInput(); source.value = "url";
            }
            lastSignature = signature;
            feedback("");
            const jsonMode = model().mode === "json";
            const reference = source.value === "documentId";
            $("value-label").textContent = reference ? "已有 documentId" : "文件 URL";
            value.placeholder = reference ? "document_…" : "https://example.com/report.pdf";
            $("type-field").hidden = reference;
            $("hint").textContent = reference
                ? "引用体检解读或建档响应中的 documentId，必须属于当前用户的本人主体；不携带 fileType。"
                : "使用 HTTP(S) 直接下载地址，单份不超过 50 MiB；URL 无法识别格式时，请选择 PDF 或图片。";
            $("mode").textContent = jsonMode
                ? `${action}当前使用完整 JSON：下方展示草稿中的附件。请在接口参数区编辑 fileList，或切回参数表单后添加附件。`
                : `输入即同步到右侧 JSON，无需点击添加即可${action}；“添加下一份”用于继续填写其他文件。清空或移除最后一份后不发送 fileList。`;
            let files = [], problem = "";
            try { files = readFiles(); } catch (error) { problem = error.message; }
            const locked = jsonMode || !!problem;
            source.disabled = value.disabled = type.disabled = locked || (files.length >= limit && editingIndex === null);
            add.disabled = locked || files.length >= limit;
            $("count").textContent = problem ? "请检查 fileList" : `${files.length} / ${limit} 份`;
            if (problem) feedback(problem, true);
            else if (!jsonMode && editingIndex !== null) {
                const warning = inputWarning(files);
                feedback(warning || `正在编辑附件 ${editingIndex + 1}，输入已同步到请求 JSON。`, !!warning);
            }
            list.replaceChildren();
            if (!files.length) {
                list.append(ApiLab.element("li", "hint", problem ? "附件原值保留在接口参数中。" : "尚未添加附件。"));
                return;
            }
            files.forEach((file, index) => {
                const row = ApiLab.element("li", "record-file-row");
                const info = ApiLab.element("div", "record-file-info");
                const title = file?.documentId !== undefined ? "已有文档" : file?.fileType === "pdf" ? "PDF"
                    : file?.fileType === "image" ? "图片" : "文件";
                const location = file?.documentId ?? file?.url ?? file;
                info.append(ApiLab.element("strong", "", `附件 ${index + 1} · ${title}`),
                    ApiLab.element("code", "", typeof location === "string" ? location : ApiLab.format(location)));
                const remove = ApiLab.element("button", "button ghost", "移除");
                remove.type = "button"; remove.disabled = locked;
                remove.setAttribute("aria-label", `移除附件 ${index + 1}`);
                remove.addEventListener("click", () => {
                    if (model().mode !== "form") return;
                    try {
                        const next = readFiles().filter((_, position) => position !== index);
                        if (editingIndex === index) clearInput();
                        else if (editingIndex !== null && editingIndex > index) editingIndex--;
                        writeFiles(next);
                    } catch (error) { feedback(error.message, true); }
                });
                const edit = ApiLab.element("button", "button ghost", "编辑");
                edit.type = "button";
                edit.disabled = locked || !(typeof file?.url === "string" || typeof file?.documentId === "string");
                edit.setAttribute("aria-label", `编辑附件 ${index + 1}`);
                edit.addEventListener("click", () => {
                    if (edit.disabled || model().mode !== "form") return;
                    editingIndex = index;
                    source.value = typeof file.documentId === "string" ? "documentId" : "url";
                    value.value = file.documentId ?? file.url;
                    type.value = file.fileType || "";
                    sync(); value.focus();
                });
                row.append(info, edit, remove); list.append(row);
            });
        }
        function inputWarning(files) {
            const text = value.value.trim();
            if (!text || source.value === "documentId") return "";
            let url;
            try { url = new URL(text); } catch { return "文件 URL 尚不完整；当前输入已保留在 JSON 中，请填写 HTTP(S) 下载地址。"; }
            if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.hash) {
                return "请使用不含凭据或片段的 HTTP(S) 下载地址；当前输入已保留在 JSON 中。";
            }
            if (files.some((file, index) => index !== editingIndex && file?.url === text)) {
                return "该文件 URL 已在附件列表中；当前输入已保留在 JSON 中，可修改或移除。";
            }
            return "";
        }
        function updateInput() {
            if (model().mode !== "form") return;
            try {
                const files = readFiles();
                const text = value.value.trim();
                if (!text) {
                    if (editingIndex !== null) {
                        files.splice(editingIndex, 1); editingIndex = null;
                        writeFiles(files);
                    }
                    return;
                }
                if (editingIndex === null) {
                    if (files.length >= limit) throw new Error(`本次最多添加 ${limit} 份附件。`);
                    editingIndex = files.length;
                }
                const file = {...files[editingIndex]};
                delete file.url; delete file.documentId; delete file.fileType;
                if (source.value === "documentId") file.documentId = text;
                else { file.url = text; if (type.value) file.fileType = type.value; }
                files[editingIndex] = file;
                writeFiles(files);
            } catch (error) { feedback(error.message, true); }
        }
        value.addEventListener("input", updateInput);
        type.addEventListener("change", updateInput);
        add.addEventListener("click", () => {
            if (model().mode !== "form" || add.disabled) return;
            clearInput(); sync(); value.focus();
        });
        source.addEventListener("change", () => {
            if (model().mode !== "form") return;
            value.value = ""; type.value = "";
            updateInput(); sync();
        });
        $("edit").addEventListener("click", () => {
            ApiLab.select(endpoint);
            const input = document.querySelector(model().mode === "json" ? "#api-json" : "#param-fileList");
            input?.scrollIntoView({block: "nearest"}); input?.focus();
        });
        ApiLab.subscribeDrafts(sync);
    }
    return {mount};
})();
if (document.querySelector("#record-file-source")) ApiLabFiles.mount();
