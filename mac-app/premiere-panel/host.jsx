// Kickoff panel, Premiere side: attach a finished proxy to every clip in the open project that
// uses the original file. Returns how many clips got it ("0" while the clip isn't imported yet).
function kickoffWalk(item, fn) {
    if (!item) return;
    if (item.type === ProjectItemType.BIN || item.type === ProjectItemType.ROOT) {
        for (var i = 0; i < item.children.numItems; i++) kickoffWalk(item.children[i], fn);
    } else {
        fn(item);
    }
}
function kickoffAttach(clipPath, proxyPath) {
    if (!app.project || !app.project.rootItem) return "0";
    var n = 0, want = String(clipPath);
    kickoffWalk(app.project.rootItem, function (it) {
        try {
            if (it.getMediaPath && it.getMediaPath() === want && it.canProxy() && !it.hasProxy()) {
                if (it.attachProxy(proxyPath, 0)) n++;
            }
        } catch (e) {}
    });
    return String(n);
}
