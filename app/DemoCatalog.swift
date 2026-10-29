import Foundation

enum DemoCatalog {
    private static func resource(_ name: String) -> Data {
        try! Data(contentsOf: Bundle.main.url(forResource: name, withExtension: "json")!)
    }
    static func snapshot() -> Snapshot {
        try! JSONDecoder().decode(Snapshot.self, from: resource("demo-catalog"))
    }
    static func reminderSettings() -> (enabled: Bool, weekday: Int, time: String) {
        let object = try! JSONSerialization.jsonObject(with: resource("demo-catalog")) as! [String: Any]
        let settings = object["quietReminder"] as? [String: Any] ?? [:]
        return (settings["enabled"] as? Bool ?? false,
                settings["weekday"] as? Int ?? 1,
                settings["time"] as? String ?? "09:00")
    }
    static func reply<T: Decodable>(_ args: [String], as type: T.Type) -> T? {
        var data: Data
        switch args.first {
        case "snapshot": data = resource("demo-catalog")
        case "task-list": data = resource("demo-tasks")
        // A read-only check: the Remote sheet and the GitHub browser run it when they open.
        case "github-status":
            data = try! JSONSerialization.data(withJSONObject: ["authenticated": true, "login": "example", "owners": ["example"],
                                                                "message": "Example account. The example catalog is read-only."])
        case "github-repos":
            func repository(_ name: String, _ description: String, pushed: String, local: String? = nil) -> [String: Any] {
                ["id": "example/" + name, "name": name, "owner": "example", "slug": "example/" + name,
                 "url": "https://github.com/example/\(name).git", "isPrivate": true, "archived": false,
                 "description": description, "pushedAt": pushed, "alreadyLocal": local != nil, "localPaths": local.map { [$0] } ?? []]
            }
            data = try! JSONSerialization.data(withJSONObject: [
                "repositories": [repository("aurora-notes", "Example notes app", pushed: "2026-10-14T09:00:00Z", local: "/Users/demo/Projects/Aurora Notes"),
                                 repository("orbit-maps", "Example map viewer", pushed: "2026-09-02T09:00:00Z"),
                                 repository("quartz-docs", "Example documentation site", pushed: "2026-06-21T09:00:00Z")],
                "owner": "example", "truncated": false, "message": "Example repositories. The example catalog is read-only."])
        case "wizard-state":
            let object = try! JSONSerialization.jsonObject(with: resource("demo-tasks")) as! [String: Any]
            let review = object["demoWizardReview"] as! [String: Any]
            let thaw = object["demoWizardThaw"] as! [String: Any]
            data = try! JSONSerialization.data(withJSONObject: ["rules": review["rules"]!,
                "cacheKinds": ["node_modules", ".cache"], "last": review, "thawLast": thaw["result"]!])
        case "wizard-thaw-prepare":
            let object = try! JSONSerialization.jsonObject(with: resource("demo-tasks")) as! [String: Any]
            let thaw = object["demoWizardThaw"] as! [String: Any]
            // A capture passes its view name; an interactive Thaw passes the selected archive IDs.
            thawSelection = args.dropFirst().first.flatMap { $0.data(using: .utf8) }
                .flatMap { try? JSONDecoder().decode([String].self, from: $0) }
            data = try! JSONSerialization.data(withJSONObject: thawPlan(thaw["review"] as! [String: Any], ids: thawSelection))
        case "wizard-thaw-execute":
            let object = try! JSONSerialization.jsonObject(with: resource("demo-tasks")) as! [String: Any]
            let thaw = object["demoWizardThaw"] as! [String: Any]
            data = try! JSONSerialization.data(withJSONObject: thawPlan(thaw["result"] as! [String: Any], ids: thawSelection))
        case "task-plan":
            let object = try! JSONSerialization.jsonObject(with: resource("demo-tasks")) as! [String: Any]
            data = try! JSONSerialization.data(withJSONObject: object["demoTaskReview"]!)
        case "wizard-result":
            let tasks = try! JSONSerialization.jsonObject(with: resource("demo-tasks")) as! [String: Any]
            let catalog = try! JSONSerialization.jsonObject(with: resource("demo-catalog")) as! [String: Any]
            var result = tasks["demoWizardReview"] as! [String: Any]
            result["status"] = "finished"
            result["message"] = "Demo only: the listed actions completed; Trash remains available for restore."
            var rows = result["rows"] as! [[String: Any]]
            for index in rows.indices {
                rows[index]["status"] = "done"
                rows[index]["message"] = ""
            }
            result["rows"] = rows
            let results = catalog["demoResults"] as! [String: Any]
            result["outcome"] = results["wizard"]!
            data = try! JSONSerialization.data(withJSONObject: result)
        case "preview":
            let object = try! JSONSerialization.jsonObject(with: resource("demo-catalog")) as! [String: Any]
            let projects = object["projects"] as! [[String: Any]]
            let paths = Set(args.dropFirst())
            let candidates = projects.flatMap { ($0["candidates"] as? [[String: Any]]) ?? [] }
                .filter { paths.contains($0["path"] as? String ?? "") }
            let items: [[String: Any]] = candidates.map { ["path": $0["path"]!, "owner": $0["owner"]!, "size": $0["size"]!] }
            let total = candidates.reduce(Int64(0)) { $0 + (($1["size"] as? NSNumber)?.int64Value ?? 0) }
            let held = projects.contains { project in
                (project["hold"] as? Bool ?? false) && items.contains { ($0["owner"] as? String) == (project["id"] as? String) }
            }
            var outcome = (object["reviewOutcomes"] as! [String: [String: Any]])["cleanup"]!
            outcome["movedToTrash"] = items.count
            outcome["held"] = []
            data = try! JSONSerialization.data(withJSONObject: ["id": "demo-cleanup", "created": "2026-10-01T10:00:00+00:00",
                "items": items, "blocked": [], "totalSize": total, "held": held, "outcome": outcome])
        case "task-history":
            let object = try! JSONSerialization.jsonObject(with: resource("demo-tasks")) as! [String: Any]
            let items = object["tasks"] as! [[String: Any]]
            data = try! JSONSerialization.data(withJSONObject: ["runs": items.compactMap { $0["lastRun"] }])
        case "space-report":
            let object = try! JSONSerialization.jsonObject(with: resource("demo-catalog")) as! [String: Any]
            data = try! JSONSerialization.data(withJSONObject: object["spaceReport"]!)
        default: return nil
        }
        return try? JSONDecoder().decode(type, from: data)
    }
    private static var thawSelection: [String]?
    // Like the backend, a Thaw plan holds one row per selected archive, in selection order.
    private static func thawPlan(_ plan: [String: Any], ids: [String]?) -> [String: Any] {
        guard let ids else { return plan }
        var plan = plan
        let all = plan["rows"] as! [[String: Any]]
        let rows = ids.compactMap { id in all.first { ($0["id"] as? String) == id } }
        let paths = Set(rows.flatMap { [$0["path"] as? String ?? ""] + ($0["checkouts"] as? [String] ?? []) })
        let locations = Set(rows.compactMap { $0["archiveLocation"] as? String })
        func keep(_ items: Any?) -> [[String: Any]] {
            (items as? [[String: Any]] ?? []).filter { paths.contains($0["path"] as? String ?? "") }
        }
        plan["rows"] = rows
        plan["held"] = keep(plan["held"])
        if var outcome = plan["outcome"] as? [String: Any] {
            outcome["held"] = keep(outcome["held"])
            if outcome["refsRestored"] != nil { outcome["refsRestored"] = keep(outcome["refsRestored"]) }
            for (key, allowed) in [("checkoutsRestored", paths), ("archivesUsed", locations)] {
                guard var entry = outcome[key] as? [String: Any] else { continue }
                let kept = (entry["paths"] as? [String] ?? []).filter { allowed.contains($0) }
                entry["paths"] = kept
                entry["count"] = kept.count
                outcome[key] = entry
            }
            plan["outcome"] = outcome
        }
        return plan
    }
    // The Thaw wizard selects from the same Frozen archives that the Freezer lists.
    static func wizardThawArchives() -> [FrozenArchive] {
        (snapshot().archives ?? []).filter { $0.state == "frozen" }
    }
}

