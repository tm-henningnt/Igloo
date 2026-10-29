import SwiftUI
import AppKit
import Combine
import UserNotifications

// macOS 27 also exports a State macro; use the established property wrapper without its macro plugin.
typealias LocalState<Value> = SwiftUI.State<Value>

struct Branch: Decodable, Identifiable {
    var name: String; var sha: String; var upstream: String; var tracking: String
    var date: String; var uncovered: Int?; var privateHistory: Bool
    var id: String { name }
    var coverage: String {
        guard let n = uncovered else { return "Unverified" }
        return n == 0 ? "Covered remotely" : "\(n) commits to review"
    }
}
struct DetachedHead: Decodable, Identifiable {
    var path: String; var sha: String; var uncovered: Int?
    var id: String { path }
}
struct Remote: Decodable, Hashable { var name: String; var url: String; var pushURL: String }
struct Worktree: Decodable, Identifiable {
    var path: String; var branch: String; var exists: Bool; var changes: Int; var locked: Bool; var prunable: String
    var id: String { path }
}
struct ChangedFile: Decodable, Identifiable {
    var checkout: String; var status: String; var path: String
    var id: String { checkout + "/" + path }
}
struct LocalFile: Decodable, Identifiable {
    var path: String; var size: Int64; var template: Bool; var tracked: Bool; var ignored: Bool
    var id: String { path }
}
struct IgnoredFile: Decodable, Identifiable {
    var checkout: String; var path: String
    var id: String { checkout + "/" + path }
}
struct Ref: Decodable, Identifiable {
    var name: String; var sha: String; var verified: Bool
    var id: String { name }
}
struct Candidate: Decodable, Identifiable {
    var id: String; var path: String; var size: Int64; var kind: String
    var owner: String; var eligible: Bool; var reason: String
}
struct FreezeBlocker: Decodable, Identifiable {
    var code: String; var message: String; var fix: String?
    var id: String { code }
    var shortLabel: String {
        switch code {
        case "no-remote": return "connect a remote"
        case "no-files": return "add files"
        case "no-commit": return "make an initial commit"
        case "changed-source": return "commit changes"
        case "open-processes": return "close open processes"
        case "own-source": return "Igloo source is protected"
        default: return "review requirements"
        }
    }
}

extension Project {
    // The Overview heading reads the same blockers as the list badge, so the two never disagree.
    var overviewSummary: (title: String, detail: String) {
        let open = blockers ?? []
        if active { return ("Project in use", "Close project shells, agents and applications before cleanup or freezing.") }
        if !activityKnown { return ("Activity needs verification", "Refresh the project to check running sessions before making changes.") }
        if let first = open.first {
            let more = open.count > 1 ? " \(open.count - 1) more on the Preservation tab." : ""
            return ("Blocked: " + first.shortLabel, first.message + more)
        }
        if needsPreservation { return ("Local work to preserve", "Review the items below before removing a checkout.") }
        return ("Ready for a closer review", "The catalog found no immediate preservation flags. Review ignored files before removing anything.")
    }
}

struct Project: Decodable, Identifiable {
    var id: String; var name: String; var path: String; var common: String; var category: String
    var projectType: String?; var detectedType: String?; var languages: [String]?; var typeEvidence: [String]?
    var kind: String { projectType ?? "Unclassified" }
    var technologyText: String { (languages ?? []).joined(separator: ", ") }
    var hold: Bool?; var blockers: [FreezeBlocker]?
    var size: Int64; var git: Bool; var ageDays: Int?; var fileAgeDays: Int?; var lastCommit: String; var remoteStatus: String
    var verifiedAt: String; var remotes: [Remote]; var active: Bool; var activityKnown: Bool; var processes: [String]
    var branches: [Branch]; var worktrees: [Worktree]; var changes: [ChangedFile]; var stashes: [String]
    var refs: [Ref]; var detachedHeads: [DetachedHead]?; var localFiles: [LocalFile]; var ignoredFiles: [IgnoredFile]; var candidates: [Candidate]
    var holds: [String]; var uncovered: Int; var unknown: Int; var note: String
    var cleanupSize: Int64 { candidates.filter(\.eligible).reduce(0) { $0 + $1.size } }
    var needsPreservation: Bool {
        !git || uncovered > 0 || unknown > 0 || !changes.isEmpty || !stashes.isEmpty
        || localFiles.contains(where: { !$0.template }) || refs.contains(where: { !$0.verified })
        || (detachedHeads ?? []).contains(where: { $0.uncovered == nil || ($0.uncovered ?? 0) > 0 })
    }
}
struct TrashItem: Decodable, Identifiable {
    var id: String; var original: String; var destination: String; var owner: String
    var size: Int64; var at: String; var status: String
}
struct Snapshot: Decodable {
    var version: Int; var generated: String; var root: String; var source: String
    var totalSize: Int64; var freeSpace: Int64; var projects: [Project]; var errors: [String]; var history: [TrashItem]; var archives: [FrozenArchive]?
}
struct PlanItem: Decodable, Identifiable {
    var path: String; var owner: String; var size: Int64
    var id: String { path }
}
struct BlockedItem: Decodable, Identifiable {
    var path: String; var reason: String
    var id: String { path }
}
struct CleanupPlan: Decodable, Identifiable {
    var held: Bool?
    var id: String; var created: String; var items: [PlanItem]; var blocked: [BlockedItem]; var totalSize: Int64
    var outcome: ReviewOutcome?
}
struct Reply: Decodable {
    var message: String?; var error: String?; var results: [ActionResult]?; var outcome: ReviewOutcome?
}
struct ReminderCheck: Decodable {
    var checked: String
    var count: Int
    var size: Int64
    var ids: [String]
    var message: String
}

enum ReminderRoute {
    static func isReminderLaunch(arguments: [String]) -> Bool {
        arguments.contains("--remind")
    }

    static func isReminderURL(_ url: URL) -> Bool {
        url.scheme == "igloo" && url.host == "remind"
    }

    static func shouldFinishAfterURL(arguments: [String], secondsSinceLaunch: TimeInterval) -> Bool {
        isReminderLaunch(arguments: arguments) || secondsSinceLaunch < 3
    }

    static func shouldSyncAgent(enabled: Bool, agentExists: Bool) -> Bool {
        enabled || agentExists
    }

    static func agentExists(bundleIdentifier: String?, fileManager: FileManager = .default) -> Bool {
        guard let bundleIdentifier else { return false }
        let path = NSHomeDirectory() + "/Library/LaunchAgents/\(bundleIdentifier).remind.plist"
        return fileManager.fileExists(atPath: path)
    }

    struct RunningCopy: Equatable {
        var processIdentifier: Int32
        var bundleIdentifier: String?
        var bundlePath: String?
    }

    /// The running copy of this app that takes the reminder, or nil when this process posts it.
    /// Only the same bundle ID qualifies; a copy at the same bundle path comes first.
    static func handoffTarget(among copies: [RunningCopy], ownProcess: Int32,
                              bundleIdentifier: String?, bundlePath: String?) -> RunningCopy? {
        guard let bundleIdentifier else { return nil }
        let others = copies.filter {
            $0.processIdentifier != ownProcess && $0.bundleIdentifier == bundleIdentifier && $0.bundlePath != nil
        }
        let ownPath = bundlePath.map { URL(fileURLWithPath: $0).standardizedFileURL.path }
        return others.first { URL(fileURLWithPath: $0.bundlePath!).standardizedFileURL.path == ownPath }
            ?? others.first
    }
}

