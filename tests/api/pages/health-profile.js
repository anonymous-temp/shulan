(() => {
    const $ = (selector) => document.querySelector(selector);
    const mutationIds = ["health-profile.facts.resolve", "health-profile.facts.delete"];
    const queryIds = ["health-profile.get", "health-profile.facts.list", "health-profile.changes.list", "health-profile.document-observations.list"];
    const cursors = {};
    let selectedFact = null;
    function setVersion(version) {
        if (!Number.isInteger(version)) return;
        ApiLab.setParams(mutationIds, {expectedProfileVersion: version}, {onlyDefault: true});
    }
    function metric(name, value) {
        const card = ApiLab.element("div", "metric-card");
        card.append(ApiLab.element("span", "", name), ApiLab.element("strong", "", String(value ?? "—")));
        return card;
    }
    function selectSubject(subject) {
        ApiLab.setParams([...queryIds, ...mutationIds], {subjectId: subject.subjectId}, {touch: false});
        ApiLab.setParams("health-profile.subjects.upsert", {subjectId: subject.subjectId, expectedSubjectVersion: subject.version,
            kind: subject.kind, displayName: subject.displayName, relationship: subject.relationship}, {touch: false});
        ApiLab.draft("health-profile.subjects.upsert").mode = "form";
        ApiLab.select("health-profile.subjects.upsert");
        selectedFact = null; $("#replace-selected").disabled = true;
        ApiLab.setParams(mutationIds, {factId: undefined, expectedFactVersion: undefined, expectedProfileVersion: undefined}, {touch: false});
        $("#fact-list").replaceChildren();
        for (const key of ["facts", "observations", "changes"]) { cursors[key] = null; $(`#next-${key}`).disabled = true; }
        ApiLab.setParams(queryIds, {cursor: undefined}, {touch: false});
    }
    function renderSubjects(items) {
        $("#subject-list").replaceChildren();
        for (const subject of items) {
            const button = ApiLab.element("button", "data-row selectable-row");
            button.append(ApiLab.element("strong", "", subject.displayName || subject.kind), ApiLab.element("span", "", `${subject.subjectId} · v${subject.version}`));
            button.addEventListener("click", () => selectSubject(subject));
            $("#subject-list").append(button);
        }
    }
    function renderFacts(items, subjectId, version) {
        $("#fact-list").replaceChildren();
        for (const fact of items) {
            const button = ApiLab.element("button", "data-row selectable-row");
            button.append(ApiLab.element("strong", "", `${fact.displayName || fact.factType} · ${fact.status}`), ApiLab.element("pre", "result-json", ApiLab.format(fact)));
            button.addEventListener("click", () => {
                selectedFact = {fact, subjectId, version}; $("#replace-selected").disabled = false;
                ApiLab.setParams(mutationIds, {subjectId, factId: fact.factId, expectedFactVersion: fact.version, expectedProfileVersion: version}, {touch: false});
                ApiLab.setParams("health-profile.facts.resolve", {decision: "confirm", replacement: undefined}, {touch: false});
                mutationIds.forEach((id) => { ApiLab.draft(id).mode = "form"; });
                ApiLab.select("health-profile.facts.resolve");
            });
            $("#fact-list").append(button);
        }
        if (!items.length) $("#fact-list").append(ApiLab.element("p", "empty", "暂无符合条件的事实。"));
    }
    ApiLab.initialize({group: "health-profile",
        onReset() {
            selectedFact = null; $("#replace-selected").disabled = true;
            ["#profile-summary", "#subject-list", "#fact-list", "#profile-details"].forEach((id) => $(id).replaceChildren());
            for (const key of ["facts", "observations", "changes"]) { cursors[key] = null; $(`#next-${key}`).disabled = true; }
        },
        onResponse(payload, {id}) {
            const data = payload.data || {};
            if (data.subjectId) ApiLab.setParams([...queryIds, ...mutationIds], {subjectId: data.subjectId}, {onlyDefault: true});
            setVersion(data.profileVersion);
            if (id === "health-profile.get" && data.subject) {
                $("#profile-summary").replaceChildren(metric("主体", data.subject.displayName || data.subject.kind), metric("subjectId", data.subject.subjectId), metric("profileVersion", data.profileVersion));
                ApiLab.setParams([...queryIds, ...mutationIds], {subjectId: data.subject.subjectId}, {onlyDefault: true});
                renderSubjects([data.subject]);
            }
            if (id === "health-profile.subjects.list") renderSubjects(data.subjects || []);
            if (id === "health-profile.subjects.upsert" && data.subject) renderSubjects([data.subject]);
            const key = id === "health-profile.facts.list" ? "facts" : id === "health-profile.document-observations.list" ? "observations" : id === "health-profile.changes.list" ? "changes" : null;
            if (key) { cursors[key] = data.nextCursor; $(`#next-${key}`).disabled = !data.nextCursor; }
            if (key === "facts") renderFacts(data.items || [], data.subjectId, data.profileVersion);
            if (key === "changes" || key === "observations") ApiLab.renderJson($("#profile-details"), data, {stateKey: id});
            if (mutationIds.includes(id)) {
                ApiLab.renderJson($("#profile-details"), data, {stateKey: id});
                selectedFact = null; $("#replace-selected").disabled = true;
            }
        },
    });
    $("#replace-selected").addEventListener("click", () => {
        if (!selectedFact) return;
        const {fact, subjectId, version} = selectedFact;
        ApiLab.draft("health-profile.facts.resolve").mode = "form";
        ApiLab.setParams("health-profile.facts.resolve", {subjectId, factId: fact.factId, expectedFactVersion: fact.version,
            expectedProfileVersion: version, decision: "replace",
            replacement: {valueType: fact.valueType, value: fact.value, effectivePeriod: fact.effectivePeriod}}, {touch: false});
        ApiLab.select("health-profile.facts.resolve");
    });
    for (const [key, id] of [["facts", "health-profile.facts.list"], ["observations", "health-profile.document-observations.list"], ["changes", "health-profile.changes.list"]]) {
        $(`#next-${key}`).addEventListener("click", () => {
            ApiLab.draft(id).mode = "form";
            ApiLab.setParams(id, {cursor: cursors[key]}, {touch: false}); ApiLab.select(id);
        });
    }
})();