// Demo-only capture uses the existing smoke path; it never reads the real catalog.
import AppKit
import SwiftUI
import CoreGraphics
import Darwin

extension AppUpdater {
    static func demoReadyUpdate() -> AppUpdater {
        let updater = demoMarkdownUpdate()
        updater.status = "Download verified. Ready to install and relaunch Igloo."
        updater.downloaded = URL(fileURLWithPath: "/fixture/Igloo-update.zip")
        return updater
    }
}

struct DemoCaptureSheet: Identifiable {
    var view: String
    var id: String { view }
}

enum DemoCapture {
    static let extraSheetViews: Set<String> = ["freeze-review", "preserve-review", "freeze-result", "freeze-failure", "freeze-hold",
        "freeze-no-remote", "freeze-no-files", "freeze-no-commit", "freeze-own-source", "freeze-process",
        "cleanup-review", "cleanup-result", "cleanup-hold", "trash-history", "wizard-review", "wizard-result",
        "wizard-start", "wizard-thaw-selection", "wizard-thaw-review", "wizard-thaw-ready", "wizard-thaw-result",
        "wizard-thaw-occupied", "wizard-thaw-remote", "wizard-thaw-corrupt", "wizard-thaw-not-frozen",
        "wizard-thaw-rehydrating",
        "task-review", "task-history", "settings", "settings-cloud", "repositories-loading"]
    static let updateViews = AppUpdater.demoFailureNames.map { "update-" + $0 } + ["update-markdown", "update-ready"]
    static let sheetViews = extraSheetViews.union(["freeze", "shelf"]).union(updateViews)
    struct Options {
        let folder: URL?
        let size: NSSize?
        let view: String
        let scope: String
        let rail: String
        let data: String
        let windowServer: Bool
        let orcaCapture: Bool
    }