enum ReminderHandoff {
    /// The LaunchAgent starts the app executable directly with --remind. When another copy of
    /// this bundle runs, it gets igloo://remind and this process exits before NSApplication starts,
    /// so it never registers as a second app and never opens a window.
    static func toRunningCopy() -> Bool {
        let own = NSRunningApplication.current
        let copies = NSWorkspace.shared.runningApplications.filter { !$0.isTerminated }.map {
            ReminderRoute.RunningCopy(processIdentifier: $0.processIdentifier,
                                      bundleIdentifier: $0.bundleIdentifier, bundlePath: $0.bundleURL?.path)
        }
        guard let target = ReminderRoute.handoffTarget(among: copies, ownProcess: own.processIdentifier,
                                                       bundleIdentifier: Bundle.main.bundleIdentifier,
                                                       bundlePath: Bundle.main.bundlePath),
              let path = target.bundlePath,
              let url = URL(string: "igloo://remind") else { return false }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = false
        configuration.addsToRecentItems = false
        let done = DispatchSemaphore(value: 0)
        var delivered = false
        NSWorkspace.shared.open([url], withApplicationAt: URL(fileURLWithPath: path),
                                configuration: configuration) { application, error in
            delivered = error == nil && application != nil
            done.signal()
        }
        // A failed or slow hand-off falls back to a local check, so the reminder is not lost.
        return done.wait(timeout: .now() + 10) == .success && delivered
    }
}

enum ReminderBackend {
    static func runCheck(completion: @escaping (ReminderCheck?) -> Void) {
        let preferences = UserPreferences.read()
        guard let resources = Bundle.main.resourceURL else { completion(nil); return }
        let script = resources.appendingPathComponent("backend").appendingPathComponent("organizer.py")
        let python = UserPreferences.bundledPython
            ?? (preferences.pythonExecutable as NSString).expandingTildeInPath
        let root = URL(fileURLWithPath: (preferences.projectsRoot as NSString).expandingTildeInPath)

        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: python)
            process.arguments = ["-E", "-s", "-B", script.path, "remind-check"]
            process.currentDirectoryURL = root
            process.environment = UserPreferences.backendEnvironment
            let output = Pipe()
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            do {
                try process.run()
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                let result = process.terminationStatus == 0
                    ? try? JSONDecoder().decode(ReminderCheck.self, from: data) : nil
                DispatchQueue.main.async { completion(result) }
            } catch {
                DispatchQueue.main.async { completion(nil) }
            }
        }
    }
}

@MainActor final class ReminderRouter: ObservableObject {
    static let shared = ReminderRouter()
    @Published var makeSpaceRequested = false
}

final class ReminderAppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    private let processStartUptime = ProcessInfo.processInfo.systemUptime
    private var reminderCheckRunning = false
    private var pendingReminderChecks = 0
    private var terminateAfterReminder = false
    private var reminderLaunchHandled = false

    func applicationWillFinishLaunching(_ notification: Notification) {
        // Igloo has one catalog window; macOS window tabs only add an empty tab bar and View menu items.
        NSWindow.allowsAutomaticWindowTabbing = false
        if ReminderRoute.isReminderLaunch(arguments: CommandLine.arguments) {
            NSApp.setActivationPolicy(.accessory)
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self
        if ReminderRoute.isReminderLaunch(arguments: CommandLine.arguments), !reminderLaunchHandled {
            reminderLaunchHandled = true
            startReminderCheck(terminateAfter: true)
        }
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        guard !CommandLine.arguments.contains("--demo"),
              urls.contains(where: ReminderRoute.isReminderURL) else { return }
        if ReminderRoute.isReminderLaunch(arguments: CommandLine.arguments) {
            guard !reminderLaunchHandled else { return }
            reminderLaunchHandled = true
            startReminderCheck(terminateAfter: true)
            return
        }
        let elapsed = ProcessInfo.processInfo.systemUptime - processStartUptime
        let finishWhenDone = ReminderRoute.shouldFinishAfterURL(arguments: CommandLine.arguments,
                                                                secondsSinceLaunch: elapsed)
        if finishWhenDone {
            application.setActivationPolicy(.accessory)
            application.windows.filter(\.isVisible).forEach { $0.close() }
        }
        startReminderCheck(terminateAfter: finishWhenDone)
    }

    private func startReminderCheck(terminateAfter: Bool) {
        terminateAfterReminder = terminateAfterReminder || terminateAfter
        pendingReminderChecks += 1
        guard !reminderCheckRunning else { return }
        runNextReminderCheck()
    }

    private func runNextReminderCheck() {
        guard pendingReminderChecks > 0 else {
            reminderCheckRunning = false
            if terminateAfterReminder { NSApp.terminate(nil) }
            return
        }
        pendingReminderChecks -= 1
        reminderCheckRunning = true
        ReminderBackend.runCheck { [weak self] result in
            guard let self else { return }
            guard let result, result.count > 0 else {
                self.reminderCheckRunning = false
                self.runNextReminderCheck()
                return
            }
            let content = UNMutableNotificationContent()
            content.title = "Quiet projects are ready for review"
            content.body = result.message
            content.sound = .default
            content.userInfo = ["route": "make-space", "projectIDs": result.ids]
            let request = UNNotificationRequest(identifier: "quiet-reminder-\(UUID().uuidString)",
                                                content: content, trigger: nil)
            UNUserNotificationCenter.current().add(request) { _ in
                DispatchQueue.main.async {
                    self.reminderCheckRunning = false
                    self.runNextReminderCheck()
                }
            }
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        if response.notification.request.content.userInfo["route"] as? String == "make-space" {
            DispatchQueue.main.async { ReminderRouter.shared.makeSpaceRequested = true }
        }
        completionHandler()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }
}
struct ActionResult: Decodable {
    var path: String
    var ok: Bool
    var message: String
    var size: Int64?

    init(path: String, ok: Bool, message: String, size: Int64? = nil) {
        self.path = path
        self.ok = ok
        self.message = message
        self.size = size
    }

    private enum CodingKeys: String, CodingKey { case path, ok, message, size }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        path = try values.decode(String.self, forKey: .path)
        ok = try values.decode(Bool.self, forKey: .ok)
        message = try values.decode(String.self, forKey: .message)
        size = try values.decodeIfPresent(Int64.self, forKey: .size)
    }
}
struct CleanupResult: Identifiable {
    var id = UUID()
    var message: String
    var results: [ActionResult]
    var outcome: ReviewOutcome
}
struct PushRequest: Identifiable {
    var project: Project; var branch: Branch; var remote: Remote
    var id: String { project.id + branch.name }
}
func bytes(_ value: Int64) -> String {
    if value == 0 { return "0 bytes" }
    if value < 1_048_576 { return ByteCountFormatter.string(fromByteCount: value, countStyle: .binary) }
    let units = ["MB", "GB", "TB", "PB"]
    var size = Double(value) / 1_048_576
    var unit = 0
    while size >= 1024 && unit < units.count - 1 { size /= 1024; unit += 1 }
    return size.formatted(.number.precision(.fractionLength(1))) + " " + units[unit]
}

