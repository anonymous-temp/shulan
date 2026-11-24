/* A synchronous client: generated answers remain local until the next explicit request. */
(() => {
    "use strict";
    const endpoint = "health-analysis.generate";
    const host = document.querySelector("#analysis-preview");
    const state = document.querySelector("#analysis-state");
    const el = ApiLab.element;
    let currentSchema = null;
    let currentAnswers = {};
    const clone = (value) => JSON.parse(JSON.stringify(value));

    function block(title, value, empty) {
        const section = el("section", "analysis-block");
        section.append(el("h3", "", title), el("p", value ? "analysis-prose" : "hint", value || empty));
        return section;
    }
    function survey(schema) {
        const section = el("section", "analysis-block analysis-survey");
        section.append(el("h3", "", schema.title || "健康情况补充问卷"));
        if (schema.description) section.append(el("p", "analysis-prose", schema.description));
        section.append(el("p", "hint", "可在此检查选项和填写效果。全部题目允许留空；答案只留在当前页面。追加到历史后，下次点击发送才会参与新的分析。"));
        let questionNumber = 0;
        for (const [pageIndex, page] of (schema.pages || []).entries()) {
            for (const [questionIndex, question] of (page.elements || []).entries()) {
                const group = el("fieldset", "analysis-question");
                questionNumber += 1;
                group.append(el("legend", "", `${questionNumber}. ${question.title ?? ""}`));
                if (question.description) group.append(el("p", "hint", question.description));
                const inputs = [];
                const choices = [...(question.choices || [])];
                if (question.type === "checkbox" && question.showNoneItem) choices.push({value: "none", text: question.noneText || "以上均无"});
                if (!["radiogroup", "checkbox"].includes(question.type)) {
                    group.append(el("p", "error-text", "响应中存在不受支持的新题型，请核对原始 JSON。"));
                } else {
                    const options = el("div", "analysis-options");
                    for (const [choiceIndex, choice] of choices.entries()) {
                        const label = el("label", "analysis-option");
                        const input = el("input");
                        input.type = question.type === "radiogroup" ? "radio" : "checkbox";
                        input.name = `analysis-${pageIndex}-${questionIndex}`;
                        input.id = `${input.name}-${choiceIndex}`;
                        input.value = choice.value;
                        inputs.push(input);
                        input.addEventListener("change", () => {
                            if (question.type === "radiogroup") currentAnswers[question.name] = input.value;
                            else {
                                if (input.checked) for (const other of inputs) {
                                    if ((input.value === "none" && input !== other) || (input.value !== "none" && other.value === "none")) other.checked = false;
                                }
                                const values = inputs.filter((item) => item.checked).map((item) => item.value);
                                if (values.length) currentAnswers[question.name] = values;
                                else delete currentAnswers[question.name];
                            }
                        });
                        label.append(input, el("span", "", choice.text ?? choice.value));
                        options.append(label);
                    }
                    group.append(options);
                }
                section.append(group);
            }
        }
        const feedback = el("p", "hint analysis-feedback");
        feedback.setAttribute("role", "status");
        const actions = el("div", "actions");
        const append = el("button", "button secondary", "追加到历史问卷");
        append.type = "button"; append.id = "analysis-append-history";
        append.addEventListener("click", () => {
            try {
                const model = ApiLab.draft(endpoint);
                const payload = model.parameters(ApiLab.context.get(), false);
                if (!Array.isArray(payload.surveyList)) throw new Error("请先将 surveyList 设置为数组。");
                const item = {schema: clone(currentSchema), answer: clone(currentAnswers)};
                const surveys = [...payload.surveyList, item];
                if (model.mode === "json") model.json = ApiLab.format({...payload, surveyList: surveys});
                ApiLab.setParams(endpoint, {surveyList: surveys});
                append.disabled = true;
                feedback.textContent = `已追加为第 ${surveys.length} 份历史问卷。可继续编辑答案后再显式发送新请求。`;
            } catch (error) { feedback.textContent = error.message; }
        });
        const copy = el("button", "button ghost", "复制问卷和本地答案");
        copy.type = "button"; copy.id = "analysis-copy-survey";
        copy.addEventListener("click", async () => {
            const content = ApiLab.format({schema: currentSchema, answer: currentAnswers});
            feedback.textContent = await ApiLabJson.copyText(document, content) ? "已复制问卷和本地答案。" : "复制失败，请从原始 JSON 手动复制问卷。";
        });
        const clear = el("button", "button ghost", "清空本地答案");
        clear.type = "button";
        clear.addEventListener("click", () => {
            currentAnswers = {};
            for (const input of section.querySelectorAll("input")) input.checked = false;
            feedback.textContent = "已清空本地答案。";
        });
        actions.append(append, copy, clear); section.append(actions, feedback);
        return section;
    }
    function render({payload, error}) {
        currentSchema = null; currentAnswers = {};
        host.setAttribute("aria-busy", "false");
        if (error) {
            state.textContent = "请求失败";
            host.replaceChildren(el("p", "error-text", `${error.status ? `HTTP ${error.status} · ` : ""}${error.message}`));
            return;
        }
        const data = payload?.data;
        if (!data || typeof data !== "object") {
            state.textContent = "响应格式待核对";
            host.replaceChildren(el("p", "error-text", "响应中没有健康分析 data，请检查原始 JSON。"));
            return;
        }
        state.textContent = "已完成";
        host.replaceChildren(
            block("综合健康评价", data.healthReview, "本次没有可生成个人健康评价的内容。"),
            block("建议补充的检查项目", data.suggestedMaterials, "本次没有新增检查建议。"),
            block("健管师深入问题", data.advancedQuestions, "本次没有新增深入沟通问题。"),
        );
        if (data.advancedSurveySchema === null) {
            host.append(block("补充问卷", "", "当前政策支持范围内没有可发布的新题。"));
        } else if (data.advancedSurveySchema && Array.isArray(data.advancedSurveySchema.pages)) {
            currentSchema = clone(data.advancedSurveySchema);
            host.append(survey(currentSchema));
        } else host.append(block("补充问卷", "", "问卷结构待核对，请检查原始 JSON。"));
    }
    function example(kind) {
        const personal = kind === "report" || kind === "chat";
        const userId = `test-demo-${personal ? kind : "empty"}`;
        ApiLab.context.update({userId});
        const surveyList = [];
        if (kind === "history") surveyList.push({schema: {title: "合成已发问卷", pages: [{name: "daily", elements: [{
            type: "checkbox", name: "old_diet", title: "您平时的正餐通常吃到哪些食物？可多选。", choices: [{value: "vegetable", text: "蔬菜"}],
        }]}]}, answer: {}});
        if (kind === "answer") surveyList.push({schema: {title: "合成活动问卷", pages: [{name: "activity", elements: [{
            type: "radiogroup", name: "activity_days", title: "您一周有几天进行锻炼？", choices: [{value: "days_1_2", text: "1～2天"}, {value: "unknown", text: "不清楚"}],
        }]}]}, answer: {activity_days: "days_1_2"}});
        const model = ApiLab.draft(endpoint);
        model.setMode("form", ApiLab.context.get());
        ApiLab.setParams(endpoint, {name: "合成用户", age: 42, gender: "男", groupId: "test-demo-group", surveyList});
        state.textContent = "示例已载入，尚未发送";
    }
    ApiLab.initialize({
        group: "health-analysis",
        onStart() {
            state.textContent = "正在等待完整结果…";
            host.setAttribute("aria-busy", "true");
            host.replaceChildren(el("p", "hint", "请求正在执行。页面等待一次完整响应；超时会取消连接，请勿连续重复发送。"));
        },
        onResult: render,
        onReset() {
            currentSchema = null; currentAnswers = {};
            state.textContent = "尚未生成";
            host.setAttribute("aria-busy", "false");
            host.replaceChildren(el("p", "hint", "公共设置已改变，请重新发送请求。"));
        },
    });
    for (const button of document.querySelectorAll("#analysis-examples button")) button.addEventListener("click", () => example(button.dataset.scenario));
})();