    static let options: Options? = {
        let args = CommandLine.arguments
        guard args.contains("--demo") else { return nil }
        let flags = ["--demo-capture", "--demo-size", "--demo-view", "--demo-scope", "--demo-rail", "--demo-data",
                     "--demo-window-server", "--demo-orca"]
        guard flags.contains(where: args.contains) else { return nil }
        func value(_ flag: String, default fallback: String) -> String {
            guard let i = args.firstIndex(of: flag) else { return fallback }
            guard i + 1 < args.count, !args[i + 1].hasPrefix("--") else { fail("Missing value for \(flag)") }
            return args[i + 1]
        }
        let sizeText = value("--demo-size", default: "")
        var size: NSSize?
        if !sizeText.isEmpty {
            let pieces = sizeText.split(separator: "x", omittingEmptySubsequences: false)
            guard pieces.count == 2, let width = Int(pieces[0]), let height = Int(pieces[1]),
                  (850...4096).contains(width), (700...4096).contains(height) else { fail("Invalid --demo-size") }
            size = NSSize(width: CGFloat(width), height: CGFloat(height))
        }
        let view = value("--demo-view", default: "overview")
        let scope = value("--demo-scope", default: "shown")
        let rail = value("--demo-rail", default: "full")
        let data = value("--demo-data", default: "normal")
        guard (Set(["overview", "git", "worktrees", "local-files", "cleanup", "plan", "freeze", "shelf", "hold"] + updateViews).union(extraSheetViews)).contains(view),
              ["shown", "hidden"].contains(scope), ["compact", "full"].contains(rail),
              ["normal", "long", "empty"].contains(data),
              !(data == "empty" && view == "freeze") else { fail("Invalid demo capture scenario") }
        let folderText = value("--demo-capture", default: "")
        if args.contains("--demo-capture") && !(folderText as NSString).isAbsolutePath {
            fail("--demo-capture requires an absolute path for a new folder")
        }
        let folder = folderText.isEmpty ? nil : URL(fileURLWithPath: folderText, isDirectory: true)
        let windowServer = args.contains("--demo-window-server")
        let orcaCapture = args.contains("--demo-orca")
        if windowServer && folder == nil { fail("--demo-window-server requires --demo-capture") }
        if orcaCapture && (!windowServer || folder == nil) { fail("--demo-orca requires --demo-window-server and --demo-capture") }
        return Options(folder: folder, size: size, view: view, scope: scope, rail: rail, data: data,
                       windowServer: windowServer, orcaCapture: orcaCapture)
    }()

    static var tab: String? {
        guard let options else { return nil }
        return ["overview": "Overview", "git": "Branches", "worktrees": "Worktrees", "local-files": "Local files",
                "cleanup": "Cleanup", "plan": "Review plan", "hold": "Review plan", "freeze": "Review plan", "shelf": "Overview"][options.view]
    }