func sheetWidth(_ preferred: CGFloat) -> CGFloat {
    let hostWidth = DemoCapture.options?.size?.width ?? NSApp.mainWindow?.contentLayoutRect.width ?? preferred
    return min(preferred, max(620, hostWidth - 40))
}
func compactTableHeight(_ rows: Int, rowHeight: CGFloat = 30, headerHeight: CGFloat = 26,
                        maxRows: Int = 8, maxHeight: CGFloat = 270) -> CGFloat {
    min(maxHeight, headerHeight + CGFloat(max(1, min(rows, maxRows))) * rowHeight)
}
func noun(_ count: Int, _ one: String, _ many: String? = nil) -> String { "\(count) " + (count == 1 ? one : many ?? one + "s") }
func shortDate(_ value: String) -> String { value.isEmpty ? "Unknown" : String(value.prefix(10)) }
func relative(_ path: String, to root: String) -> String {
    path.hasPrefix(root + "/") ? String(path.dropFirst(root.count + 1)) : path
}

final class OrganizerStore: ObservableObject {
    @Published var preferences = UserPreferences.read()
    @Published var snapshot: Snapshot?
    @Published var busy = false
    @Published var status = "Opening catalog…"
    @Published var selection: String?
    @Published var filter = "All projects"
    @Published var search = ""
    @Published var typeFilter = "All types"
    @Published var order = "Largest first"
    @Published var quietDays = 60
    @Published var error: String?
    @Published var plan: CleanupPlan?
    @Published var cleanupResult: CleanupResult?
    @Published var historyShown = false
    @Published var pushRequest: PushRequest?
    @Published var remoteSetup: Project?
    @Published var commitSetup: Project?
    @Published var freezeSetup: Project?
    @Published var freezeMode = "freeze"
    @Published var demoCaptureSheet: DemoCaptureSheet?
    @Published var vaultShown = false
    @Published var helpShown = false
    @Published var repositoriesShown = false
    @Published var spaceShown = false
    @Published var explorerProjectIDs: Set<String>?
    @Published var wizardShown = false
    @Published var wizardMakeSpace = false
    @Published var tasksShown = false
    @Published var sheetErrorHosts: [UUID] = []
    var isDemo: Bool { CommandLine.arguments.contains("--demo") }
    var progressTimer: Timer?
    var smokeStarted = false
    var reminderAgentSynced = false

