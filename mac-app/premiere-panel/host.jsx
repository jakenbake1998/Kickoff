// Kickoff panel, Premiere side. kickoffSync(json) gets [{clip, proxy}] for every proxy that is done and
// checked, walks the open project once, and attaches each proxy to every clip that uses its original
// file and has none (a re-import or a detached proxy gets it back). Returns, per clip path:
// "on" (at least one clip in the project has a proxy), "missing" (the clip isn't in this project).
function kickoffWalk(item, fn) {
    if (!item) return;
    if (item.type === ProjectItemType.BIN || item.type === ProjectItemType.ROOT) {
        for (var i = 0; i < item.children.numItems; i++) kickoffWalk(item.children[i], fn);
    } else {
        fn(item);
    }
}
function kickoffSync(json) {
    var jobs = eval("(" + json + ")"), out = {}, byPath = {};
    if (!app.project || !app.project.rootItem) return "{}";
    kickoffWalk(app.project.rootItem, function (it) {
        try {
            var p = it.getMediaPath ? it.getMediaPath() : "";
            if (p) (byPath[p] = byPath[p] || []).push(it);
        } catch (e) {}
    });
    for (var j = 0; j < jobs.length; j++) {
        var items = byPath[jobs[j].clip] || [], on = false;
        for (var k = 0; k < items.length; k++) {
            try {
                if (items[k].hasProxy()) { on = true; continue; }
                if (items[k].canProxy() && items[k].attachProxy(jobs[j].proxy, 0)) on = true;
            } catch (e) {}
        }
        out[jobs[j].clip] = items.length ? (on ? "on" : "off") : "missing";
    }
    var s = [];
    for (var c in out) s.push('"' + c.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '":"' + out[c] + '"');
    return "{" + s.join(",") + "}";
}