    @MainActor static func sheetRoot(_ view: String, store: OrganizerStore) -> AnyView {
        switch view {
        case "freeze", "freeze-review", "preserve-review", "freeze-result", "freeze-failure", "freeze-hold",
             "freeze-no-remote", "freeze-no-files", "freeze-no-commit", "freeze-own-source", "freeze-process":
            guard let project = store.selected else { return AnyView(Text("No demo project")) }
            let mode = view == "preserve-review" ? "preserve" : "freeze"
            let stage: String? = view == "freeze-result" ? "result" : (view == "freeze-failure" ? "failure" : (view.hasSuffix("-review") ? "review" : nil))
            let blocker = ["freeze-no-remote": "no-remote", "freeze-no-files": "no-files", "freeze-no-commit": "no-commit",
                           "freeze-own-source": "own-source", "freeze-process": "open-processes"][view]
            return AnyView(FreezeSetupView(project: project, initialMode: mode, captureStage: stage,
                                           captureBlocker: blocker).environmentObject(store))
        case "shelf":
            return AnyView(FrozenProjectsView().environmentObject(store))
        case "trash-history":
            return AnyView(HistoryView().environmentObject(store))
        case "task-history":
            return AnyView(TaskHistoryCaptureView().environmentObject(store))
        case "repositories-loading":
            return AnyView(RemoteRepositoriesView(captureLoading: true).environmentObject(store))
        case "cleanup-result":
            guard let outcome = DemoPreservationCatalog.load()?.demoResults?.cleanup else {
                return AnyView(Text("Demo cleanup result unavailable"))
            }
            let result = CleanupResult(message: "Demo only: the folders remain recoverable in Trash.",
                                       results: [
                                        ActionResult(path: "/Users/demo/Projects/Harbor CLI/node_modules",
                                                     ok: true, message: "Moved to Trash.", size: 1_073_741_824),
                                        ActionResult(path: "/Users/demo/Projects/Harbor CLI/.cache",
                                                     ok: true, message: "Moved to Trash.", size: 536_870_912)
                                       ],
                                       outcome: outcome)
            return AnyView(CleanupResultView(result: result, onDone: {}).environmentObject(store))
        case "wizard-result":
            return AnyView(CleanupWizardView(captureResult: true).environmentObject(store))
        case "cleanup-review", "cleanup-hold":
            let plan: CleanupPlan?
            if view == "cleanup-hold",
               let project = DemoCatalog.snapshot().projects.first(where: { $0.name == "Signal Lab" }),
               let candidate = project.candidates.first {
                plan = DemoCatalog.reply(["preview", candidate.path], as: CleanupPlan.self)
            } else if let review: WizardPlan = DemoCatalog.reply(["task-plan", "demo"], as: WizardPlan.self) {
                plan = review.rows.first(where: { $0.name == "Harbor CLI Copy" })?.cleanup
            } else {
                plan = nil
            }
            guard var plan else { return AnyView(Text("Demo cleanup unavailable")) }
            plan.held = view == "cleanup-hold"
            return AnyView(CleanupReview(plan: plan).environmentObject(store))
        case "wizard-review":
            return AnyView(CleanupWizardView(captureReview: true).environmentObject(store))
        case "wizard-start", "wizard-thaw-selection", "wizard-thaw-review", "wizard-thaw-ready", "wizard-thaw-result",
             "wizard-thaw-occupied", "wizard-thaw-remote", "wizard-thaw-corrupt", "wizard-thaw-not-frozen",
             "wizard-thaw-rehydrating":
            return AnyView(CleanupWizardView(captureView: view).environmentObject(store))
        case "task-review":
            guard let plan: WizardPlan = DemoCatalog.reply(["task-plan", "demo"], as: WizardPlan.self) else {
                return AnyView(Text("Demo task review unavailable"))
            }
            return AnyView(TaskPlanReview(plan: plan, allowApply: true, completed: {}).environmentObject(store))
        case "settings", "settings-cloud":
            return AnyView(AppSettingsView().environmentObject(store))
        case "update-markdown":
            return AnyView(UpdatesView().environmentObject(AppUpdater.demoMarkdownUpdate()))
        case "update-ready":
            return AnyView(UpdatesView().environmentObject(AppUpdater.demoReadyUpdate()))
        case let updateView where updateView.hasPrefix("update-"):
            guard let updater = AppUpdater.demoFailure(String(updateView.dropFirst("update-".count))) else {
                return AnyView(Text("Demo update state unavailable"))
            }
            return AnyView(UpdatesView().environmentObject(updater))
        default:
            return AnyView(Text("Unknown demo sheet"))
        }
    }

    static func fail(_ message: String) -> Never {
        FileHandle.standardError.write(Data(("Demo capture: " + message + "\n").utf8))
        exit(1)
    }

