import Foundation

enum DemoCatalog {
    private static func resource(_ name: String) -> Data {
        try! Data(contentsOf: Bundle.main.url(forResource: name, withExtension: "json")!)
    }
    static func snapshot() -> Snapshot {
        try! JSONDecoder().decode(Snapshot.self, from: resource("demo-catalog"))
    }
    static func reply<T: Decodable>(_ args: [String], as type: T.Type) -> T? {
        var data: Data
        switch args.first {
        case "snapshot": data = resource("demo-catalog")
        case "task-list": data = resource("demo-tasks")
        case "wizard-state":
            let object = try! JSONSerialization.jsonObject(with: resource("demo-tasks")) as! [String: Any]
            let review = object["demoWizardReview"] as! [String: Any]
            data = try! JSONSerialization.data(withJSONObject: ["rules": review["rules"]!,
                "cacheKinds": ["node_modules", ".cache"], "last": review])
        case "task-plan":
            let object = try! JSONSerialization.jsonObject(with: resource("demo-tasks")) as! [String: Any]
            data = try! JSONSerialization.data(withJSONObject: object["demoTaskReview"]!)
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
        default: return nil
        }
        return try? JSONDecoder().decode(type, from: data)
    }
}

// Demo-only capture uses the existing smoke path; it never reads the real catalog.
import AppKit
import SwiftUI
import ApplicationServices
import CoreGraphics
import Darwin

struct DemoCaptureSheet: Identifiable {
    var view: String
    var id: String { view }
}

enum DemoCapture {
    static let extraSheetViews: Set<String> = ["freeze-review", "preserve-review", "freeze-result", "freeze-failure", "freeze-hold",
        "freeze-no-remote", "freeze-no-files", "freeze-no-commit", "freeze-own-source", "freeze-process",
        "cleanup-review", "cleanup-hold", "wizard-review", "task-review", "settings", "settings-cloud"]
    static let updateViews = AppUpdater.demoFailureNames.map { "update-" + $0 }
    static let sheetViews = extraSheetViews.union(["freeze", "shelf"]).union(updateViews)
    struct Options {
        let folder: URL?
        let size: NSSize?
        let view: String
        let scope: String
        let rail: String
        let data: String
        let windowServer: Bool
    }