// Every clip in the open project that could take a proxy and has none: its media path, one per file.
function kickoffNoProxy() {
    var seen = {}, s = [];
    if (!app.project || !app.project.rootItem) return "[]";
    kickoffWalk(app.project.rootItem, function (it) {
        try {
            var p = it.getMediaPath ? it.getMediaPath() : "";
            if (!p || seen[p] || !/\.(mov|mp4|mxf|r3d|braw|crm|mts|m4v|avi)$/i.test(p)) return;
            if (it.canProxy() && !it.hasProxy()) {
                seen[p] = 1;
                s.push('"' + p.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"');
            }
        } catch (e) {}
    });
    return "[" + s.join(",") + "]";
}

// Every video clip in the open project, one per file: its media path and its proxy's path ("" if none).
function kickoffAll() {
    var seen = {}, s = [], q = function (x) { return '"' + String(x).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"'; };
    if (!app.project || !app.project.rootItem) return "[]";
    kickoffWalk(app.project.rootItem, function (it) {
        try {
            var p = it.getMediaPath ? it.getMediaPath() : "";
            if (!p || !/\.(mov|mp4|mxf|r3d|braw|crm|mts|m4v|avi)$/i.test(p) || !it.canProxy()) return;
            var px = "";
            try { if (it.hasProxy()) px = it.getProxyPath() || "?"; } catch (e) {}
            if (seen[p] !== undefined) {
                if (px && !s[seen[p]][1]) s[seen[p]][1] = px;
                return;
            }
            seen[p] = s.length;
            s.push([p, px]);
        } catch (e) {}
    });
    var out = [];
    for (var i = 0; i < s.length; i++) out.push("[" + q(s[i][0]) + "," + q(s[i][1]) + "]");
    return "[" + out.join(",") + "]";
}

// Queues each [{clip, out}] in Media Encoder with the given preset (.epr) and starts the queue.
// Returns how many were queued.
function kickoffEncode(json, preset) {
    var jobs = eval("(" + json + ")"), n = 0;
    try { app.encoder.launchEncoder(); } catch (e) {}
    for (var i = 0; i < jobs.length; i++) {
        try {
            var id = app.encoder.encodeFile(jobs[i].clip, jobs[i].out, preset, 1);
            if (id) n++;
        } catch (e) {}
    }
    try { app.encoder.startBatch(); } catch (e) {}
    return String(n);
}

// ---- Kickoff Adjustment Layer ----
// kickoffTracks(): the active sequence's video tracks (V1 first) and how many clips each holds.
// kickoffClips(json [track numbers]): every enabled clip on those tracks, times in ticks.
// kickoffPlace(json {seq, target, name, color, pieces:[[startTicks, endTicks]...]}): puts one adjustment layer
// piece per [start, end] on the target track, which must be empty. The panel works out the pieces.
function kickoffQ(x) { return '"' + String(x).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"'; }
function kickoffTracks() {
    var seq = app.project ? app.project.activeSequence : null;
    if (!seq) return '{"err":"Open a sequence first."}';
    var t = [];
    for (var i = 0; i < seq.videoTracks.numTracks; i++) t.push(seq.videoTracks[i].clips.numItems);
    return '{"seq":' + kickoffQ(seq.sequenceID) + ',"name":' + kickoffQ(seq.name) + ',"fr":' + kickoffQ(seq.timebase) + ',"tracks":[' + t.join(",") + ']}';
}
function kickoffClips(json) {
    var want = eval("(" + json + ")"), seq = app.project.activeSequence, out = [];
    for (var w = 0; w < want.length; w++) {
        var tr = seq.videoTracks[want[w] - 1], cl = [];
        for (var c = 0; tr && c < tr.clips.numItems; c++) {
            var ti = tr.clips[c];
            try { if (ti.disabled) continue; } catch (e) {}
            var path = "", sp = 1, rev = 0;
            try { path = ti.projectItem ? (ti.projectItem.getMediaPath() || ti.projectItem.nodeId) : ""; } catch (e) {}
            try { sp = ti.getSpeed(); } catch (e) {}
            try { rev = ti.isSpeedReversed() ? 1 : 0; } catch (e) {}
            cl.push('{"s":' + ti.start.ticks + ',"e":' + ti.end.ticks + ',"i":' + ti.inPoint.ticks + ',"o":' + ti.outPoint.ticks +
                    ',"sp":' + sp + ',"rev":' + rev + ',"src":' + kickoffQ(path) + '}');
        }
        out.push("[" + cl.join(",") + "]");
    }
    return "[" + out.join(",") + "]";
}
// The adjustment layer to use: one with the chosen name, else any adjustment layer.
function kickoffFindAdj(want) {
    var named = null, any = null;
    kickoffWalk(app.project.rootItem, function (it) {
        if (named) return;
        var adj = false;
        try { adj = it.isAdjustmentLayer ? it.isAdjustmentLayer() : false; } catch (e) {}
        if (!adj) {
            try { adj = /adjustment layer/i.test(it.name) && !it.getMediaPath() && !it.isSequence(); } catch (e) {}
        }
        if (!adj) return;
        if (it.name === want) named = it;
        else if (!any) any = it;
    });
    return named || any;
}
function kickoffTime(ticks) { var t = new Time(); t.ticks = String(ticks); return t; }
function kickoffPlace(json) {
    var job = eval("(" + json + ")"), seq = app.project.activeSequence;
    if (!seq || seq.sequenceID !== job.seq) return '{"err":"The sequence changed. Press the button again."}';
    var tr = seq.videoTracks[job.target - 1];
    if (!tr) return '{"err":"V' + job.target + ' does not exist."}';
    if (tr.clips.numItems) return '{"err":"V' + job.target + ' is not empty. Clear it or pick an empty track."}';
    var name = job.name || "Kickoff Adjustment Layer", adj = kickoffFindAdj(name), colored = 0;
    if (!adj) return '{"err":"noadj"}';
    // the layer's own project item takes the label first (only when it's the one with this name, so
    // another adjustment layer isn't recolored); new clips start with their item's label
    if (adj.name === name) { try { adj.setColorLabel(job.color); colored = 1; } catch (x) {} }
    var placed = 0, off = 0;
    for (var p = 0; p < job.pieces.length; p++) {
        var s = job.pieces[p][0], e = job.pieces[p][1], ti = null;
        try { tr.overwriteClip(adj, kickoffTime(s)); } catch (x) {}
        // pieces go left to right on an empty track, so the new one is clip number p (else look for it)
        if (tr.clips.numItems > p && String(tr.clips[p].start.ticks) === String(s)) ti = tr.clips[p];
        for (var c = 0; !ti && c < tr.clips.numItems; c++) if (String(tr.clips[c].start.ticks) === String(s)) ti = tr.clips[c];
        if (!ti) continue;
        try { ti.end = kickoffTime(e); } catch (x) {}
        try { ti.name = name; } catch (x) {}
        // and on the clip itself, where this Premiere allows it
        try { if (ti.setColorLabel) { ti.setColorLabel(job.color); colored++; } } catch (x) {}
        placed++;
        if (String(ti.end.ticks) !== String(e)) off++;
    }
    return '{"placed":' + placed + ',"color":' + (colored ? 1 : 0) + ',"off":' + off + ',"total":' + job.pieces.length + ',"adj":' + kickoffQ(adj.name) + '}';
}

// ---- Sync tab (the Kickoff window inside the panel) ----
// The open project's name (no .prproj), so the XML is named after it, and importing an XML into it.
function kickoffProjectName() {
    try { return String(app.project.name).replace(/\.prproj$/i, ""); } catch (e) { return ""; }
}
function kickoffImport(path) {
    try { return app.project.importFiles([path], true, app.project.rootItem, false) ? "1" : "0"; } catch (e) { return "0"; }
}