    static func start(store: OrganizerStore) {
        guard let options else { return }
        if let folder = options.folder {
            do {
                try createPrivateDirectory(folder)
                let started = try JSONSerialization.data(withJSONObject: ["pid": ProcessInfo.processInfo.processIdentifier])
                try writePrivate(started, to: folder.appendingPathComponent("started.json"))
            } catch { fail("Cannot create capture folder: \(error.localizedDescription)") }
        }
        if CommandLine.arguments.contains("--demo-light") { NSApp.appearance = NSAppearance(named: .aqua) }
        if CommandLine.arguments.contains("--demo-dark") { NSApp.appearance = NSAppearance(named: .darkAqua) }
        let timeout: TimeInterval = options.orcaCapture ? 900 : (options.windowServer ? 60 : 15)
        let deadline = Date().addingTimeInterval(timeout)
        if options.folder != nil {
            DispatchQueue.main.asyncAfter(deadline: .now() + timeout) { fail("Capture timed out") }
        }
        var configured = false
        var captureWindowActivated = false
        var accessibilityPostedAt: Date?
        var lastSize = NSSize.zero
        var stable = 0
        @MainActor func poll() {
            guard Date() < deadline else { fail("Window did not become ready") }
            guard let window = NSApp.windows.first(where: { $0.contentView != nil && $0.parent == nil }) else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { poll() }; return
            }
            if !configured {
                if let size = options.size { window.setContentSize(size) }
                if options.view == "hold" || options.view == "freeze-hold" {
                    store.selection = store.snapshot?.projects.first(where: { $0.hold == true })?.id
                    if options.view == "hold" { store.filter = "On hold" }
                } else if ["freeze-review", "preserve-review", "freeze-result", "freeze-failure"].contains(options.view) {
                    store.selection = store.snapshot?.projects.first(where: {
                        $0.git && $0.hold != true && ($0.blockers ?? []).isEmpty && !$0.remotes.isEmpty
                    })?.id
                } else if let code = ["freeze-no-remote": "no-remote", "freeze-no-files": "no-files", "freeze-no-commit": "no-commit",
                                      "freeze-own-source": "own-source", "freeze-process": "open-processes"][options.view] {
                    store.selection = store.snapshot?.projects.first(where: { ($0.blockers ?? []).contains(where: { $0.code == code }) })?.id
                }
                if options.view == "freeze" { store.freezeSetup = store.selected }
                else if options.view == "shelf" { store.vaultShown = true }
                else if sheetViews.contains(options.view) { store.demoCaptureSheet = DemoCaptureSheet(view: options.view) }
                configured = true
                if options.folder == nil { return }
            }
            let target = sheetViews.contains(options.view) ? window.attachedSheet : window
            guard let target, let view = target.contentView, view.bounds.width > 0, view.bounds.height > 0 else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { poll() }; return
            }
            if options.folder != nil && !captureWindowActivated {
                NSApp.activate(ignoringOtherApps: true)
                window.makeKeyAndOrderFront(nil)
                target.makeKeyAndOrderFront(nil)
                // The settings-cloud dialog is a sheet on the target and takes the key status.
                captureWindowActivated = NSApp.isActive && (target.isKeyWindow || window.isKeyWindow || target.attachedSheet?.isKeyWindow == true)
            }
            guard options.folder == nil || captureWindowActivated else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { poll() }
                return
            }
            view.layoutSubtreeIfNeeded()
            let size = view.bounds.size
            stable = size == lastSize ? stable + 1 : 0
            lastSize = size
            guard stable >= 3 else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { poll() }; return }
            if accessibilityPostedAt == nil {
                for candidate in NSApp.windows where candidate.contentView != nil {
                    NSAccessibility.post(element: candidate, notification: .layoutChanged)
                    if let content = candidate.contentView {
                        NSAccessibility.post(element: content, notification: .layoutChanged)
                    }
                    if let sheet = candidate.attachedSheet {
                        NSAccessibility.post(element: sheet, notification: .layoutChanged)
                    }
                }
                accessibilityPostedAt = Date()
            }
            guard let postedAt = accessibilityPostedAt, Date().timeIntervalSince(postedAt) >= 0.7 else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { poll() }; return
            }
            var candidates = NSApp.windows.filter { $0.contentView != nil }
            for candidate in candidates {
                if let sheet = candidate.attachedSheet, !candidates.contains(where: { $0 === sheet }) {
                    candidates.append(sheet)
                }
            }
            let trees = candidates.map { candidate -> [String: Any] in
                candidate.contentView?.layoutSubtreeIfNeeded()
                var visited: [ObjectIdentifier: NSObject] = [:]
                var node = accessibilityTree(candidate, window: candidate, visited: &visited, depth: 0)
                node["windowKind"] = candidate === window ? "window" : (candidate === window.attachedSheet ? "sheet" : "other")
                return node
            }
            let tree: [String: Any] = ["role": "AXCapture", "label": "", "enabled": NSNull(), "frame": NSNull(), "children": trees]
            if !hasExpectedControls(tree, view: options.view) && Date().timeIntervalSince(postedAt) < 3 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { poll() }; return
            }
            // Render the SwiftUI roots offscreen. This omits native window
            // chrome and may omit AppKit-hosted controls; visual review is
            // required before an image is treated as evidence.
            guard let folder = options.folder, let contentView = window.contentView else { fail("Missing capture content") }
            contentView.displayIfNeeded()
            view.displayIfNeeded()
            let actualSize = contentView.bounds.size
            let scheme: ColorScheme = CommandLine.arguments.contains("--demo-dark") ? .dark : .light
            let mainRoot = AnyView(OrganizerView().environmentObject(store).environmentObject(AppUpdater()))
            let png = renderOffscreen(mainRoot, size: actualSize, scheme: scheme)
            var sheetPNG: Data?
            if sheetViews.contains(options.view) {
                sheetPNG = renderOffscreen(sheetRoot(options.view, store: store), size: view.bounds.size, scheme: scheme)
            }
            let actual = window.contentView?.bounds.size ?? .zero
            var result: [String: Any] = ["pid": ProcessInfo.processInfo.processIdentifier, "view": options.view,
                                         "windowWidth": window.contentLayoutRect.width, "windowHeight": window.contentLayoutRect.height,
                                         "actualWidth": actual.width, "actualHeight": actual.height,
                                         "backingScale": window.backingScaleFactor, "renderer": "ImageRenderer SwiftUI roots; native window chrome excluded", "tree": tree]
            if let requested = options.size {
                result["requestedWidth"] = requested.width
                result["requestedHeight"] = requested.height
            }
            do {
                let json = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
                var windows = [windowMetadata(window, kind: "window")]
                if let sheet = window.attachedSheet { windows.append(windowMetadata(sheet, kind: "sheet")) }
                let windowJSON = try JSONSerialization.data(withJSONObject: ["pid": ProcessInfo.processInfo.processIdentifier, "windows": windows], options: [.prettyPrinted, .sortedKeys])
                let renderFolder = folder.appendingPathComponent("render", isDirectory: true)
                try createPrivateDirectory(renderFolder)
                try writePrivate(png, to: renderFolder.appendingPathComponent("window-render.png"))
                if let sheetPNG { try writePrivate(sheetPNG, to: renderFolder.appendingPathComponent("sheet-render.png")) }
                try writePrivate(json, to: folder.appendingPathComponent("accessibility.json"))
                // This file is the readiness signal for the window-server launcher.
                try writePrivate(windowJSON, to: folder.appendingPathComponent("windows.json"))
            } catch { fail("Cannot write capture: \(error.localizedDescription)") }
            // A presented sheet can defer NSApp.terminate indefinitely.
            // The optional launcher captures the live window before signaling.
            if options.windowServer {
                func waitForLauncher() {
                    if FileManager.default.fileExists(atPath: folder.appendingPathComponent("window-server.done").path) {
                        Darwin.exit(0)
                    }
                    guard Date() < deadline else { fail("Window-server capture timed out") }
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.1, execute: waitForLauncher)
                }
                waitForLauncher()
            } else {
                Darwin.exit(0)
            }
        }
        DispatchQueue.main.async { poll() }
    }

    private static func windowMetadata(_ window: NSWindow, kind: String) -> [String: Any] {
        let number = window.windowNumber
        let descriptions = CGWindowListCopyWindowInfo(.optionIncludingWindow, CGWindowID(max(number, 0))) as? [[String: Any]]
        let onscreen = descriptions?.first?[kCGWindowIsOnscreen as String] as? Bool
        let frame = window.frame
        return ["kind": kind, "windowNumber": number,
                "frame": ["x": frame.origin.x, "y": frame.origin.y, "width": frame.width, "height": frame.height],
                "isOnScreen": onscreen as Any? ?? NSNull()]
    }

    @MainActor private static func renderOffscreen(_ content: AnyView, size: NSSize, scheme: ColorScheme) -> Data {
        let root = content.environment(\.colorScheme, scheme)
            .frame(width: size.width, height: size.height)
            .background(Color(nsColor: .windowBackgroundColor))
        let renderer = ImageRenderer(content: root)
        renderer.proposedSize = ProposedViewSize(width: size.width, height: size.height)
        renderer.scale = 1
        guard let image = renderer.nsImage, let tiff = image.tiffRepresentation,
              let bitmap = NSBitmapImageRep(data: tiff),
              let data = bitmap.representation(using: .png, properties: [:]) else {
            fail("Cannot render the demo view offscreen")
        }
        return data
    }

    private static func createPrivateDirectory(_ folder: URL) throws {
        let result = folder.path.withCString { Darwin.mkdir($0, mode_t(0o700)) }
        guard result == 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
        let changed = folder.path.withCString { Darwin.chmod($0, mode_t(0o700)) }
        guard changed == 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
    }

    private static func writePrivate(_ data: Data, to url: URL) throws {
        let descriptor = url.path.withCString { Darwin.open($0, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, mode_t(0o600)) }
        guard descriptor >= 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
        let file = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        defer { try? file.close() }
        guard Darwin.fchmod(descriptor, mode_t(0o600)) == 0 else {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
        try file.write(contentsOf: data)
    }

    private static func hasExpectedControls(_ tree: [String: Any], view: String) -> Bool {
        let expectedLabels: [String]
        switch view {
        case "plan": expectedLabels = ["Preservation", "Connect remote", "Freeze", "Hold"]
        case "freeze": expectedLabels = ["Requirements", "Cancel", "Build review"]
        case "freeze-no-files": expectedLabels = ["Add files to this project", "Open in Finder", "Open Terminal"]
        case "freeze-hold": expectedLabels = ["This project is on Hold", "Requirements"]
        case "freeze-review", "preserve-review": expectedLabels = ["Expected outcome", "Git history"]
        case "freeze-result": expectedLabels = ["Outcome", "Checkout removed", "Net space change after the archive copy", "Done"]
        case "freeze-failure": expectedLabels = ["Outcome", "Done"]
        case "cleanup-review": expectedLabels = ["Review cleanup", "Expected outcome"]
        case "cleanup-hold": expectedLabels = ["Review cleanup", "This project is on Hold"]
        case "cleanup-result": expectedLabels = ["Cleanup result", "Freed from disk", "In Trash (can be restored)", "Done"]
        case "wizard-result": expectedLabels = ["Cleanup wizard", "Results", "Estimate", "Checkout removed", "In Trash (can be restored)"]
        case "wizard-review": expectedLabels = ["Expected outcome", "Review"]
        case "wizard-start": expectedLabels = ["Make space", "Thaw projects"]
        case "wizard-thaw-selection": expectedLabels = ["Frozen projects", "Create Thaw plan"]
        case "wizard-thaw-review": expectedLabels = ["Thaw plan", "Expected outcome"]
        case "wizard-thaw-ready": expectedLabels = ["Only ready projects will run", "Ice Garden", "I reviewed the remote refs"]
        case "wizard-thaw-occupied": expectedLabels = ["Thaw plan", "Path occupied", "Show path in Finder"]
        case "wizard-thaw-remote": expectedLabels = ["Thaw plan", "Remote unavailable", "Check remote again"]
        case "wizard-thaw-corrupt": expectedLabels = ["Thaw plan", "Archive needs attention", "Open Freezer"]
        case "wizard-thaw-not-frozen": expectedLabels = ["Thaw plan", "No longer Frozen", "Open Freezer"]
        case "wizard-thaw-rehydrating": expectedLabels = ["Thawing Ice Garden"]
        case "wizard-thaw-result": expectedLabels = ["Checkouts restored", "Remote refs restored", "Start a new Thaw plan", "Done"]
        case "task-review": expectedLabels = ["Review task actions", "Expected outcome"]
        case "settings": expectedLabels = ["Igloo Settings", "Freezer backup folder"]
        case "settings-cloud": expectedLabels = ["Confirm cloud backup"]
        case "repositories-loading": expectedLabels = ["GitHub repositories", "Loading repositories for example"]
        case "shelf": expectedLabels = ["Done", "Show archive", "Thaw from remote"]
        case "update-markdown": expectedLabels = []
        case "update-ready": expectedLabels = ["Install and relaunch"]
        default: return true
        }
        let expectedIdentifiers: [String]
        switch view {
        case "plan":
            expectedIdentifiers = ["detail.connectRemote", "detail.commitSelectedFiles", "detail.preserve",
                                  "detail.freeze", "detail.hold", "detail.preservationNotes",
                                  "detail.exportProjectReport", "detail.savePreservation"]
        case "freeze": expectedIdentifiers = ["freeze.cancel", "freeze.buildReview"]
        case "shelf":
            expectedIdentifiers = ["shelf.backUpNow", "shelf.verifyBackup", "shelf.done", "shelf.showArchive",
                                   "shelf.freeze", "shelf.offlineRecovery", "shelf.thawRemote"]
        case "wizard-start": expectedIdentifiers = ["wizard.start.makeSpace", "wizard.start.thaw"]
        case "wizard-thaw-selection": expectedIdentifiers = ["wizard.thaw.back", "wizard.thaw.prepare", "wizard.thaw.archive.demo-icegarden"]
        case "wizard-thaw-review":
            expectedIdentifiers = ["wizard.thaw.acceptance", "wizard.thaw.execute", "wizard.thaw.row.demo-icegarden"]
        case "wizard-thaw-ready":
            expectedIdentifiers = ["wizard.thaw.acceptance", "wizard.thaw.execute", "wizard.thaw.row.demo-icegarden"]
        case "wizard-thaw-occupied": expectedIdentifiers = ["wizard.thaw.showPath"]
        case "wizard-thaw-remote": expectedIdentifiers = ["wizard.thaw.checkRemote"]
        case "wizard-thaw-corrupt", "wizard-thaw-not-frozen": expectedIdentifiers = ["wizard.thaw.openFreezer"]
        case "wizard-thaw-result": expectedIdentifiers = ["wizard.thaw.newPlan"]
        case "update-markdown": expectedIdentifiers = ["updates.releaseNotes"]
        case "update-ready": expectedIdentifiers = ["updates.install", "updates.releaseNotes"]
        default: expectedIdentifiers = []
        }
        func labels(_ node: [String: Any]) -> [String] {
            let own = ["title", "label", "value"].compactMap { node[$0] as? String }
            return own + ((node["children"] as? [[String: Any]]) ?? []).flatMap { labels($0) }
        }
        func identifiers(_ node: [String: Any]) -> [String] {
            let own = [node["identifier"] as? String].compactMap { $0 }
            return own + ((node["children"] as? [[String: Any]]) ?? []).flatMap { identifiers($0) }
        }
        let values = labels(tree).map { $0.lowercased() }
        let hasLabels = expectedLabels.allSatisfy { name in values.contains { $0.contains(name.lowercased()) } }
        let ids = identifiers(tree)
        return hasLabels && expectedIdentifiers.allSatisfy(ids.contains)
    }

    private static func accessibilityTree(_ object: NSObject, window: NSWindow,
                                          visited: inout [ObjectIdentifier: NSObject], depth: Int) -> [String: Any] {
        let identity = ObjectIdentifier(object)
        guard depth < 100, visited[identity] == nil else { return [:] }
        visited[identity] = object
        func attribute(_ name: String) -> AnyObject? {
            let selector = NSSelectorFromString("accessibilityAttributeValue:")
            if object.responds(to: selector), let value = object.perform(selector, with: name)?.takeUnretainedValue() { return value }
            let names = ["AXRole": "accessibilityRole", "AXTitle": "accessibilityTitle", "AXDescription": "accessibilityLabel",
                         "AXValue": "accessibilityValue", "AXHelp": "accessibilityHelp",
                         "AXRoleDescription": "accessibilityRoleDescription", "AXIdentifier": "accessibilityIdentifier",
                         "AXChildren": "accessibilityChildren"]
            guard let method = names[name] else { return nil }
            let fallback = NSSelectorFromString(method)
            guard object.responds(to: fallback) else { return nil }
            return object.perform(fallback)?.takeUnretainedValue()
        }
        func enabled() -> Bool? {
            if let number = attribute("AXEnabled") as? NSNumber { return number.boolValue }
            if let control = object as? NSControl { return control.isEnabled }
            let selector = NSSelectorFromString("isAccessibilityEnabled")
            guard object.responds(to: selector) else { return nil }
            typealias Method = @convention(c) (AnyObject, Selector) -> Bool
            return unsafeBitCast(object.method(for: selector), to: Method.self)(object, selector)
        }
        func screenFrame() -> NSRect? {
            if let position = attribute("AXPosition") as? NSValue,
               let size = attribute("AXSize") as? NSValue {
                return NSRect(origin: position.pointValue, size: size.sizeValue)
            }
            if let view = object as? NSView {
                let viewFrame = view.convert(view.bounds, to: nil)
                return view.window?.convertToScreen(viewFrame) ?? viewFrame
            }
            if let candidate = object as? NSWindow { return candidate.frame }
            return nil
        }

        let nativeTitle = (object as? NSButton)?.title ?? ""
        let attributedTitle = (object as? NSButton)?.attributedTitle.string ?? ""
        let axTitle = attribute("AXTitle") as? String ?? ""
        let title = [axTitle, nativeTitle, attributedTitle].first(where: { !$0.isEmpty }) ?? ""
        let axLabel = attribute("AXDescription") as? String ?? ""
        var frame: Any = NSNull()
        if let screen = screenFrame(), screen.origin.x.isFinite, screen.origin.y.isFinite,
           screen.width.isFinite, screen.height.isFinite {
            let local = window.convertFromScreen(screen)
            frame = ["x": local.origin.x, "y": local.origin.y, "width": local.width, "height": local.height]
        }

        var result: [String: Any] = ["role": attribute("AXRole") as? String ?? "",
                                     "title": title, "label": axLabel.isEmpty ? title : axLabel,
                                     "enabled": enabled() as Any? ?? NSNull(), "frame": frame,
                                     "class": NSStringFromClass(type(of: object))]
        for (key, name) in [("value", "AXValue"), ("help", "AXHelp"),
                            ("roleDescription", "AXRoleDescription"), ("identifier", "AXIdentifier")] {
            if let value = attribute(name) as? String, !value.isEmpty { result[key] = value }
            else if key == "value", let value = attribute(name) as? NSNumber { result[key] = value }
        }
        if !nativeTitle.isEmpty { result["nativeTitle"] = nativeTitle }
        if !attributedTitle.isEmpty { result["attributedTitle"] = attributedTitle }
        if let value = (object as? NSControl)?.stringValue, !value.isEmpty { result["stringValue"] = value }

        var children = (attribute("AXChildren") as? [NSObject]) ?? []
        if let view = object as? NSView { children.append(contentsOf: view.subviews) }
        if let candidate = object as? NSWindow, let content = candidate.contentView { children.append(content) }
        result["children"] = children.map {
            accessibilityTree($0, window: window, visited: &visited, depth: depth + 1)
        }.filter { !$0.isEmpty }
        return result
    }
}