    init() {
        if isDemo {
            preferences = UserPreferences.defaults()
            preferences.cloneRoot = "/Users/demo/Projects"
            preferences.githubOwner = "example"
            preferences.pythonExecutable = "/usr/local/bin/python3"
            preferences.githubExecutable = "/usr/local/bin/gh"
            preferences.projectsRoot = "/Users/demo/Projects"
            preferences.archiveRoot = "/Users/demo/Project Recovery"
            preferences.stateRoot = "/Users/demo/Library/Igloo"
            let reminder = DemoCatalog.reminderSettings()
            preferences.quietReminderEnabled = reminder.enabled
            preferences.quietReminderWeekday = reminder.weekday
            preferences.quietReminderTime = reminder.time
            return
        }
        quietDays = preferences.quietDays
        if !FileManager.default.fileExists(atPath: UserPreferences.file.path) { try? preferences.write() }
    }
    var root: URL { URL(fileURLWithPath: (preferences.projectsRoot as NSString).expandingTildeInPath) }
    var stateDirectory: URL { URL(fileURLWithPath: (preferences.stateRoot as NSString).expandingTildeInPath) }
    var source: URL { Bundle.main.resourceURL!.appendingPathComponent("backend") }
    func persistQuietDays() {
        guard !isDemo else { return }
        preferences.quietDays = quietDays
        try? preferences.write()
    }
    var selected: Project? { snapshot?.projects.first(where: { $0.id == selection }) }
    var visible: [Project] {
        let values = (snapshot?.projects ?? []).filter { p in
            let match: Bool
            switch filter {
            case "Cleanup candidates": match = p.cleanupSize > 0
            case "Needs preservation": match = p.needsPreservation
            case "Quiet projects": match = !p.active && (p.ageDays ?? -1) >= quietDays
            case "No remote": match = p.git && p.remotes.isEmpty
            case "No Git metadata": match = !p.git
            case "Active sessions": match = p.active
            case "On hold": match = p.hold == true
            default: match = true
            }
            return match && (typeFilter == "All types" || p.kind == typeFilter) && (search.isEmpty || (p.name + " " + p.path + " " + p.note + " " + p.kind + " " + p.technologyText).localizedCaseInsensitiveContains(search))
        }
        return values.sorted { a, b in
            switch order {
            case "Name": return a.name.localizedStandardCompare(b.name) == .orderedAscending
            case "Quietest first": return (a.ageDays ?? -1) > (b.ageDays ?? -1)
            case "Most cleanup": return a.cleanupSize > b.cleanupSize
            default: return a.size > b.size
            }
        }
    }
    func invoke<T: Decodable>(_ args: [String], as type: T.Type,
                              failed: ((Error) -> Void)? = nil, done: @escaping (T) -> Void) {
        if isDemo {
            if let value: T = DemoCatalog.reply(args, as: type) { done(value) }
            else {
                // The refusal waits one turn, so a sheet that acts on appear is open and can show it.
                DispatchQueue.main.async {
                    self.error = "The example catalog is read-only."
                    failed?(NSError(domain: "Organizer", code: 1, userInfo: [NSLocalizedDescriptionKey: "The example catalog is read-only."]))
                }
            }
            return
        }
        guard !busy else { return }
        busy = true
        status = args.first == "scan" ? "Discovering projects, measuring folders and checking remotes…"
            : (args.first == "remind-agent" ? "Updating the reminder schedule…" : "Checking local state…")
        progressTimer?.invalidate()
        if ["scan", "freeze-preview", "freeze-execute", "freeze-retire", "rehydrate", "wizard-scan", "wizard-prepare", "wizard-execute", "wizard-thaw-prepare", "wizard-thaw-execute", "task-run", "task-apply"].contains(args.first ?? "") {
            progressTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
                guard let self = self,
                      let data = try? Data(contentsOf: self.stateDirectory.appendingPathComponent("progress.json")),
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: String],
                      let message = obj["message"] else { return }
                self.status = message
            }
        }
        let script = source.appendingPathComponent("organizer.py")
        let python = UserPreferences.bundledPython ?? (preferences.pythonExecutable as NSString).expandingTildeInPath
        let cwd = root
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: python)
            process.arguments = ["-E", "-s", "-B", script.path] + args
            process.currentDirectoryURL = cwd
            process.environment = UserPreferences.backendEnvironment
            let output = Pipe()
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            do {
                try process.run()
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                let envelope = try? JSONDecoder().decode(Reply.self, from: data)
                if let failure = envelope?.error { throw NSError(domain: "Organizer", code: 1, userInfo: [NSLocalizedDescriptionKey: failure]) }
                if process.terminationStatus != 0 {
                    throw NSError(domain: "Organizer", code: 1, userInfo: [NSLocalizedDescriptionKey: "The backend could not finish. Check the Python installation and folder permissions."])
                }
                let decoded = try JSONDecoder().decode(T.self, from: data)
                DispatchQueue.main.async {
                    self.busy = false
                    self.progressTimer?.invalidate()
                    self.status = "Ready"
                    done(decoded)
                }
            } catch {
                DispatchQueue.main.async {
                    self.busy = false
                    self.progressTimer?.invalidate()
                    self.status = "Action stopped"
                    self.error = error.localizedDescription
                    failed?(error)
                }
            }
        }
    }
    func syncReminderAgent(done: @escaping (String?) -> Void = { _ in }) {
        guard !isDemo,
              ReminderRoute.shouldSyncAgent(
                enabled: preferences.quietReminderEnabled,
                agentExists: ReminderRoute.agentExists(bundleIdentifier: Bundle.main.bundleIdentifier)) else {
            done(nil)
            return
        }
        let value: [String: Any] = [
            "enabled": preferences.quietReminderEnabled,
            "weekday": preferences.quietReminderWeekday,
            "time": preferences.quietReminderTime,
            "appPath": Bundle.main.bundleURL.path,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: value),
              let json = String(data: data, encoding: .utf8) else {
            done("The reminder settings could not be encoded.")
            return
        }
        invoke(["remind-agent", json], as: Reply.self,
               failed: { done($0.localizedDescription) }) { _ in done(nil) }
    }
    func openMakeSpaceWizard() {
        wizardMakeSpace = true
        wizardShown = true
    }
    func load() {
        if isDemo {
            snapshot = DemoCatalog.snapshot()
            if let options = DemoCapture.options {
                if options.data == "empty" { snapshot?.projects = [] }
                if options.data == "long", !(snapshot?.projects.isEmpty ?? true) {
                    snapshot?.projects[0].name = "A very long fictional project name for checking truncation and wrapping in narrow windows"
                }
            }
            if selection == nil { selection = snapshot?.projects.first?.id }
            status = "Demo catalog · fictional example projects"
            smokeIfRequested()
            return
        }
        guard FileManager.default.fileExists(atPath: root.path) else {
            error = "Choose an existing Projects root in Igloo Settings."
            return
        }
        invoke(["snapshot"], as: Snapshot.self) { value in
            self.snapshot = value
            if self.typeFilter != "All types" && !value.projects.contains(where: { $0.kind == self.typeFilter }) {
                self.typeFilter = "All types"
            }
            if self.selection == nil { self.selection = value.projects.max(by: { $0.size < $1.size })?.id }
            self.status = "\(value.projects.count) catalog entries · \(value.source)"
            self.smokeIfRequested()
            if !self.reminderAgentSynced,
               ReminderRoute.shouldSyncAgent(
                    enabled: self.preferences.quietReminderEnabled,
                    agentExists: ReminderRoute.agentExists(bundleIdentifier: Bundle.main.bundleIdentifier)) {
                self.reminderAgentSynced = true
                self.syncReminderAgent()
            }
        }
    }
    func analyze() {
        invoke(["scan"], as: Reply.self) { _ in self.load() }
    }
    func action(_ args: [String]) {
        invoke(args, as: Reply.self) { reply in
            let failures = reply.results?.filter { !$0.ok } ?? []
            let moved = reply.results?.filter { $0.ok }.count ?? 0
            if args.first == "trash", let outcome = reply.outcome {
                let message = reply.message ?? "Cleanup finished. Review the observed space and restore history."
                self.load()
                self.cleanupResult = CleanupResult(message: message, results: reply.results ?? [], outcome: outcome)
                return
            }
            var lines = failures.prefix(5).map { "Could not move: \($0.path)\n\($0.message)" }
            if reply.results != nil { lines.insert("\(moved) directories moved; \(failures.count) could not be moved. Full move records are in History.", at: 0) }
            let message = ([reply.message].compactMap { $0 } + lines).joined(separator: "\n\n")
            self.load()
            if !message.isEmpty {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                    let alert = NSAlert()
                    alert.messageText = "Igloo"
                    alert.informativeText = message
                    alert.addButton(withTitle: "OK")
                    alert.runModal()
                }
            }
        }
    }
    func preview(_ paths: [String]) {
        invoke(["preview"] + paths, as: CleanupPlan.self) { self.plan = $0 }
    }
    func reveal(_ path: String) {
        guard !isDemo else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }
    func terminal(_ path: String) {
        guard !isDemo else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        p.arguments = ["-a", "Terminal", path]
        try? p.run()
    }
    func export(_ project: Project) {
        guard !isDemo else { return }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = project.name + " — project report.txt"
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let holds = project.holds.map { "- " + $0 }.joined(separator: "\n")
        let branches = project.branches.map { "\($0.name): \($0.coverage) — \($0.sha)" }.joined(separator: "\n")
        let detached = (project.detachedHeads ?? []).map { "\($0.path): \($0.sha) — \($0.uncovered.map(String.init) ?? "unverified") commits outside verified tips" }.joined(separator: "\n")
        let ignored = project.ignoredFiles.map { $0.checkout + "/" + $0.path }.joined(separator: "\n")
        let text = """
        Project report: \(project.name)
        Path: \(project.path)
        Hold: \(project.hold == true ? "On" : "Off")
        Notes: \(project.note)
        Snapshot: \(snapshot?.generated ?? "")
        Remote check: \(project.verifiedAt)

        Review before preservation
        \(holds)

        Branches
        \(branches)

        Detached HEADs
        \(detached)

        Ignored files/directories to review separately
        \(ignored)

        Preserve changes, stashes, local tags, ignored configuration and runtime data.
        Verify backups before removing source checkouts. Do not bulk-push private/archive history.
        """
        do { try text.write(to: url, atomically: true, encoding: .utf8) }
        catch { self.error = error.localizedDescription }
    }
    func smokeIfRequested() {
        if isDemo && DemoCapture.options != nil && !smokeStarted {
            smokeStarted = true
            DemoCapture.start(store: self)
            return
        }
        guard CommandLine.arguments.contains("--smoke-test"), !smokeStarted else { return }
        smokeStarted = true
        if let match = ProcessInfo.processInfo.environment["ORGANIZER_SMOKE_PROJECT"] {
            selection = snapshot?.projects.first(where: { $0.name == match })?.id
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
            guard let window = NSApp.windows.first(where: { $0.isVisible }), let view = window.contentView else { NSApp.terminate(nil); return }
            if let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                view.cacheDisplay(in: view.bounds, to: bitmap)
                if let data = bitmap.representation(using: .png, properties: [:]) {
                    try? data.write(to: self.stateDirectory.appendingPathComponent("smoke.png"))
                }
            }
            let text = "entries=\(self.snapshot?.projects.count ?? 0)\nselected=\(self.selected?.name ?? "")\n"
            try? text.write(to: self.stateDirectory.appendingPathComponent("smoke.txt"), atomically: true, encoding: .utf8)
            NSApp.terminate(nil)
        }
    }
}