    static let options: Options? = {
        let args = CommandLine.arguments
        guard args.contains("--demo") else { return nil }
        let flags = ["--demo-capture", "--demo-size", "--demo-view", "--demo-scope", "--demo-rail", "--demo-data", "--demo-window-server"]
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
        let updateViews = ["quota", "quota-no-reset", "forbidden", "offline", "timeout", "server", "malformed", "cancelled-download", "digest-mismatch"].map { "update-" + $0 }
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
        if windowServer && folder == nil { fail("--demo-window-server requires --demo-capture") }
        return Options(folder: folder, size: size, view: view, scope: scope, rail: rail, data: data, windowServer: windowServer)
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
        case "task-review":
            guard let plan: WizardPlan = DemoCatalog.reply(["task-plan", "demo"], as: WizardPlan.self) else {
                return AnyView(Text("Demo task review unavailable"))
            }
            return AnyView(TaskPlanReview(plan: plan, allowApply: true, completed: {}).environmentObject(store))
        case "settings", "settings-cloud":
            return AnyView(AppSettingsView().environmentObject(store))
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
        let timeout: TimeInterval = options.windowServer ? 60 : 15
        let deadline = Date().addingTimeInterval(timeout)
        if options.folder != nil {
            DispatchQueue.main.asyncAfter(deadline: .now() + timeout) { fail("Capture timed out") }
        }
        let accessibilityDeadline = Date().addingTimeInterval(3)
        var configured = false
        var postedAccessibility = false
        var lastSize = NSSize.zero
        var stable = 0
        func poll() {
            guard Date() < deadline else { fail("Window did not become ready") }
            guard let window = NSApp.windows.first(where: { $0.contentView != nil && $0.parent == nil }) else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1, execute: poll); return
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
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1, execute: poll); return
            }
            view.layoutSubtreeIfNeeded()
            let size = view.bounds.size
            stable = size == lastSize ? stable + 1 : 0
            lastSize = size
            guard stable >= 3 else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.1, execute: poll); return }
            if !postedAccessibility {
                NSAccessibility.post(element: target, notification: .layoutChanged)
                postedAccessibility = true
            }
            // Keep every visited element alive for the duration of a traversal;
            // otherwise short-lived bridge objects may reuse an identity.
            var visited: [ObjectIdentifier: NSObject] = [:]
            let appKitTree = accessibilityTree(target, visited: &visited, depth: 0)
            // Query the system AX server off the main thread so it can ask
            // this process for SwiftUI accessibility elements.
            DispatchQueue.global(qos: .userInitiated).async {
                let systemTree = systemAccessibilityTree()
                DispatchQueue.main.async {
            let tree: [String: Any] = ["role": "AXCapture", "children": [appKitTree, systemTree].filter { !$0.isEmpty }]
            if !hasExpectedControls(tree, view: options.view) && Date() < accessibilityDeadline {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1, execute: poll); return
            }
            // Render the SwiftUI roots offscreen. This omits native window
            // chrome and may omit AppKit-hosted controls; visual review is
            // required before an image is treated as evidence.
            guard let folder = options.folder, let contentView = window.contentView else { fail("Missing capture content") }
            contentView.displayIfNeeded()
            view.displayIfNeeded()
            let actualSize = contentView.bounds.size
            let scheme: ColorScheme = CommandLine.arguments.contains("--demo-dark") ? .dark : .light
            let mainRoot = AnyView(OrganizerView().environmentObject(store))
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
                try writePrivate(png, to: folder.appendingPathComponent("window.png"))
                if let sheetPNG { try writePrivate(sheetPNG, to: folder.appendingPathComponent("sheet.png")) }
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
            }
        }
        DispatchQueue.main.async(execute: poll)
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

    // Query our own process through the system AX interface without prompting
    // for access. Some SwiftUI elements are exposed through this interface
    // but not through an in-process NSView accessibility walk.
    private static func systemAccessibilityTree() -> [String: Any] {
        let app = AXUIElementCreateApplication(getpid())
        AXUIElementSetMessagingTimeout(app, 2)
        var visited: [AXUIElement] = []
        func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
            var value: CFTypeRef?
            guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
            return value
        }
        func walk(_ element: AXUIElement, _ depth: Int) -> [String: Any] {
            guard depth < 50, !visited.contains(where: { CFEqual($0, element) }) else { return [:] }
            visited.append(element)
            var node: [String: Any] = [:]
            for (key, name) in [("role", "AXRole"), ("title", "AXTitle"), ("label", "AXDescription"), ("value", "AXValue")] {
                if let value = attribute(element, name) as? String, !value.isEmpty { node[key] = value }
            }
            if let value = attribute(element, "AXPosition"), CFGetTypeID(value) == AXValueGetTypeID() {
                var point = CGPoint.zero
                if AXValueGetValue(value as! AXValue, .cgPoint, &point) { node["position"] = [point.x, point.y] }
            }
            if let value = attribute(element, "AXSize"), CFGetTypeID(value) == AXValueGetTypeID() {
                var size = CGSize.zero
                if AXValueGetValue(value as! AXValue, .cgSize, &size) { node["size"] = [size.width, size.height] }
            }
            var elements = (attribute(element, "AXChildren") as? [AXUIElement]) ?? []
            if depth == 0 { elements += (attribute(element, "AXWindows") as? [AXUIElement]) ?? [] }
            node["children"] = elements.map { walk($0, depth + 1) }.filter { !$0.isEmpty }
            return node
        }
        return walk(app, 0)
    }

    private static func hasExpectedControls(_ tree: [String: Any], view: String) -> Bool {
        let expected: [String]
        switch view {
        case "plan": expected = ["Preservation", "Connect remote", "Freeze", "Hold"]
        case "freeze": expected = ["Requirements", "Cancel", "Build review"]
        case "freeze-no-files": expected = ["Add files to this project", "Open in Finder", "Open Terminal"]
        case "freeze-hold": expected = ["This project is on Hold", "Requirements"]
        case "freeze-review", "preserve-review": expected = ["Expected outcome", "Git history"]
        case "freeze-result", "freeze-failure": expected = ["Outcome", "Done"]
        case "cleanup-review": expected = ["Review cleanup", "Expected outcome"]
        case "cleanup-hold": expected = ["Review cleanup", "This project is on Hold"]
        case "wizard-review": expected = ["Expected outcome", "Review"]
        case "task-review": expected = ["Review task actions", "Expected outcome"]
        case "settings": expected = ["Igloo Settings", "Freezer backup folder"]
        case "settings-cloud": expected = ["Confirm cloud backup"]
        case "shelf": expected = ["Done", "Show archive", "Thaw from remote"]
        default: return true
        }
        func labels(_ node: [String: Any]) -> [String] {
            let own = ["title", "label", "value"].compactMap { node[$0] as? String }
            return own + ((node["children"] as? [[String: Any]]) ?? []).flatMap { labels($0) }
        }
        let values = labels(tree).map { $0.lowercased() }
        return expected.allSatisfy { name in values.contains { $0.contains(name.lowercased()) } }
    }

    private static func accessibilityTree(_ object: NSObject, visited: inout [ObjectIdentifier: NSObject], depth: Int) -> [String: Any] {
        let identity = ObjectIdentifier(object)
        guard depth < 50, visited[identity] == nil else { return [:] }
        visited[identity] = object
        func attribute(_ name: String) -> AnyObject? {
            let selector = NSSelectorFromString("accessibilityAttributeValue:")
            if object.responds(to: selector), let value = object.perform(selector, with: name)?.takeUnretainedValue() { return value }
            let names = ["AXRole": "accessibilityRole", "AXTitle": "accessibilityTitle", "AXDescription": "accessibilityLabel",
                         "AXValue": "accessibilityValue", "AXChildren": "accessibilityChildren"]
            guard let method = names[name] else { return nil }
            let fallback = NSSelectorFromString(method)
            guard object.responds(to: fallback) else { return nil }
            return object.perform(fallback)?.takeUnretainedValue()
        }
        var result: [String: Any] = [:]
        for (key, attributeName) in [("role", "AXRole"), ("title", "AXTitle"), ("label", "AXDescription"), ("value", "AXValue")] {
            if let value = attribute(attributeName) as? String, !value.isEmpty { result[key] = value }
        }
        if let value = attribute("AXPosition") as? NSValue {
            let point = value.pointValue
            result["position"] = [point.x, point.y]
        }
        if let value = attribute("AXSize") as? NSValue {
            let size = value.sizeValue
            result["size"] = [size.width, size.height]
        }
        if let children = attribute("AXChildren") as? [NSObject] {
            result["children"] = children.map { accessibilityTree($0, visited: &visited, depth: depth + 1) }.filter { !$0.isEmpty }
        }
        return result
    }
}
