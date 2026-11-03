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