// A sheet with this modifier shows store errors itself. The main window alert waits while such a sheet is open,
// so a refusal shows where the user acted and no alert is left for after the sheet closes.
struct SheetErrorAlert: ViewModifier {
    @ObservedObject var store: OrganizerStore
    @LocalState private var host = UUID()
    func body(content: Content) -> some View {
        content
            .alert("Action stopped", isPresented: Binding(get: { store.error != nil && store.sheetErrorHosts.last == host },
                                                          set: { if !$0 { store.error = nil } })) {
                Button("OK") { store.error = nil }
            } message: { Text(store.error ?? "") }
            .onAppear { store.sheetErrorHosts.append(host) }
            .onDisappear { store.sheetErrorHosts.removeAll { $0 == host } }
    }
}
extension View {
    func sheetErrorAlert(_ store: OrganizerStore) -> some View { modifier(SheetErrorAlert(store: store)) }
}
struct SheetProgressRow: View {
    let text: String
    var body: some View {
        HStack(spacing: 6) {
            ProgressView().controlSize(.small)
            Text(text).font(.caption).foregroundStyle(.secondary)
        }.accessibilityElement(children: .combine)
            .accessibilityIdentifier("sheet.progress")
    }
}

struct OrganizerView: View {
    @EnvironmentObject var store: OrganizerStore
    @EnvironmentObject var updater: AppUpdater
    @ObservedObject private var reminderRouter = ReminderRouter.shared
    @Environment(\.openWindow) private var openWindow
    @AppStorage("compactSidebar") private var compactSidebarPreference = false
    @AppStorage("scopeInspectorShown") private var scopeInspectorPreference = true
    @LocalState private var demoCompactSidebar = DemoCapture.options?.rail == "compact"
    @LocalState private var demoScopeInspector = DemoCapture.options?.scope != "hidden"
    private var compactSidebar: Bool { DemoCapture.options == nil ? compactSidebarPreference : demoCompactSidebar }
    private var scopeInspectorShown: Bool { DemoCapture.options == nil ? scopeInspectorPreference : demoScopeInspector }
    @LocalState private var navigationPopover = false
    var body: some View {
        GeometryReader { geometry in
            let showRail = DemoCapture.options == nil
                ? compactSidebar || geometry.size.width < (scopeInspectorShown ? 1320 : 1100)
                : compactSidebar
            HStack(spacing: 0) {
                OrganizerNavigation(compact: showRail)
                    .frame(width: showRail ? 48 : 224)
                Divider()
            VStack(spacing: 0) {
                HStack {
                    Text(noun(store.visible.count, "project")).foregroundStyle(.secondary).lineLimit(1)
                    Spacer()
                    Picker("Sort", selection: $store.order) {
                        Text("Largest first").tag("Largest first")
                        Text("Most cleanup").tag("Most cleanup")
                        Text("Quietest first").tag("Quietest first")
                        Text("Name").tag("Name")
                    }.labelsHidden().frame(width: 135)
                }.padding(12)
                Picker("Project type", selection: $store.typeFilter) {
                    Text("All types").tag("All types")
                    ForEach(Array(Set((store.snapshot?.projects ?? []).map(\.kind))).sorted(), id: \.self) { Text($0).tag($0) }
                }.padding(.horizontal, 12).padding(.bottom, 10)
                Divider()
                List(selection: $store.selection) {
                    ForEach(store.visible) { project in
                        ProjectRow(project: project).tag(project.id)
                            .contextMenu {
                                if store.busy { Text("Working…") }
                                Button("Preserve…") { store.freezeMode = "preserve"; store.freezeSetup = project }
                                    .disabled(!project.git || store.busy)
                                    .help(store.busy ? "Working…" : (project.git ? "Preserve this project." : "Igloo can preserve Git projects only."))
                                Button("Freeze…") { store.freezeMode = "freeze"; store.freezeSetup = project }
                                    .disabled(!project.git || store.busy)
                                    .help(store.busy ? "Working…" : (project.git ? "Freeze this project." : "Igloo can preserve Git projects only."))
                            }
                    }
                }
                .overlay {
                    if store.visible.isEmpty {
                        ContentUnavailableView(store.snapshot?.projects.isEmpty == false ? "No matching projects" : "Start with an analysis",
                                               systemImage: "folder.badge.questionmark",
                                               description: Text("Analyze Projects discovers folders, branches, worktrees and cleanup candidates."))
                    }
                }
            }
            .frame(width: 250)
            .searchable(text: $store.search, prompt: "Find a project or note")
            Divider()
            Group {
                if let project = store.selected {
                    ProjectDetail(project: project).id(project.id)
                } else {
                    ContentUnavailableView("Choose a project", systemImage: "folder", description: Text("Review its history and local files before cleaning up."))
                }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
            if scopeInspectorShown {
                Divider()
                ScopeInspector().environmentObject(store).frame(width: 270)
            }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(minWidth: scopeInspectorShown ? 1300 : (compactSidebar ? 850 : 1100), minHeight: 700)
        .toolbar {
            ToolbarItem(placement: .navigation) {
                Button {
                    let width = NSApp.keyWindow?.contentLayoutRect.width ?? 1440
                    if width < (scopeInspectorShown ? 1320 : 1100) { navigationPopover = true }
                    else if DemoCapture.options == nil { compactSidebarPreference.toggle() }
                    else { demoCompactSidebar.toggle() }
                } label: {
                    Label("Toggle navigation", systemImage: "sidebar.left")
                }.help("Expand or collapse navigation")
                    .popover(isPresented: $navigationPopover, arrowEdge: .bottom) {
                        OrganizerNavigation().environmentObject(store).frame(width: 224, height: 650)
                    }
            }
            ToolbarItem(placement: .primaryAction) {
                Button { store.analyze() } label: { Label("Analyze Projects", systemImage: "arrow.triangle.2.circlepath") }
                    .help(store.busy ? "Wait for the current operation to finish." : "Analyze Projects (Shift-Command-R)").accessibilityLabel("Analyze Projects")
                    .keyboardShortcut("r", modifiers: [.command, .shift]).disabled(store.busy)
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: 0) {
                Divider()
                HStack(spacing: 10) {
                    if store.busy { ProgressView().controlSize(.small) }
                    Text(store.status).lineLimit(1)
                    Spacer()
                    if let snapshot = store.snapshot {
                        if !snapshot.errors.isEmpty {
                            Button("\(snapshot.errors.count) scan issues") {
                                let alert = NSAlert()
                                alert.messageText = "Some folders could not be inspected"
                                alert.informativeText = snapshot.errors.joined(separator: "\n")
                                alert.runModal()
                            }.buttonStyle(.link)
                        }
                        Text((snapshot.projects.isEmpty ? "No projects cataloged" : "\(bytes(snapshot.totalSize)) cataloged") + " · \(bytes(snapshot.freeSpace)) free").monospacedDigit().lineLimit(1)
                    }
                    Divider().frame(height: 16)
                    Button {
                        if DemoCapture.options == nil { scopeInspectorPreference.toggle() }
                        else { demoScopeInspector.toggle() }
                    } label: {
                        Label(scopeInspectorShown ? "Hide scope" : "Show scope", systemImage: "chart.pie")
                    }.help("Show or hide the current scope visualization")
                        .keyboardShortcut("i", modifiers: [.command, .option])
                }.font(.caption).foregroundStyle(.secondary).padding(.horizontal, 14).padding(.vertical, 8)
            }.background(.bar)
        }
        .sheet(item: $store.demoCaptureSheet) { sheet in DemoCapture.sheetRoot(sheet.view, store: store) }
        .sheet(item: $store.plan) { CleanupReview(plan: $0).environmentObject(store) }
        .sheet(item: $store.cleanupResult) { result in
            CleanupResultView(result: result) { store.cleanupResult = nil }
        }
        .sheet(isPresented: $store.historyShown) { HistoryView().environmentObject(store) }
        .sheet(item: $store.pushRequest) { PushReview(request: $0).environmentObject(store) }
        .sheet(item: $store.remoteSetup) { RemoteSetupView(project: $0).environmentObject(store) }
        .sheet(item: $store.commitSetup) { CommitSetupView(project: $0).environmentObject(store) }
        .sheet(item: $store.freezeSetup) { FreezeSetupView(project: $0, initialMode: store.freezeMode).environmentObject(store) }
        .sheet(isPresented: $store.vaultShown) { FrozenProjectsView().environmentObject(store) }
        .sheet(isPresented: $store.helpShown) { AppHelpView().environmentObject(store) }
        .sheet(isPresented: $store.tasksShown) { TasksView().environmentObject(store) }
        .sheet(isPresented: $store.wizardShown) {
            CleanupWizardView(startMakeSpace: store.wizardMakeSpace).environmentObject(store)
        }
        .sheet(isPresented: $store.spaceShown) { SpaceView(initialProjectIDs: store.explorerProjectIDs).environmentObject(store) }
        .sheet(isPresented: $store.repositoriesShown) { RemoteRepositoriesView().environmentObject(store) }
        .alert("Action stopped", isPresented: Binding(get: { store.error != nil && store.sheetErrorHosts.isEmpty },
                                                      set: { if !$0 { store.error = nil } })) {
            Button("OK") { store.error = nil }
        } message: { Text(store.error ?? "") }
        .onChange(of: store.typeFilter) {
            if !store.visible.contains(where: { $0.id == store.selection }) { store.selection = store.visible.first?.id }
        }
        .onChange(of: store.filter) {
            if !store.visible.contains(where: { $0.id == store.selection }) { store.selection = store.visible.first?.id }
        }
        .onChange(of: reminderRouter.makeSpaceRequested) {
            openReminderWizardIfRequested()
        }
        .onAppear {
            store.load()
            openReminderWizardIfRequested()
            if CommandLine.arguments.contains("--update-install-failed") || updater.failed {
                openWindow(id: "updates")
            }
        }
    }
    private func openReminderWizardIfRequested() {
        guard reminderRouter.makeSpaceRequested else { return }
        reminderRouter.makeSpaceRequested = false
        NSApp.unhide(nil)
        NSApp.activate(ignoringOtherApps: true)
        if !NSApp.windows.contains(where: \.isVisible) { openWindow(id: "catalog") }
        store.openMakeSpaceWizard()
    }
}

struct ProjectRow: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    private var isSelected: Bool { store.selection == project.id }
    private var duplicateName: Bool {
        (store.snapshot?.projects.filter { $0.name == project.name }.count ?? 0) > 1
    }
    private var subtitle: String {
        let metadata = project.kind + (project.technologyText.isEmpty ? "" : " · " + project.technologyText)
        guard duplicateName else { return metadata }
        let folder = URL(fileURLWithPath: project.path).lastPathComponent
        return metadata + (folder.isEmpty ? "" : " · " + folder)
    }
    private var preservationHint: String? {
        guard project.needsPreservation, (project.blockers ?? []).isEmpty else { return nil }
        if !project.changes.isEmpty { return "Uncommitted changes" }
        if !project.stashes.isEmpty { return "Git stashes" }
        if project.localFiles.contains(where: { !$0.template }) { return "Local files" }
        if project.unknown == 0 && project.refs.contains(where: { !$0.verified }) { return "Unverified refs" }
        if (project.detachedHeads ?? []).contains(where: { $0.uncovered == nil || ($0.uncovered ?? 0) > 0 }) {
            return "Detached commits to review"
        }
        return nil
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(project.name).fontWeight(.medium).lineLimit(1)
                Spacer(minLength: 8)
                Text(bytes(project.size)).monospacedDigit().foregroundStyle(.secondary).font(.callout)
            }
            Text(subtitle)
                .font(.caption).foregroundStyle(.secondary).lineLimit(2).help(project.path)
            HStack(spacing: 8) {
                if project.active { Label("Active", systemImage: "bolt.fill") }
                else if let days = project.ageDays { Text("\(days)d since Git activity") }
                if project.uncovered > 0 {
                    HStack(spacing: 4) {
                        Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.orange).accessibilityHidden(true)
                        Text(noun(project.uncovered, "branch", "branches") + " to review")
                    }
                }
                else if project.unknown > 0 { Text("Unverified history").foregroundStyle(.secondary) }
                else if !project.git { Text("Local folder") }
                if let preservationHint {
                    Label(preservationHint, systemImage: "tray.full")
                }
                Spacer(minLength: 0)
            }.font(.caption).lineLimit(1)
            ProjectBlockerBadges(blockers: project.blockers ?? [], held: project.hold == true,
                                 selected: isSelected)
                .frame(maxWidth: .infinity, alignment: .leading)
        }.padding(.vertical, 6).help(project.path)
    }
}

