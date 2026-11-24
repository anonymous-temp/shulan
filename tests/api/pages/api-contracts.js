/* Public inputs from web/routes.py, web/handlers and modules/report/schemas.py.
 * serverDefault documents omission semantics; preset is only a UI convenience.
 * Keep this inventory in step with the public Handler contract, not internal services.
 */
const ApiLabContracts = (() => {
    const descriptions = {
        userId: "用户标识",
        sessionId: "会话标识",
        subjectId: "健康主体标识",
        requestId: "本次操作的幂等标识",
        name: "姓名",
        age: "年龄（周岁）",
        gender: "性别",
        sex: "性别",
        groupId: "群组标识",
        surveyList: "历史问卷及作答",
        fileId: "用于解读的报告文件链接",
        documentId: "已保存的文档标识",
        reportId: "体检报告标识",
        basicInfo: "基础健康资料",
        fileList: "用于本次请求的报告附件",
        hospitalData: "院内健康资料",
        weRunData: "微信运动数据",
        doctorProductPackages: "医生产品或服务配置",
        smartDeviceData: "智能设备记录",
        questionId: "当前建档问题标识",
        answer: "题目答案",
        other: "补充文字或自由提问",
        thinking: "是否开启模型思考",
        reasoningEffort: "模型思考强度",
        round: "建档轮次",
        questionnaireId: "整卷问卷标识",
        answers: "整卷问卷的答案映射",
        count: "返回推荐问题的数量",
        eventId: "医生消息的幂等标识",
        content: "医生消息正文",
        payload: "卡片业务数据",
        displayType: "消息展示形式",
        customType: "业务卡片类型",
        title: "卡片标题",
        message: "用户发送的消息",
        finalReportId: "最终报告批次标识",
        reportType: "批次中的报告类型",
        documentObservationView: "画像中的原报告资料展示范围",
        includeEvidence: "是否请求循证检索与解释",
        factStatuses: "筛选事实列表的治理状态",
        indicatorScope: "筛选趋势区的指标范围",
        indicatorLimit: "每个指标最多返回的趋势点数",
        includeArchived: "是否列出已归档主体",
        view: "原报告观察项的展示范围",
        cursor: "上一页返回的翻页游标",
        limit: "本次返回的最大条数",
        kind: "主体类别",
        expectedSubjectVersion: "预期的当前主体版本",
        displayName: "主体的展示名称",
        relationship: "主体与本人的关系",
        categories: "筛选事实类别",
        statuses: "筛选事实状态",
        factTypes: "筛选事实类型编码",
        effectiveFrom: "有效时间筛选起点",
        effectiveTo: "有效时间筛选终点",
        factId: "健康事实标识",
        expectedFactVersion: "预期的当前事实版本",
        expectedProfileVersion: "预期的当前画像版本",
        decision: "事实处理方式",
        replacement: "用于替换的新事实内容",
        reason: "处理原因",
        deleteScope: "删除范围",
        eventTypes: "筛选更改记录的事件类型",
        testData: "菌群检测来源及下载地址",
        microbiomeReportData: "结构化菌群报告资料",
        microbiomeData: "兼容入口的原始结构化菌群数据",
    };
    const f = (name, type = "text", options = {}) => ({name, type, description: descriptions[name], ...options});
    const user = () => f("userId", "text", {required: true});
    const session = () => f("sessionId", "text", {required: true});
    const ids = () => [user(), session()];
    const subject = () => f("subjectId", "text", {hint: "留空由服务端选择本人"});
    const reasoningEffort = () => f("reasoningEffort", "text", {
        choices: ["low", "high", "max"], choiceLabels: {low: "低", high: "高", max: "最高"},
        serverDefault: "low", hint: "仅 thinking=true 时生效",
    });
    const thinking = (serverDefault = false) => f("thinking", "boolean", {serverDefault});
    const requestId = () => f("requestId", "text", {required: true, generate: "test-request", hint: "重复请求保留此值可验证幂等"});
    const paging = () => [f("cursor", "text", {hint: "游标与当前用户、主体及过滤条件绑定"}), f("limit", "integer", {serverDefault: 20, hint: "1–100"})];
    const reportTypes = ["evaluate", "intervene", "instruction", "opinion", "explain"];
    const reportTypeLabels = {evaluate: "评估报告", intervene: "干预方案", instruction: "临床医嘱", opinion: "诊断意见", explain: "报告解读"};
    const observationViewLabels = {standard: "标准", complete: "完整"};
    const reference = () => [f("finalReportId"), f("reportType", "text", {
        choices: reportTypes, choiceLabels: reportTypeLabels,
        hint: "随 finalReportId 一起使用；只提供批次时读取 evaluate",
    })];
    const fileList = (max, options = {}) => f("fileList", "array", {
        maxItems: max,
        hint: `最多 ${max} 份；每项使用 {documentId} 或 {url, fileType}，不能混用`,
        example: [{url: "https://example.com/report.pdf", fileType: "pdf"}],
        ...options,
    });
    const mutation = () => [user(), f("subjectId", "text", {required: true}), f("factId", "text", {required: true}),
        f("expectedFactVersion", "integer", {required: true}), f("expectedProfileVersion", "integer", {required: true}), requestId()];
    const endpoints = [];
    function add(id, group, title, path, fields = [], extra = {}) {
        endpoints.push({id, group, title, path, method: "POST", fields, ...extra});
    }
    add("health", "health", "健康检查", "/health", [], {method: "GET"});
    add("health.live", "health", "存活检查", "/health/live", [], {method: "GET"});
    add("health.ready", "health", "就绪检查", "/health/ready", [], {method: "GET"});
    add("health-analysis.generate", "health-analysis", "生成健康分析", "/api/v1/health-analysis", [
        user(), f("name", "text", {required: true, hint: "本次明确确认的本人姓名，去除首尾空白后不可为空"}),
        f("age", "integer", {required: true, hint: "本次确认年龄，0–150岁的整数"}),
        f("gender", "text", {required: true, choices: ["男", "女"]}),
        f("groupId", "text", {required: true, hint: "待查询的群组标识"}),
        f("surveyList", "array", {required: true, preset: "[]", maxItems: 20,
            hint: "最多20份；每项是 {schema, answer}，answer直接为题目name到答案值的映射。全部旧题参与去重，允许部分未答。",
            example: [{schema: {title: "已有生活问卷", pages: [{name: "daily", elements: [{type: "radiogroup", name: "activity_days",
                title: "您一周有几天进行锻炼？", choices: [{value: "days_1_2", text: "1～2天"}, {value: "unknown", text: "不清楚"}]}]}]},
                answer: {activity_days: "days_1_2"}}]}),
    ], {timeoutMs: 180000, hint: "同步等待四项完整结果。没有个人资料时仍生成基础卷；仅合格新题耗尽时无新卷。客户端不提交到旧问卷接口。"});
    add("analysis.health-report", "interpret", "体检报告解读", "/api/v1/interpret/health-report", [
        user(), f("fileId", "text", {hint: "HTTP(S) 文件 URL；与 documentId 二选一"}),
        f("documentId", "text", {hint: "复用当前用户、主体已有文档；与 fileId 二选一"}), subject(), f("reportId"),
    ], {exactlyOne: [["fileId", "documentId"]]});
    add("record.create", "record", "创建建档", "/api/v1/qa/create", [...ids(), f("name"),
        f("basicInfo", "object", {example: {gender: "男", age: 30, height: 175, weight: 70}}), fileList(5),
        f("hospitalData", "array", {example: [{diagnosis: "高血压", recordedAt: "2026-08-18T09:00:00+08:00"}]}),
        f("weRunData", "array", {example: [{date: "2026-08-17", steps: 8200}]}),
        f("doctorProductPackages", "array", {example: [{id: 1, packageName: "健康管理体验包", products: [{id: 101, serviceName: "基础健康咨询"}]}]}),
        f("smartDeviceData", "object", {example: {restingHeartRate: {value: 72, unit: "bpm", observedAt: "2026-08-18T08:00:00+08:00"}}}),
    ]);
    const answer = () => [...ids(), f("questionId", "text", {required: true}),
        f("answer", "json", {example: {content: "近期饭后腹胀"}, hint: "后端接受字符串、对象或数组；字符串需要双引号。文字也可直接填入 other"}),
        f("other", "text", {multiline: true, hint: "非空时优先于 answer；补充入口可一次提出多个疑问，或回答“没有了”"}), thinking(), reasoningEffort()];
    add("record.answer.complete", "record", "提交答案（完整）", "/api/v1/qa/answer/complete", answer(), {draftKey: "record.answer", atLeastOne: [["answer", "other"]]});
    add("record.answer.stream", "record", "提交答案（SSE）", "/api/v1/qa/answer/stream", answer(), {draftKey: "record.answer", stream: true, resultStatuses: ["question", "finished"], atLeastOne: [["answer", "other"]]});
    add("record.status", "record", "恢复建档状态", "/api/v1/qa/status", ids());
    add("record.history", "record", "建档历史", "/api/v1/qa/history", ids());
    add("record.continue", "record", "兼容：继续建档（已弃用）", "/api/v1/qa/continue", [...ids(), f("round", "integer", {required: true, preset: 1}), thinking()],
        {hint: "新流程通过答案提交自动推进。本接口保留用于验证兼容行为：有效请求返回 HTTP 409，不会开启续轮。"});
    add("record.complete", "record", "兼容：读取完成结果", "/api/v1/qa/complete", [...ids(), f("round", "integer", {required: true, preset: 1})],
        {hint: "仅幂等读取已完成会话，不能提前结束活动问答；未完成或 round 不匹配返回 HTTP 409。新流程 round 固定为 1，旧完成历史可使用原轮次。"});
    add("questionnaire.create", "questionnaire", "生成整卷问卷", "/api/v1/record/questionnaires", [...ids(), fileList(5)],
        {hint: "生成并缓存当前会话的整份问卷；无附件也可生成。202 表示后台处理中。"});
    add("questionnaire.status", "questionnaire", "恢复整卷问卷", "/api/v1/record/questionnaires", ids(), {method: "GET"});
    add("questionnaire.submit", "questionnaire", "提交整卷答案", "/api/v1/record/questionnaires/{questionnaireId}/answers", [
        f("questionnaireId", "text", {required: true, location: "path"}),
        f("userId", "text", {required: true, location: "query"}),
        f("answers", "object", {required: true, preset: "{}", example: {}, hint: "题目 name 到答案值的映射；允许空对象，不预选未知或以上均无。"}),
    ], {hint: "questionnaireId 编码到路径，userId 编码到查询参数；HTTP 正文仅包含 answers。相同答案可重试，不同答案返回 409。"});
    add("questionnaire.factor-assessment", "questionnaire", "查询因素分析", "/api/v1/record/questionnaires/{questionnaireId}/factor-assessment", [
        f("questionnaireId", "text", {required: true, location: "path"}),
        f("userId", "text", {required: true, location: "query"}),
    ], {method: "GET", hint: "只读取已提交问卷的独立因素分析，不触发生成。202 表示排队或处理中；200 返回完成、失败或不可用状态。"});
    add("chat.suggestions", "chat", "推荐问题", "/api/v1/chat/suggestions", [f("userId"), f("count", "integer", {serverDefault: 3, hint: "1–10；不发送 userId 可测试匿名推荐"})], {method: "GET"});
    add("chat.messages.custom", "chat", "插入医生卡片", "/api/v1/chat/messages/custom", [...ids(),
        f("eventId", "text", {required: true, generate: "test-doctor-event"}), f("content", "text", {required: true, multiline: true}),
        f("payload", "object", {example: {reportId: "test-report", title: "健康评估报告"}}),
        f("displayType", "text", {choices: ["CARD"], choiceLabels: {CARD: "卡片"}, serverDefault: "CARD"}),
        f("customType", "text", {serverDefault: "DOCTOR_MESSAGE_CARD", hint: "可自定义；例如 HEALTH_REPORT_CARD、INTERVESE_CARD、DIAGNOSIS_CARD"}), f("title"),
    ]);
    const chatSend = () => [...ids(), f("message", "text", {required: true, multiline: true}), thinking(true), reasoningEffort(), ...reference()];
    add("chat.send.complete", "chat", "发送消息（完整）", "/api/v1/chat/send/complete", chatSend(), {draftKey: "chat.send", requires: [["reportType", "finalReportId"]]});
    add("chat.send.stream", "chat", "发送消息（SSE）", "/api/v1/chat/send/stream", chatSend(), {draftKey: "chat.send", stream: true, requires: [["reportType", "finalReportId"]]});
    add("chat.history", "chat", "对话历史", "/api/v1/chat/history", ids());
    add("health-profile.get", "health-profile", "读取画像", "/api/v1/health-profile/get", [user(), subject(),
        f("documentObservationView", "text", {
            choices: ["standard", "complete"], choiceLabels: observationViewLabels, serverDefault: "standard",
            hint: "完整视图仍沿用事实、趋势及概览的准入规则",
        }),
        f("includeEvidence", "boolean", {serverDefault: false}),
        f("factStatuses", "array", {
            choices: ["active", "pending", "disputed"], choiceLabels: {active: "已生效", pending: "待确认", disputed: "有争议"},
            example: ["pending", "disputed"], serverDefault: ["active", "pending", "disputed"],
            hint: "仅筛选当前健康事实（factGroups），省略时包含已生效、待确认和有争议的事实",
        }),
        f("indicatorScope", "array", {
            choices: ["all", "abnormal", "flagged", "pending", "unjudged"],
            choiceLabels: {
                all: "全部（all）",
                abnormal: "临床异常（abnormal）",
                flagged: "来源标记异常（flagged）",
                pending: "事实待核实（pending）",
                unjudged: "判定依据不足（unjudged）",
            },
            serverDefault: "all", hint: "事实待核实按事实状态筛选，判定依据不足按结果判定筛选",
        }),
        f("indicatorLimit", "integer", {serverDefault: 12, hint: "1–50"}),
    ]);
    add("health-profile.subjects.list", "health-profile", "健康主体列表", "/api/v1/health-profile/subjects/list", [user(), f("includeArchived", "boolean", {serverDefault: false})]);
    add("health-profile.document-observations.list", "health-profile", "文档观测列表", "/api/v1/health-profile/document-observations/list", [user(), subject(), f("documentId"),
        f("view", "text", {choices: ["standard", "complete"], choiceLabels: observationViewLabels, serverDefault: "standard", hint: "完整视图展示已保存的解读及其限制"}), ...paging()]);
    add("health-profile.subjects.upsert", "health-profile", "新增 / 更新主体", "/api/v1/health-profile/subjects/upsert", [user(), requestId(), f("subjectId"),
        f("kind", "text", {
            choices: ["self", "blood_relative", "non_blood_family", "friend", "other"],
            choiceLabels: {self: "本人", blood_relative: "血亲", non_blood_family: "非血亲家属", friend: "朋友", other: "其他"},
            hint: "创建时必填；self 仅支持更新已有本人",
        }),
        f("expectedSubjectVersion", "integer", {hint: "更新已有主体时必填"}), f("displayName"),
        f("relationship", "object", {example: {relationType: "father", isBloodRelative: true}}),
    ], {hint: "创建时填写 kind，省略 subjectId / expectedSubjectVersion；更新时填写主体及其当前版本。"});
    add("health-profile.facts.list", "health-profile", "健康事实列表", "/api/v1/health-profile/facts/list", [user(), subject(),
        f("categories", "array", {example: ["laboratory", "condition"]}), f("statuses", "array", {example: ["active", "pending", "disputed"]}),
        f("factTypes", "array", {example: ["lab.alt"]}), f("effectiveFrom", "text", {hint: "带时区 ISO 8601"}),
        f("effectiveTo", "text", {hint: "带时区 ISO 8601"}), ...paging(),
    ]);
    add("health-profile.facts.resolve", "health-profile", "确认 / 拒绝 / 更正事实", "/api/v1/health-profile/facts/resolve", [...mutation(),
        f("decision", "text", {
            required: true, choices: ["confirm", "reject", "replace"],
            choiceLabels: {confirm: "确认", reject: "拒绝", replace: "更正"}, preset: "confirm",
        }),
        f("replacement", "object", {hint: "仅 replace 时填写；confirm / reject 时留空", example: {valueType: "quantity", value: {number: 70, unit: "kg"}, effectivePeriod: {start: "2026-09-10T00:00:00Z", end: null}}}), f("reason"),
    ], {confirm: "确认处理这条事实？该操作会写入真实画像。", when: [{field: "decision", value: "replace", required: ["replacement"]}]});
    add("health-profile.facts.delete", "health-profile", "删除事实", "/api/v1/health-profile/facts/delete", [...mutation(),
        f("deleteScope", "text", {required: true, preset: "fact", hint: "当前后端支持 fact"}), f("reason"),
    ], {confirm: "确认删除这条画像事实？事实值将立即从画像中清除。"});
    add("health-profile.changes.list", "health-profile", "画像变更记录", "/api/v1/health-profile/changes/list", [user(), subject(),
        f("eventTypes", "array", {example: ["subject_created", "fact_confirmed"]}), ...paging(),
    ]);
    add("report.submit", "report", "生成最终报告", "/api/v1/report", [...ids(), f("name"), f("age", "integer"), f("sex"),
        fileList(10, {urlInput: true,
            placeholder: "https://example.com/report.pdf\n或填写完整的 fileList JSON 数组",
            hint: "最多 10 份；可直接粘贴 HTTPS 文件 URL，每行一个，自动生成 {url, fileType}。类型无法识别时在下方选择；也可填写 JSON 数组，使用 {documentId} 或 {url, fileType}，不能混用。",
        }), f("testData", "array", {example: [{resultDetailUrl: "https://example.com/microbiome.pdf"}]}),
        f("hospitalData", "array", {example: [{dataType: "INSPECTION", itemName: "血糖", testResultValue: 5.2, referenceRange: "3.9–6.1"}]}),
        f("microbiomeReportData", "object", {hint: "可选的结构化菌群报告数据，按生产 ReportRequest 契约提供。"}),
        f("microbiomeData", "object", {example: {sample: {sampleId: "SAMPLE-DEMO", sampleType: "粪便", reportDate: "2026-09-10"}, overallMetrics: [{code: "gmwi2", name: "肠道微生态健康指数", value: "-0.2"}]}}),
    ], {confirm: "确认执行真实报告后台任务？任务可能调用 LLM、证据服务并向已配置地址发送回调。"});
    add("report.list", "report", "最终报告列表", "/api/v1/report/list", [user(), f("sessionId", "text", {hint: "选择不发送可查询此用户全部会话"}), ...paging()]);
    add("report.get", "report", "最终报告详情", "/api/v1/report/get", [user(), f("finalReportId", "text", {required: true}),
        f("reportType", "text", {required: true, choices: reportTypes, choiceLabels: reportTypeLabels, preset: "evaluate"}),
    ]);
    return endpoints;
})();
if (typeof module !== "undefined") module.exports = ApiLabContracts;
