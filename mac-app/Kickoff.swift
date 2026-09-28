// Kickoff: the Mac window. Drop shoot folders, cards or a song on the window (or the Dock icon), press
// Start, and the engine (musicsync.py) builds the Premiere project while the window shows progress.
//
// The window itself is ui/index.html in a web view; this file only does what a web page can't:
// accept folder drops, run the engine, keep the history of runs, and open files. The installer compiles it with the Command
// Line Tools (swiftc), and kickoff-update.sh recompiles it when a new version arrives from GitHub.
import Cocoa
import WebKit
import UniformTypeIdentifiers

let support = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/Kickoff")
let historyFile = support.appendingPathComponent("history.json")
let settingsFile = support.appendingPathComponent("settings.json")   // the Settings page, read by the engine
let audioExtensions: Set<String> = ["wav", "aif", "aiff", "bwf", "mp3", "m4a", "flac", "aac", "caf"]
let videoExtensions: Set<String> = ["mov", "mp4", "mxf", "m4v", "mts", "m2ts", "avi", "mkv", "r3d", "braw",
                                    "insv", "360", "wmv", "webm"]

// what the window takes: folders (a shoot, a day, a card) and audio files (the song)
func usable(_ urls: [URL]) -> [URL] {
    urls.filter { url in
        (try? url.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true
            || audioExtensions.contains(url.pathExtension.lowercased())
            || videoExtensions.contains(url.pathExtension.lowercased())     // XMLs, text, stills: left out
    }
}

final class DropWebView: WKWebView {
    var onDrop: (([URL], CGPoint) -> Void)?           // what was dropped, and where (page coordinates)
    var onDragging: ((Bool, CGPoint) -> Void)?

    override init(frame: CGRect, configuration: WKWebViewConfiguration) {
        super.init(frame: frame, configuration: configuration)
        registerForDraggedTypes([.fileURL])
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func items(_ info: NSDraggingInfo) -> [URL] {
        let objs = info.draggingPasteboard.readObjects(forClasses: [NSURL.self],
                                                       options: [.urlReadingFileURLsOnly: true]) ?? []
        return usable(objs.compactMap { $0 as? URL })
    }

    // the page's own coordinates (top-left origin), so it can tell which box a drop landed on
    private func pagePoint(_ info: NSDraggingInfo) -> CGPoint {
        let p = convert(info.draggingLocation, from: nil)
        return CGPoint(x: p.x, y: isFlipped ? p.y : bounds.height - p.y)
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        guard !items(sender).isEmpty else { return [] }
        onDragging?(true, pagePoint(sender))
        return .copy
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        guard !items(sender).isEmpty else { return [] }
        onDragging?(true, pagePoint(sender))
        return .copy
    }

    override func draggingExited(_ sender: NSDraggingInfo?) { onDragging?(false, .zero) }

    override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool { !items(sender).isEmpty }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        onDragging?(false, .zero)
        let urls = items(sender)
        guard !urls.isEmpty else { return false }
        onDrop?(urls, pagePoint(sender))
        return true
    }

    override func concludeDragOperation(_ sender: NSDraggingInfo?) {}
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    var window: NSWindow!
    var web: DropWebView!
    var proc: Process?
    var ready = false
    var stopped = false
    var pending: [URL] = []                // dropped before the page was ready
    var updateNote = ""
    var mode = "auto"                     // auto / music / setup, from the switch in the window
    var rebuild = false                   // "start over" box: ignore earlier runs on the folder
    var xmlName = ""                      // what the XML is called ("": the footage folder's name)
    var xmlDir = "drive"                  // "Export XML to": a folder, or "drive" (top of the footage's drive)

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "kickoff")
        let frame = NSRect(x: 0, y: 0, width: 620, height: 780)
        web = DropWebView(frame: frame, configuration: cfg)
        web.setValue(false, forKey: "drawsBackground")          // no white flash while loading
        web.navigationDelegate = self
        web.onDrop = { [weak self] urls, at in self?.stage(urls, at: at) }
        web.onDragging = { [weak self] on, at in self?.js("Kickoff.dragging(\(on), \(Int(at.x)), \(Int(at.y)))") }

        window = NSWindow(contentRect: frame, styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Kickoff"
        window.titlebarAppearsTransparent = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(srgbRed: 0x17 / 255.0, green: 0x19 / 255.0, blue: 0x1d / 255.0, alpha: 1)
        window.minSize = NSSize(width: 520, height: 620)
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("KickoffMain")
        // open at the screen's full height (under the menu bar, above the Dock), wide enough for
        // the page to fit without scrolling
        if let vis = (window.screen ?? NSScreen.main)?.visibleFrame {
            let w = min(vis.width, max(window.frame.width, 1180))
            window.setFrame(NSRect(x: vis.midX - w / 2, y: vis.minY, width: w, height: vis.height), display: true)
        }
        window.makeKeyAndOrderFront(nil)

        let ui = support.appendingPathComponent("ui")
        web.loadFileURL(ui.appendingPathComponent("index.html"), allowingReadAccessTo: ui)
        NSApp.activate(ignoringOtherApps: true)
        checkForUpdates()
    }

    // folders dropped on the Dock icon, or on the app in Finder
    func application(_ sender: NSApplication, open urls: [URL]) {
        stage(usable(urls))
    }

    // hand to the window, which puts each in the box it was dropped on; nothing runs until Start
    func stage(_ urls: [URL], at: CGPoint? = nil) {
        guard !urls.isEmpty else { return }
        guard ready else { pending += urls; return }
        window.makeKeyAndOrderFront(nil)
        let paths = json(urls.map { $0.path })
        if let at = at {
            js("Kickoff.drop ? Kickoff.drop(\(paths), \(Int(at.x)), \(Int(at.y))) : Kickoff.add(\(paths))")
        } else {
            js("Kickoff.add(\(paths))")
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window?.makeKeyAndOrderFront(nil)
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard proc?.isRunning == true else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "Kickoff is still syncing"
        alert.informativeText = "Quitting stops it before the Premiere project is written."
        alert.addButton(withTitle: "Keep Syncing")
        alert.addButton(withTitle: "Quit")
        if alert.runModal() == .alertSecondButtonReturn {
            stopped = true
            proc?.terminate()
            return .terminateNow
        }
        return .terminateCancel
    }

    // MARK: web page <-> app

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let cmd = body["cmd"] as? String else { return }
        let path = (body["path"] as? String).map { URL(fileURLWithPath: $0) }
        switch cmd {
        case "ready":
            ready = true
            if !updateNote.isEmpty { js("Kickoff.info(\(json(["update": updateNote])))") }
            js("Kickoff.history(\(json(loadHistory())))")
            if let data = try? Data(contentsOf: settingsFile),
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                js("Kickoff.settings && Kickoff.settings(\(json(obj)))")
            }
            if !pending.isEmpty {
                let urls = pending
                pending = []
                stage(urls)
            }
        case "mode": mode = body["mode"] as? String ?? "auto"
        case "rebuild": rebuild = body["on"] as? Bool ?? false
        case "pick": pick(target: body["target"] as? String ?? "any", row: body["row"] as? Int ?? -1)
        case "run":
            let paths = body["paths"] as? [String] ?? (body["folder"] as? String).map { [$0] } ?? []
            if let m = body["mode"] as? String { mode = m }
            xmlDir = body["xmlDir"] as? String ?? "drive"
            xmlName = (body["xmlName"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if !paths.isEmpty { run(paths) }
        case "history-add":
            if let entry = body["entry"] as? [String: Any] {
                var list = loadHistory()
                list.insert(entry, at: 0)
                saveHistory(Array(list.prefix(300)))
            }
        case "history-remove":
            if let id = body["id"] as? String {
                saveHistory(loadHistory().filter { ($0["id"] as? String) != id })
                js("Kickoff.history(\(json(loadHistory())))")
            }
        case "settings":
            if let obj = body["settings"] as? [String: Any],
               let data = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted]) {
                try? data.write(to: settingsFile, options: .atomic)
            } else {
                try? FileManager.default.removeItem(at: settingsFile)      // reset to defaults
            }
        case "history-clear":
            saveHistory([])
            js("Kickoff.history([])")
        case "stop":
            stopped = true
            proc?.terminate()
        case "title": window.title = body["text"] as? String ?? "Kickoff"
        case "premiere": if let p = path { openInPremiere(p) }
        case "report": if let p = path { openReport(p) }
        case "reveal": if let p = path { NSWorkspace.shared.activateFileViewerSelecting([p]) }
        default: break
        }
    }

    // links in the page open in the browser, never inside the window
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if action.navigationType == .linkActivated, let url = action.request.url {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        } else {
            decisionHandler(.allow)
        }
    }

    func js(_ code: String) {
        web?.evaluateJavaScript(code, completionHandler: nil)
    }

    func json(_ value: Any) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: [value]),
              let s = String(data: data, encoding: .utf8) else { return "null" }
        return String(s.dropFirst().dropLast())                  // unwrap the [ ] around it
    }

    // MARK: history of runs (a JSON list, newest first, written by the window when a run ends)

    func loadHistory() -> [[String: Any]] {
        guard let data = try? Data(contentsOf: historyFile),
              let list = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return [] }
        return list
    }

    func saveHistory(_ list: [[String: Any]]) {
        guard let data = try? JSONSerialization.data(withJSONObject: list, options: [.prettyPrinted]) else { return }
        try? data.write(to: historyFile, options: .atomic)
    }

    // MARK: running the engine

    @objc func pickFromMenu() { pick(target: "any", row: -1) }

    // target: "song" (one audio file), "footage" (folders) or "any"; row: which song/footage row
    func pick(target: String, row: Int) {
        guard proc?.isRunning != true else { NSSound.beep(); return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = target != "song"
        panel.canChooseFiles = target != "export"
        if target == "song" { panel.allowedContentTypes = [.audio] }
        else if panel.canChooseFiles { panel.allowedContentTypes = [.audiovisualContent, .folder] }
        panel.allowsMultipleSelection = target == "footage" || target == "any"
        panel.canCreateDirectories = target == "export"
        panel.prompt = target == "export" ? "Export Here" : "Add"
        panel.message = target == "song" ? "Choose the song"
            : target == "footage" ? "Choose the footage: the shoot folder, a day, cards or clips"
            : target == "export" ? "Choose the folder the Premiere XML goes in"
            : "Choose the shoot folder or cards (and the song if it isn't in the folder)"
        panel.beginSheetModal(for: window) { [weak self] result in
            guard let self = self, result == .OK else { return }
            let paths = self.json(usable(panel.urls).map { $0.path })
            if target == "any" {
                self.js("Kickoff.add(\(paths))")
            } else {
                self.js("Kickoff.picked(\(paths), \(self.json(target)), \(row))")
            }
        }
    }

    func run(_ paths: [String]) {
        guard proc?.isRunning != true else { NSSound.beep(); return }
        stopped = false
        window.makeKeyAndOrderFront(nil)
        js("Kickoff.started(\(json(paths)))")

        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [support.appendingPathComponent("kickoff-gui-run.sh").path, "--", mode, rebuild ? "rebuild" : "add"] + paths
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
        env["PYTHONUNBUFFERED"] = "1"
        env["KICKOFF_XML_DIR"] = xmlDir
        env["KICKOFF_NAME"] = xmlName
        env["KICKOFF_SETTINGS"] = settingsFile.path
        p.environment = env
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        readLines(out) { [weak self] line in
            guard let self = self else { return }
            if line.hasPrefix("@@kickoff ") {
                self.js("Kickoff.event(JSON.parse(\(self.json(String(line.dropFirst(10))))))")
            } else {
                self.js("Kickoff.log(\(self.json(line)))")
            }
        }
        readLines(err) { [weak self] line in
            guard let self = self else { return }
            self.js("Kickoff.log(\(self.json(line)))")
        }
        p.terminationHandler = { [weak self] finished in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {     // let the last lines land first
                guard let self = self else { return }
                let code = self.stopped ? -15 : Int(finished.terminationStatus)
                self.js("Kickoff.exit(\(code))")
                self.proc = nil
                if code == 0 { NSApp.requestUserAttention(.informationalRequest) }
            }
        }
        do {
            try p.run()
            proc = p
        } catch {
            js("Kickoff.log(\(json("error: can't start the engine: \(error.localizedDescription)")))")
            js("Kickoff.exit(1)")
        }
    }

    final class LineBuffer { var data = Data() }

    func readLines(_ pipe: Pipe, _ handle: @escaping (String) -> Void) {
        let buffer = LineBuffer()             // only touched from the pipe's handler, one call at a time
        pipe.fileHandleForReading.readabilityHandler = { h in
            let data = h.availableData
            if data.isEmpty {                                    // end of output
                h.readabilityHandler = nil
                if !buffer.data.isEmpty {
                    let rest = String(decoding: buffer.data, as: UTF8.self)
                    DispatchQueue.main.async { handle(rest) }
                }
                return
            }
            buffer.data.append(data)
            while let nl = buffer.data.firstIndex(of: 0x0a) {
                let line = String(decoding: buffer.data[buffer.data.startIndex..<nl], as: UTF8.self)
                buffer.data.removeSubrange(buffer.data.startIndex...nl)
                DispatchQueue.main.async { handle(line) }
            }
        }
    }

    // MARK: opening results

    func premiereApp() -> URL? {
        var found: [URL] = []
        let fm = FileManager.default
        for name in (try? fm.contentsOfDirectory(atPath: "/Applications")) ?? [] where name.hasPrefix("Adobe Premiere Pro") {
            let dir = URL(fileURLWithPath: "/Applications").appendingPathComponent(name)
            if name.hasSuffix(".app") {
                found.append(dir)
            } else {
                for inner in (try? fm.contentsOfDirectory(atPath: dir.path)) ?? []
                where inner.hasPrefix("Adobe Premiere Pro") && inner.hasSuffix(".app") {
                    found.append(dir.appendingPathComponent(inner))
                }
            }
        }
        if let newest = found.max(by: {
            $0.lastPathComponent.compare($1.lastPathComponent, options: .numeric) == .orderedAscending
        }) {
            return newest
        }
        for v in stride(from: 30, through: 22, by: -1) {
            if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.adobe.PremierePro.\(v)") {
                return url
            }
        }
        return nil
    }

    func openInPremiere(_ xml: URL) {
        NSWorkspace.shared.activateFileViewerSelecting([xml])
        guard let app = premiereApp() else {
            let alert = NSAlert()
            alert.messageText = "Premiere Pro isn't in Applications"
            alert.informativeText = "The project is selected in Finder. In Premiere, use File > Import and pick it."
            alert.beginSheetModal(for: window, completionHandler: nil)
            return
        }
        NSWorkspace.shared.open([xml], withApplicationAt: app, configuration: NSWorkspace.OpenConfiguration(),
                                completionHandler: nil)
    }

    func openReport(_ report: URL) {
        let ws = NSWorkspace.shared
        if ws.urlForApplication(toOpen: report) != nil {
            ws.open(report)
        } else if let textEdit = ws.urlForApplication(withBundleIdentifier: "com.apple.TextEdit") {
            ws.open([report], withApplicationAt: textEdit, configuration: NSWorkspace.OpenConfiguration(),
                    completionHandler: nil)
        } else {
            ws.activateFileViewerSelecting([report])
        }
    }

    // MARK: updates

    // Fetches the newest engine and window from GitHub; they apply now (engine) or next launch
    // (window). Offline, nothing changes.
    func checkForUpdates() {
        let script = support.appendingPathComponent("kickoff-update.sh")
        guard FileManager.default.fileExists(atPath: script.path) else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [script.path, Bundle.main.bundlePath]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        p.terminationHandler = { [weak self] _ in
            let text = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            DispatchQueue.main.async {
                guard let self = self, !text.isEmpty else { return }
                self.updateNote = text
                if self.ready { self.js("Kickoff.info(\(self.json(["update": text])))") }
            }
        }
        try? p.run()
    }

    // MARK: menu

    // The About box shows Kickoff's real version (the engine's VERSION), not the bundle's fixed 1.0.
    @objc func showAbout() {
        var v = "?"
        if let text = try? String(contentsOf: support.appendingPathComponent("musicsync.py"), encoding: .utf8),
           let r = text.range(of: #"VERSION = "([^"]+)""#, options: .regularExpression) {
            v = String(text[r]).replacingOccurrences(of: "VERSION = ", with: "")
                .replacingOccurrences(of: "\"", with: "")
        }
        NSApp.activate(ignoringOtherApps: true)
        NSApp.orderFrontStandardAboutPanel(options: [.applicationVersion: "Version \(v)", .version: ""])
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Kickoff", action: #selector(showAbout), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Kickoff", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Kickoff", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let fileItem = NSMenuItem()
        main.addItem(fileItem)
        let fileMenu = NSMenu(title: "File")
        let open = fileMenu.addItem(withTitle: "Add Folders…", action: #selector(pickFromMenu), keyEquivalent: "o")
        open.target = self
        fileMenu.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        fileItem.submenu = fileMenu

        let editItem = NSMenuItem()
        main.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu

        let winItem = NSMenuItem()
        main.addItem(winItem)
        let winMenu = NSMenu(title: "Window")
        winMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winItem.submenu = winMenu
        NSApp.windowsMenu = winMenu
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