struct ProjectDetail: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    @LocalState var tab = DemoCapture.tab ?? ProcessInfo.processInfo.environment["ORGANIZER_SMOKE_TAB"] ?? "Overview"
    @LocalState var fetchConfirm = false
    @LocalState var gitSection = DemoCapture.initialState("gitSection") ?? "Changes"
    @LocalState var hold = false
    @LocalState var note = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 5) {
                    Text(project.name).font(.title2).fontWeight(.semibold).textSelection(.enabled)
                    Text(project.path).font(.caption).foregroundStyle(.secondary).textSelection(.enabled).lineLimit(2)
                }
                Spacer()
                Text(bytes(project.size)).font(.title2).monospacedDigit().foregroundStyle(.secondary)
            }
            ViewThatFits(in: .horizontal) {
                HStack {
                    projectActions
                }.fixedSize(horizontal: true, vertical: false)
                HStack {
                    Menu("Project actions") { projectActions }
                    Spacer()
                }
            }.disabled(store.busy)
                .help(store.busy ? "Wait for the current operation to finish." : "Project actions for the selected project.")
            VStack(spacing: 0) {
                Picker("Project detail", selection: $tab) {
                    Text("Overview").tag("Overview")
                    Text("Git").tag("Branches")
                    Text("Worktrees").tag("Worktrees")
                    Text("Local files").tag("Local files")
                    Text("Cleanup").tag("Cleanup")
                    Text("Preservation").tag("Review plan")
                }.pickerStyle(.segmented).controlSize(.small).labelsHidden().padding(10)
                    .accessibilityIdentifier("detail.tabs")
                Divider()
                GeometryReader { pane in
                    Group {
                        switch tab {
                        case "Branches": ProjectGitView(project: project, section: $gitSection)
                        case "Worktrees": WorktreesView(project: project)
                        case "Local files": LocalFilesView(project: project)
                        case "Cleanup": CleanupView(project: project)
                        case "Review plan": PlanView(project: project, hold: $hold, note: $note)
                        default:
                            OverviewView(project: project) { target, section in
                                tab = target
                                if !section.isEmpty { gitSection = section }
                            }
                        }
                    }.frame(width: pane.size.width, height: pane.size.height).clipped()
                }
            }
            .background(Color(nsColor: .controlBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 7))
            .overlay(RoundedRectangle(cornerRadius: 7).stroke(Color(nsColor: .separatorColor), lineWidth: 1))

        }
        .padding(20)
        .frame(minWidth: 500, maxHeight: .infinity)
        .navigationTitle(project.name)
        .onAppear { hold = project.hold == true; note = project.note }
        .confirmationDialog("Fetch current Git metadata?", isPresented: $fetchConfirm, titleVisibility: .visible) {
            Button("Fetch & verify") { store.action(["fetch", project.path]) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Downloads commits and updates remote-tracking refs for this project. It does not change source files, switch branches or prune refs. This can resolve uncertain branch coverage.")
        }
    }
    @ViewBuilder private var projectActions: some View {
        Button("Show in Finder") { store.reveal(project.path) }
        Button("Open Terminal") { store.terminal(project.path) }
        Button("Preserve…") { store.freezeMode = "preserve"; store.freezeSetup = project }
            .disabled(!project.git).help(project.git ? "Preserve this project." : "Igloo can preserve Git projects only.")
        Button("Freeze…") { store.freezeMode = "freeze"; store.freezeSetup = project }
            .disabled(!project.git).help(project.git ? "Freeze this project." : "Igloo can preserve Git projects only.")
        if project.git {
            Button("Refresh project") { store.action(["refresh", project.path]) }
            Button("Fetch & verify…") { fetchConfirm = true }.disabled(project.remotes.isEmpty)
                .help(project.remotes.isEmpty ? "Connect a remote before fetching and verifying refs." : "Fetch remote metadata and verify branch coverage.")
        }
    }

}

