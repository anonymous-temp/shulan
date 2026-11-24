/* Dependency-free JSON inspector. Values are always rendered as text, never HTML. */
const ApiLabJson = (() => {
    const views = new WeakMap();
    const format = (value) => JSON.stringify(value, null, 4);

    // navigator.clipboard only exists in secure contexts (https or localhost);
    // the page service is routinely opened over plain LAN HTTP, where copying
    // must fall back to a synchronous execCommand("copy") inside the click gesture.
    async function copyText(doc, text) {
        try {
            const clipboard = doc.defaultView?.navigator?.clipboard;
            if (clipboard?.writeText) {
                await clipboard.writeText(text);
                return true;
            }
        } catch { /* Unfocused document or denied permission: try the legacy path. */ }
        try {
            if (!doc.body || !doc.execCommand) return false;
            const holder = doc.createElement("textarea");
            holder.value = text;
            holder.setAttribute("readonly", "");
            holder.style.position = "fixed";
            holder.style.top = "-1000px";
            doc.body.append(holder);
            holder.select();
            holder.setSelectionRange?.(0, text.length);
            const copied = doc.execCommand("copy");
            holder.remove();
            return copied;
        } catch { return false; }
    }

    function render(target, value, {label = "JSON 数据", stateKey = "result"} = {}) {
        const doc = target.ownerDocument;
        const previous = views.get(target);
        const expanded = previous?.stateKey === stateKey
            ? new Map(previous.branches.map((branch) => [branch.path, branch.node.open])) : new Map();
        const branches = [];
        const make = (tag, className, text) => {
            const node = doc.createElement(tag);
            if (className) node.className = className;
            if (text !== undefined) node.textContent = text;
            return node;
        };
        const token = (type, text) => make("span", `json-${type}`, text);
        const line = (tag, depth) => {
            const node = make(tag, "json-line");
            node.style.setProperty("--json-depth", depth);
            node.append(doc.createTextNode("    ".repeat(depth)));
            return node;
        };
        function item(data, key, depth, path, comma) {
            const array = Array.isArray(data);
            const object = data !== null && typeof data === "object";
            const entries = object ? Object.entries(data) : [];
            const opening = array ? "[" : "{";
            const closing = array ? "]" : "}";
            const content = line(entries.length ? "summary" : "div", depth);
            if (key !== undefined) content.append(token("key", JSON.stringify(key)), token("punctuation", ": "));
            if (!entries.length) {
                const type = object ? "punctuation" : data === null ? "null" : typeof data;
                content.append(token(type, object ? opening + closing : JSON.stringify(data)), token("punctuation", comma + "\n"));
                return content;
            }
            const node = make("details", "json-node");
            const children = make("div", "json-children");
            const end = line("div", depth);
            const count = `${entries.length} ${array ? "项" : "个字段"}`;
            content.setAttribute("aria-label", `${key ?? (depth ? "数组元素" : label)} · ${count}`);
            content.append(token("punctuation", opening),
                make("span", "json-collapsed", ` … ${count} ${closing}${comma}`), doc.createTextNode("\n"));
            end.append(token("punctuation", closing + comma + "\n"));
            node.append(content, children, end);
            let filled = false;
            function fill() {
                if (filled) return;
                filled = true;
                entries.forEach(([name, child], index) => children.append(item(
                    child, array ? undefined : name, depth + 1,
                    JSON.stringify([...JSON.parse(path), name]), index < entries.length - 1 ? "," : ""
                )));
            }
            branches.push({node, path, fill});
            node.open = expanded.get(path) ?? depth < 2;
            node.addEventListener("toggle", () => { if (node.open) fill(); });
            if (node.open) fill();
            return node;
        }
        const viewer = make("div", "json-viewer");
        const toolbar = make("div", "json-toolbar");
        const actions = make("div", "actions json-actions");
        const feedback = make("span", "json-feedback");
        feedback.setAttribute("role", "status");
        function button(text, action) {
            const control = make("button", "button ghost compact", text);
            control.type = "button";
            control.addEventListener("click", action);
            actions.append(control);
            return control;
        }
        button("展开全部", () => {
            // fill() appends newly discovered branches to this same list.
            for (let index = 0; index < branches.length; index++) {
                branches[index].fill(); branches[index].node.open = true;
            }
        });
        button("折叠全部", () => { branches.forEach((branch) => { branch.node.open = false; }); });
        button("复制 JSON", async () => {
            feedback.textContent = await copyText(doc, format(value))
                ? "已复制完整 JSON" : "复制失败，请展开后手动选择文本";
        });
        toolbar.append(make("span", "json-caption", "JSON · 4 空格缩进"), actions, feedback);
        const tree = make("div", "json-tree");
        tree.setAttribute("role", "region");
        tree.setAttribute("aria-label", label);
        tree.tabIndex = 0;
        tree.append(item(value, undefined, 0, "[]", ""));
        viewer.append(toolbar, tree);
        target.replaceChildren(viewer);
        views.set(target, {stateKey, branches});
    }
    return {render, format, copyText};
})();
if (typeof module !== "undefined" && module.exports) module.exports = ApiLabJson;