struct IglooApp: App {
    @StateObject var store = OrganizerStore()
    @StateObject var updater = AppUpdater()
    @NSApplicationDelegateAdaptor(ReminderAppDelegate.self) var reminderDelegate
    var body: some Scene {
        WindowGroup("Igloo", id: "catalog") {
            OrganizerView().environmentObject(store).environmentObject(updater)
                .onAppear {
                    updater.demo = store.isDemo
                    if store.isDemo && CommandLine.arguments.contains("--demo-light") {
                        NSApp.appearance = NSAppearance(named: .aqua)
                    } else if store.isDemo && CommandLine.arguments.contains("--demo-dark") {
                        NSApp.appearance = NSAppearance(named: .darkAqua)
                    }
                }
                // An open catalog window takes the reminder URL, so SwiftUI does not open another window.
                .handlesExternalEvents(preferring: ["remind"], allowing: ["*"])
        }
        // The app delegate handles igloo://remind; the URL never creates a catalog window.
        .handlesExternalEvents(matching: [])
        .defaultSize(width: 1440, height: 900)
        .windowResizability(.contentMinSize)
        .commands {
            OrganizerAboutCommands(updater: updater)
            CommandGroup(replacing: .help) {
                Button("Igloo Help") { store.helpShown = true }
                    .keyboardShortcut("?", modifiers: [.command])
            }
            CommandGroup(after: .newItem) {
                Button("Analyze Projects") { store.analyze() }.keyboardShortcut("r", modifiers: [.command, .shift]).disabled(store.busy)
                    .help(store.busy ? "Wait for the current operation to finish." : "Analyze Projects.")
                Button("Trash & Restore History") { store.historyShown = true }
            }
        }
        Window("About Igloo", id: "about") {
            AboutView().environmentObject(store).environmentObject(updater)
        }
        .windowResizability(.contentSize)
        .windowStyle(.hiddenTitleBar)
        .defaultPosition(.center)
        Window("Igloo updates", id: "updates") {
            UpdatesView().environmentObject(updater)
        }.windowResizability(.contentSize).defaultPosition(.center)
        Settings { AppSettingsView().environmentObject(store) }
    }
}

struct ReminderRunnerApp: App {
    @NSApplicationDelegateAdaptor(ReminderAppDelegate.self) var reminderDelegate
    var body: some Scene {
        Settings { EmptyView() }
    }
}

@main enum IglooMain {
    static func main() {
        if ReminderRoute.isReminderLaunch(arguments: CommandLine.arguments) {
            if ReminderHandoff.toRunningCopy() { exit(0) }
            ReminderRunnerApp.main()
        } else {
            IglooApp.main()
        }
    }
}


struct CommitReviewPlan: Decodable {
    var id: String; var created: String; var path: String; var files: [String]
}
struct PublishRef: Decodable, Identifiable {
    var ref: String; var sha: String
    var id: String { ref }
}
struct FreezeReviewPlan: Decodable {
    var id: String; var created: String; var name: String; var path: String; var destination: String
    var publish: [PublishRef]; var localRefs: [PublishRef]; var checkouts: [String]
    var localCount: Int; var localSize: Int64; var localFiles: [String]; var excluded: [String]
    var archiveLocation: String; var message: String
    var mode: String?; var outcome: ReviewOutcome?
}
struct FrozenArchive: Decodable, Identifiable {
    var id: String; var name: String; var path: String; var state: String; var created: String
    var destination: String; var message: String; var archiveLocation: String; var checkouts: Int
    var paths: [String]?; var leftovers: [String]?
}
struct GitHubStatus: Decodable {
    var authenticated: Bool; var login: String; var message: String; var owners: [String]?
}

struct AppHelpView: View {
    @EnvironmentObject var store: OrganizerStore
    @LocalState var topic = DemoCapture.initialState("helpTopic") ?? "Getting started"
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Igloo Help").font(.title2).fontWeight(.semibold)
            Picker("Help topic", selection: $topic) {
                Text("Getting started").tag("Getting started")
                Text("Freeze & thaw").tag("Freeze & thaw")
                Text("Safety & storage").tag("Safety & storage")
            }.labelsHidden().pickerStyle(.segmented).frame(maxWidth: .infinity)
            ScrollView {
                if topic == "Getting started" {

                    VStack(alignment: .leading, spacing: 18) {
                        helpSection("Analyze your projects", "Analyze Projects is the circular arrows button in the toolbar (Shift-Command-R). It discovers repositories and linked worktrees, measures folders and checks configured remotes. It reads project metadata and filenames without changing checkouts. Source dates and observed process activity are estimates of use.")
                        helpSection("Settings and installation", "The app bundles its backend and can be copied to Applications. Settings lets you choose the workspace root, Freezer and state locations, clone folder, quiet threshold, saved Freeze threshold used by quiet-project reminders, reminder day and time, GitHub owner default and installed tool paths. The reminder refreshes age from local Git history or elapsed time since the file-age scan, without fetching. It skips Hold and Frozen projects and asks for notification permission when you turn it on. Python, Git and gh remain local prerequisites.")
                        helpSection("Tasks and schedules", "Tasks save reusable cleanup rules for selected projects. Start with a Review that lists the exact folders before anything changes. You can run a reviewed cache cleanup or schedule one after giving permission. Past the freeze threshold, the wizard suggests Freeze and waits for your Review. Tasks do not commit or push source, Freeze projects, or remove their checkouts. If you change the rules, enable the schedule again. Daily and weekly schedules run while you are logged in; busy runs are skipped and recorded. To run a saved task from Terminal, choose Install CLI in Tasks. When the CLI is installed, the button is Reinstall CLI.")
                        helpSection("Project wizard", "Choose Make space to set Git activity or source-file age rules, suggest cleanup and Freeze actions, and review them before one acceptance. Choose Thaw projects to select several Frozen projects and review each remote ref, original path, local-only files and blockers. One acceptance restores every ready row; a failure in one row does not stop the others. The remote supplies available refs, and the Freezer archive supplies Frozen refs missing from it. Wizard Thaw does not install dependencies or run project code, and says to reinstall dependencies for each restored project. Offline recovery remains available per project in Freezer.")
                        helpSection("Explore space", "Space explorer offers sunburst, icicle and Marimekko views of project sizes and measured dependency/build types. Filter projects by age, detected activity or type, and color by type, activity or age. Select a segment to focus a project; Open project returns to its cleanup and preservation tools. Other project files is the unclassified remainder, not a claim that data is safe to delete.")
                        helpSection("Browse GitHub", "GitHub repositories lets you browse your account or an organization, search, hide existing local clones, select several repositories and clone them into a chosen project folder. Existing paths are never overwritten. Run Analyze Projects afterward to index them.")
                        helpSection("Project types and technology", "Analysis suggests project purpose separately from technology using bounded filenames and package metadata. Overview lets you correct the project type or reset it to Automatic. Catalog search and the type filter use these labels. Space explorer groups, colors and filters by project purpose, technology, age and activity. Complete technology combinations keep aggregate sizes from being counted more than once.")
                        helpSection("Current scope", "Show scope / Hide scope at the bottom-right toggles the visualization beside your work (Option-Command-I). It follows your current filters or the selected project. Choose a breakdown or open the full Space Explorer with this scope. Narrow windows retain the icon rail; the navigation button opens full labels in a popover when there is not room to expand.")
                        helpSection("Choose what to review", "The sidebar groups project views, review filters, tools and recovery. Its toolbar button collapses it to an icon rail. Quiet projects shows its age threshold below the project views. Needs preservation highlights local history, changed files, stashes and local data. Cleanup candidates lists ignored dependencies and tool caches.")
                        helpSection("Connect GitHub", "Open a project’s Preservation tab. Connect an existing remote, or create a private GitHub repository. Your gh login supplies the personal-account default; you can explicitly choose an organization. Local folder labels never choose the GitHub owner or visibility.")
                        helpSection("Commit and publish", "Commit selected files lets you review a specific checkout and file selection. Other staged files must be reviewed too. Environment, key and data filenames stay local. Git → Branches can push one reviewed commit and destination; it never force-pushes.")
                        helpSection("Clean caches", "Select exact directories in Cleanup and review a fresh preview. Active sessions, tracked files, nested repositories and local configuration can stop cleanup. Results show Freed separately from In Trash. Moves stay recoverable in Trash; restoring an item lowers its run's In Trash value. Space is freed when Trash is emptied separately. See the Space report in Trash & restore history for run totals.")
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(18).padding(.bottom, 30)
                } else if topic == "Freeze & thaw" {

                    VStack(alignment: .leading, spacing: 18) {
                        helpSection("Prepare a project", "Commit reviewed source changes, then close its shells, agents and applications. Igloo checks for them again before cleanup or Freeze. Ignored local files such as .env files and databases can remain. All registered worktrees are reviewed together.")
                        helpSection("Requirements and Hold", "The project list shows a badge when a quick check finds a blocker. Open Preservation and choose Preserve or Freeze to see the Requirements checklist and available fixes. Both need a remote. Hold excludes a project from wizard and task proposals; manual actions remain available and show a warning.")
                        helpSection("Preserve", "Preserve reviews ordinary branches and tags, local-only files and rebuild folders. It stores local files and Git recovery in the Freezer, tests reconstruction, pushes the reviewed refs and verifies them remotely. Private refs, stashes and reflogs stay in local recovery, and the checkout remains.")
                        helpSection("Freeze", "Build a Review and inspect its outcome summary for recovery sets, refs, checkout removals and estimated space. Accept once to Preserve, verify recovery and the remote, then remove the reviewed checkouts. A small project marker and the Freezer copy remain. If removal cannot finish, the result explains why and the preserved archive stays available.")
                        helpSection("Thaw later", "Use Project wizard → Thaw projects to restore several Frozen projects from their remotes in one Review. Freezer still offers remote Thaw and offline recovery for one project at a time. Thaw restores the Frozen source state, local files and Git/worktree metadata without overwriting occupied paths. Reinstall dependencies, then pull newer remote changes when ready.")
                        helpSection("Recover interruptions", "If removal or restoration is interrupted, use the same archive’s recovery action again. Already restored files are checked before continuing. Possible post-interruption edits or recovery copies are preserved for review.")
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(18).padding(.bottom, 30)
                } else if topic == "Safety & storage" {

                    VStack(alignment: .leading, spacing: 18) {
                        helpSection("Freezer storage", "Settings → Freezer sets the Freezer location. The current location is " + displayPath(store.preferences.archiveRoot) + ". It contains local-only data and Git metadata and is not encrypted. You can choose a backup folder, including cloud storage after confirming unencrypted upload. A missing backup never blocks Preserve.")
                        helpSection("Freezer backup", "When a backup folder is configured, Igloo copies recovery there after Preserve and checks its checksums. In Freezer, Back up now retries missing copies and Verify backup checks existing copies. Backups are only added automatically; use Remove from backup for copies no longer in the Freezer.")
                        helpSection("Updates", "Check for Updates shows release notes and downloads a verified update for your runtime. Choose Install and relaunch to verify the downloaded app signature, replace the app, and reopen it. The previous app stays in its backup until the new process remains open briefly. Installation requires a writable, non-translocated app location and never asks for an administrator password. If installation fails, Igloo restores and reopens the previous app and shows the failure in the update window. Close any dialog that blocks termination, then try again. macOS privacy permissions may need approval again after an ad-hoc-signed update. If a check or download fails, the window explains the problem and links to releases. For a request limit, wait until the displayed UTC reset time when available.")
                        helpSection("App state", "Settings → Runtime state sets the state folder. The current folder is " + displayPath(store.preferences.stateRoot) + ". Preferences live in " + displayPath(UserPreferences.file.path) + ". Keep the Trash journal while you may need restoration. The app’s own source can use Git setup and commits; it is protected from cache cleanup and freezing.")
                        helpSection("What is omitted", "Only reviewed ignored dependency and rebuild folders without tracked content are omitted. Igloo preserves recognized local configuration inside those folders. Custom dependency edits need your review. External symlink targets remain external; the archive preserves the link, not its target.")
                        helpSection("What checks mean", "Unverified branch coverage may need Fetch & verify to download current remote metadata. Fetch does not switch branches or prune refs. Hash checks and recovery tests protect against changed or corrupt archives; they do not certify that committed history is free of private data.")
                        helpSection("When an action stops", "Leave source and archive copies in place. Resolve the reported active process, changed files, remote access or destination conflict, then create a fresh review or resume recovery. The app never empties Trash or force-pushes.")
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(18).padding(.bottom, 30)
                }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
            HStack {
                Spacer()
                Button("Done") { store.helpShown = false }.keyboardShortcut(.cancelAction)
            }
        }.padding(24).frame(width: sheetWidth(860), height: 700)
    }
    private func displayPath(_ path: String) -> String { (path as NSString).abbreviatingWithTildeInPath }
    private func helpSection(_ title: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.headline)
            Text(text).font(.callout).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        }
    }
}
