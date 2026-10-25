import SwiftUI
import AppKit
import Combine

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
struct Project: Decodable, Identifiable {
    var id: String; var name: String; var path: String; var common: String; var category: String
    var projectType: String?; var detectedType: String?; var languages: [String]?; var typeEvidence: [String]?
    var kind: String { projectType ?? "Unclassified" }
    var technologyText: String { (languages ?? []).joined(separator: ", ") }
    var size: Int64; var git: Bool; var ageDays: Int?; var fileAgeDays: Int?; var lastCommit: String; var remoteStatus: String
    var verifiedAt: String; var remotes: [Remote]; var active: Bool; var activityKnown: Bool; var processes: [String]
    var branches: [Branch]; var worktrees: [Worktree]; var changes: [ChangedFile]; var stashes: [String]
    var refs: [Ref]; var detachedHeads: [DetachedHead]?; var localFiles: [LocalFile]; var ignoredFiles: [IgnoredFile]; var candidates: [Candidate]
    var holds: [String]; var uncovered: Int; var unknown: Int; var decision: String; var note: String
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
    var id: String; var created: String; var items: [PlanItem]; var blocked: [BlockedItem]; var totalSize: Int64
}
struct Reply: Decodable {
    var message: String?; var error: String?; var results: [ActionResult]?
}
struct ActionResult: Decodable { var path: String; var ok: Bool; var message: String }
struct PushRequest: Identifiable {
    var project: Project; var branch: Branch; var remote: Remote
    var id: String { project.id + branch.name }
}
func bytes(_ value: Int64) -> String {
    ByteCountFormatter.string(fromByteCount: value, countStyle: .binary)
}
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
    @Published var historyShown = false
    @Published var pushRequest: PushRequest?
    @Published var remoteSetup: Project?
    @Published var commitSetup: Project?
    @Published var freezeSetup: Project?
    @Published var vaultShown = false
    @Published var helpShown = false
    @Published var repositoriesShown = false
    @Published var spaceShown = false
    @Published var explorerProjectIDs: Set<String>?
    @Published var wizardShown = false
    @Published var tasksShown = false
    var isDemo: Bool { CommandLine.arguments.contains("--demo") }
    var progressTimer: Timer?
    var smokeStarted = false

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
            case "Cleanup opportunities": match = p.cleanupSize > 0
            case "Needs preservation": match = p.needsPreservation
            case "Quiet projects": match = !p.active && (p.ageDays ?? -1) >= quietDays
            case "No remote": match = p.git && p.remotes.isEmpty
            case "No Git metadata": match = !p.git
            case "Active sessions": match = p.active
            case "Marked to retire": match = p.decision == "Retire"
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
    func invoke<T: Decodable>(_ args: [String], as type: T.Type, done: @escaping (T) -> Void) {
        if isDemo {
            if let value: T = DemoCatalog.reply(args, as: type) { done(value) }
            else { error = "The example catalog is read-only." }
            return
        }
        guard !busy else { return }
        busy = true
        status = args.first == "scan" ? "Discovering projects, measuring folders and checking remotes…" : "Checking local state…"
        progressTimer?.invalidate()
        if ["scan", "freeze-preview", "freeze-execute", "freeze-retire", "rehydrate", "wizard-scan", "wizard-prepare", "wizard-execute", "task-run", "task-apply"].contains(args.first ?? "") {
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
                }
            }
        }
    }
    func load() {
        if isDemo {
            snapshot = DemoCatalog.snapshot()
            if selection == nil { selection = snapshot?.projects.first?.id }
            status = "Demo catalog · fictional example projects"
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
        }
    }
    func analyse() {
        invoke(["scan"], as: Reply.self) { _ in self.load() }
    }
    func action(_ args: [String]) {
        invoke(args, as: Reply.self) { reply in
            let failures = reply.results?.filter { !$0.ok } ?? []
            let moved = reply.results?.filter { $0.ok }.count ?? 0
            var lines = failures.prefix(5).map { "Held: \($0.path)\n\($0.message)" }
            if reply.results != nil { lines.insert("\(moved) directories moved; \(failures.count) held. Full move records are in History.", at: 0) }
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
        panel.nameFieldStringValue = project.name + " — preservation plan.txt"
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let holds = project.holds.map { "- " + $0 }.joined(separator: "\n")
        let branches = project.branches.map { "\($0.name): \($0.coverage) — \($0.sha)" }.joined(separator: "\n")
        let detached = (project.detachedHeads ?? []).map { "\($0.path): \($0.sha) — \($0.uncovered.map(String.init) ?? "unverified") commits outside verified tips" }.joined(separator: "\n")
        let ignored = project.ignoredFiles.map { $0.checkout + "/" + $0.path }.joined(separator: "\n")
        let text = """
        Preservation plan: \(project.name)
        Path: \(project.path)
        Decision: \(project.decision)
        Notes: \(project.note)
        Snapshot: \(snapshot?.generated ?? "")
        Remote check: \(project.verifiedAt)

        Review before retirement
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

struct OrganizerView: View {
    @EnvironmentObject var store: OrganizerStore
    @AppStorage("compactSidebar") private var compactSidebar = false
    @AppStorage("scopeInspectorShown") private var scopeInspectorShown = true
    @LocalState private var navigationPopover = false
    var body: some View {
        GeometryReader { geometry in
            let showRail = compactSidebar || geometry.size.width < (scopeInspectorShown ? 1320 : 1100)
            HStack(spacing: 0) {
                OrganizerNavigation(compact: showRail)
                    .frame(width: showRail ? 48 : 224)
                Divider()
            VStack(spacing: 0) {
                HStack {
                    Text("\(store.visible.count) projects").foregroundStyle(.secondary).lineLimit(1)
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
                    }
                }
                .overlay {
                    if store.visible.isEmpty {
                        ContentUnavailableView(store.snapshot?.projects.isEmpty == false ? "No matching projects" : "Start with an analysis",
                                               systemImage: "folder.badge.questionmark",
                                               description: Text("Analyse Projects discovers folders, branches, worktrees and cleanup opportunities."))
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
        .frame(minWidth: scopeInspectorShown ? 1100 : 850, minHeight: 700)
        .toolbar {
            ToolbarItem(placement: .navigation) {
                Button {
                    let width = NSApp.keyWindow?.contentLayoutRect.width ?? 1440
                    if width < (scopeInspectorShown ? 1320 : 1100) { navigationPopover = true }
                    else { compactSidebar.toggle() }
                } label: {
                    Label("Toggle navigation", systemImage: "sidebar.left")
                }.help("Expand or collapse navigation")
                    .popover(isPresented: $navigationPopover, arrowEdge: .bottom) {
                        OrganizerNavigation().environmentObject(store).frame(width: 224, height: 650)
                    }
            }
            ToolbarItem(placement: .primaryAction) {
                Button { store.analyse() } label: { Label("Analyse Projects", systemImage: "arrow.triangle.2.circlepath") }
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
                        Text("\(bytes(snapshot.totalSize)) cataloged · \(bytes(snapshot.freeSpace)) free").monospacedDigit().lineLimit(1)
                    }
                    Divider().frame(height: 16)
                    Button { scopeInspectorShown.toggle() } label: {
                        Label(scopeInspectorShown ? "Hide scope" : "Show scope", systemImage: "chart.pie")
                    }.help("Show or hide the current scope visualization")
                        .keyboardShortcut("i", modifiers: [.command, .option])
                }.font(.caption).foregroundStyle(.secondary).padding(.horizontal, 14).padding(.vertical, 8)
            }.background(.bar)
        }
        .sheet(item: $store.plan) { CleanupReview(plan: $0).environmentObject(store) }
        .sheet(isPresented: $store.historyShown) { HistoryView().environmentObject(store) }
        .sheet(item: $store.pushRequest) { PushReview(request: $0).environmentObject(store) }
        .sheet(item: $store.remoteSetup) { RemoteSetupView(project: $0).environmentObject(store) }
        .sheet(item: $store.commitSetup) { CommitSetupView(project: $0).environmentObject(store) }
        .sheet(item: $store.freezeSetup) { FreezeSetupView(project: $0).environmentObject(store) }
        .sheet(isPresented: $store.vaultShown) { FrozenProjectsView().environmentObject(store) }
        .sheet(isPresented: $store.helpShown) { AppHelpView().environmentObject(store) }
        .sheet(isPresented: $store.tasksShown) { TasksView().environmentObject(store) }
        .sheet(isPresented: $store.wizardShown) { CleanupWizardView().environmentObject(store) }
        .sheet(isPresented: $store.spaceShown) { SpaceView(initialProjectIDs: store.explorerProjectIDs).environmentObject(store) }
        .sheet(isPresented: $store.repositoriesShown) { RemoteRepositoriesView().environmentObject(store) }
        .alert("Action stopped", isPresented: Binding(get: { store.error != nil }, set: { if !$0 { store.error = nil } })) {
            Button("OK") { store.error = nil }
        } message: { Text(store.error ?? "") }
        .onChange(of: store.typeFilter) {
            if !store.visible.contains(where: { $0.id == store.selection }) { store.selection = store.visible.first?.id }
        }
        .onChange(of: store.filter) {
            if !store.visible.contains(where: { $0.id == store.selection }) { store.selection = store.visible.first?.id }
        }
        .onAppear { store.load() }
    }
}

struct ProjectRow: View {
    let project: Project
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(project.name).fontWeight(.medium).lineLimit(1)
                Spacer(minLength: 8)
                Text(bytes(project.size)).monospacedDigit().foregroundStyle(.secondary).font(.callout)
            }
            Text(project.kind + (project.technologyText.isEmpty ? "" : " · " + project.technologyText))
                .font(.caption).foregroundStyle(.secondary).lineLimit(1).help(project.category + " · " + project.technologyText)
            HStack(spacing: 8) {
                if project.active { Label("Active", systemImage: "bolt.fill") }
                else if let days = project.ageDays { Text("\(days)d since Git activity") }
                if project.uncovered > 0 { Text("\(project.uncovered) branches to review").foregroundStyle(.orange) }
                else if project.unknown > 0 { Text("Unverified history").foregroundStyle(.secondary) }
                else if !project.git { Text("Local folder") }
                Spacer(minLength: 0)
            }.font(.caption).lineLimit(1)
            if project.decision != "Undecided" {
                Text(project.decision).font(.caption).foregroundStyle(.secondary)
            }
        }.padding(.vertical, 6).help(project.path)
    }
}

struct ProjectDetail: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    @LocalState var tab = ProcessInfo.processInfo.environment["ORGANIZER_SMOKE_TAB"] ?? "Overview"
    @LocalState var fetchConfirm = false
    @LocalState var gitSection = "Changes"
    @LocalState var decision = "Undecided"
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
            VStack(spacing: 0) {
                ScrollView(.horizontal) {
                Picker("Project detail", selection: $tab) {
                    Text("Overview").tag("Overview")
                    Text("Git").tag("Branches")
                    Text("Worktrees").tag("Worktrees")
                    Text("Local files").tag("Local files")
                    Text("Cleanup").tag("Cleanup")
                    Text("Plan").tag("Review plan")
                }.pickerStyle(.segmented).controlSize(.small).labelsHidden().fixedSize(horizontal: true, vertical: false).padding(10)
                }.frame(height: 44)
                Divider()
                GeometryReader { pane in
                    Group {
                        switch tab {
                        case "Branches": ProjectGitView(project: project, section: $gitSection)
                        case "Worktrees": WorktreesView(project: project)
                        case "Local files": LocalFilesView(project: project)
                        case "Cleanup": CleanupView(project: project)
                        case "Review plan": PlanView(project: project, decision: $decision, note: $note)
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
        .onAppear { decision = project.decision; note = project.note }
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
        if project.git {
            Button("Refresh project") { store.action(["refresh", project.path]) }
            Button("Fetch & verify…") { fetchConfirm = true }.disabled(project.remotes.isEmpty)
        }
    }

}

struct PlanView: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    @Binding var decision: String
    @Binding var note: String
    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
            VStack(alignment: .leading, spacing: 14) {
            LifecycleControls(project: project)
            Divider()
            Text("Decide what happens next").font(.headline)
            Picker("Project decision", selection: $decision) {
                ForEach(["Undecided", "Keep", "Review", "Archive", "Retire"], id: \.self) { Text($0).tag($0) }
            }.frame(maxWidth: 320)
            Text("Marking a project for retirement does not delete files or publish history.").font(.callout).foregroundStyle(.secondary)
            Text("Preservation notes").font(.headline)
            TextEditor(text: $note).font(.body).frame(minHeight: 140).border(Color.secondary.opacity(0.25))
                .accessibilityLabel("Preservation notes")
            Text("Before removing a whole checkout: preserve local changes and stashes, review branch and tag history, archive important ignored files, then verify the backup. Keep active sessions intact.")
                .font(.callout).foregroundStyle(.secondary)
            }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            HStack {
                Button("Export preservation plan…") { store.export(project) }
                Spacer()
                Button("Save decision") { store.action(["decide", project.id, decision, note]) }
                    .buttonStyle(.borderedProminent).disabled(store.busy)
            }.padding(12).background(.bar)
        }
    }
}

struct CleanupReview: View {
    @EnvironmentObject var store: OrganizerStore
    let plan: CleanupPlan
    @LocalState var reviewed = false
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Review cleanup").font(.title2).fontWeight(.semibold)
            Text("\(plan.items.count) directories · \(bytes(plan.totalSize)) to move to Trash")
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(plan.items) { item in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(item.path).font(.callout).textSelection(.enabled)
                            Text(bytes(item.size)).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    if !plan.blocked.isEmpty {
                        Divider()
                        Text("Held for review").font(.headline)
                        ForEach(plan.blocked) { item in
                            VStack(alignment: .leading, spacing: 3) {
                                Text(item.path).font(.callout)
                                Text(item.reason).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
            Text("Files remain in Trash until you empty it. Restore through History. Dependencies may contain custom edits; only continue after reviewing the listed folders. This preview expires after ten minutes.")
                .font(.callout).foregroundStyle(.secondary)
            Toggle("I have reviewed these folders for local edits.", isOn: $reviewed)
            HStack {
                Button("Cancel", role: .cancel) { store.plan = nil }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Move \(plan.items.count) directories to Trash") {
                    let id = plan.id
                    store.plan = nil
                    store.action(["trash", id])
                }.buttonStyle(.borderedProminent).disabled(plan.items.isEmpty || !reviewed || store.busy)
            }
        }.padding(24).frame(width: 700, height: 550)
    }
}

struct PushReview: View {
    @EnvironmentObject var store: OrganizerStore
    let request: PushRequest
    @LocalState var reviewed = false
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Publish one branch").font(.title2).fontWeight(.semibold)
            Text(request.branch.name).font(.headline).textSelection(.enabled)
            Text("Destination: \(request.remote.pushURL)\nRef: refs/heads/\(request.branch.name)\nCommit: \(request.branch.sha)")
                .font(.callout).textSelection(.enabled)
            Text("This publishes the selected committed history. Review it for private data first. Other branches, tags, stashes and uncommitted files are excluded. A rejected push will stop; the app never force-pushes.")
                .font(.callout).foregroundStyle(.secondary)
            Toggle("I have reviewed this branch and destination for publication.", isOn: $reviewed)
            HStack {
                Button("Cancel", role: .cancel) { store.pushRequest = nil }
                Spacer()
                Button("Push this branch") {
                    let r = request
                    store.pushRequest = nil
                    store.action(["push", r.project.path, r.branch.name, r.branch.sha, r.remote.name, r.remote.pushURL])
                }.buttonStyle(.borderedProminent).disabled(!reviewed || store.busy)
            }
        }.padding(24).frame(width: 660)
    }
}

struct HistoryView: View {
    @EnvironmentObject var store: OrganizerStore
    @LocalState var selected: String?
    @LocalState var restoreConfirm = false
    var item: TrashItem? { store.snapshot?.history.first { $0.id == selected } }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Trash & restore history").font(.title2).fontWeight(.semibold)
            Table(store.snapshot?.history.reversed().map { $0 } ?? [], selection: $selected) {
                TableColumn("Original directory") { Text($0.original).lineLimit(1).truncationMode(.middle).help($0.original) }.width(min: 320)
                TableColumn("Size") { Text(bytes($0.size)) }.width(90)
                TableColumn("State", value: \.status).width(85)
                TableColumn("Date") { Text(shortDate($0.at)) }.width(90)
            }
            Text("Restore requires the original path to be empty and the Trash item to remain available. The app never overwrites a replacement directory or permanently empties Trash.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Done") { store.historyShown = false }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Show in Trash") { if let i = item { store.reveal(i.destination) } }.disabled(item == nil)
                Button("Restore selected…") { restoreConfirm = true }
                    .disabled(store.busy || item == nil || item?.status == "restored")
            }
        }.padding(24).frame(width: 880, height: 520)
        .confirmationDialog("Restore this directory to its original location?", isPresented: $restoreConfirm, titleVisibility: .visible) {
            Button("Restore") { if let i = item { store.action(["restore", i.id]) } }
            Button("Cancel", role: .cancel) {}
        } message: { Text(item?.original ?? "") }
    }
}

@main struct IglooApp: App {
    @StateObject var store = OrganizerStore()
    @StateObject var updater = AppUpdater()
    var body: some Scene {
        WindowGroup("Igloo", id: "catalog") {
            OrganizerView().environmentObject(store)
                .onAppear {
                    updater.demo = store.isDemo
                    if store.isDemo && CommandLine.arguments.contains("--demo-light") {
                        NSApp.appearance = NSAppearance(named: .aqua)
                    } else if store.isDemo && CommandLine.arguments.contains("--demo-dark") {
                        NSApp.appearance = NSAppearance(named: .darkAqua)
                    }
                }
        }
        .defaultSize(width: 1440, height: 900)
        .windowResizability(.contentMinSize)
        .commands {
            OrganizerAboutCommands(updater: updater)
            CommandGroup(replacing: .help) {
                Button("Igloo Help") { store.helpShown = true }
                    .keyboardShortcut("?", modifiers: [.command])
            }
            CommandGroup(after: .newItem) {
                Button("Analyse Projects") { store.analyse() }.keyboardShortcut("r", modifiers: [.command, .shift]).disabled(store.busy)
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
}
struct FrozenArchive: Decodable, Identifiable {
    var id: String; var name: String; var path: String; var state: String; var created: String
    var destination: String; var message: String; var archiveLocation: String; var checkouts: Int
    var paths: [String]?; var leftovers: [String]?
}
struct GitHubStatus: Decodable {
    var authenticated: Bool; var login: String; var message: String; var owners: [String]?
}

struct LifecycleControls: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text("Git setup & freezing").font(.headline)
            HStack {
                Button("Connect remote…") { store.remoteSetup = project }
                Button("Commit selected files…") { store.commitSetup = project }
                    .disabled(project.changes.isEmpty)
                Button("Freeze…") { store.freezeSetup = project }
                    .disabled(project.remotes.isEmpty || (project.path == store.source.path || store.source.path.hasPrefix(project.path + "/")))
            }.disabled(store.busy || !project.git)
            Text("Prepare a verified local archive and publish reviewed source, then remove checkouts in a separate step. Local folder names do not choose a GitHub owner.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}

struct RemoteSetupView: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    @LocalState var mode = "Existing repository"
    @LocalState var remote = "origin"
    @LocalState var url = ""
    @LocalState var owner = ""
    @LocalState var repository = ""
    @LocalState var login = ""
    @LocalState var accountStatus = "Checking GitHub CLI…"
    @LocalState var accepted = false
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Connect a remote").font(.title2).fontWeight(.semibold)
            Text(project.path).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            Picker("Repository", selection: $mode) {
                Text("Use existing repository").tag("Existing repository")
                Text("Create private GitHub repository").tag("New private repository")
            }.pickerStyle(.segmented)
            Form {
                TextField("Remote name", text: $remote)
                if mode == "Existing repository" {
                    TextField("Git remote URL", text: $url, prompt: Text("https://github.com/owner/repository.git"))
                } else {
                    TextField("GitHub owner", text: $owner, prompt: Text("Personal account or organization"))
                    TextField("Repository name", text: $repository)
                    Text(accountStatus).font(.caption).foregroundStyle(.secondary)
                    Text("Visibility: Private").font(.callout)
                }
            }
            Text("Choose the actual GitHub account or organization. The personal default comes from your gh login. Existing remotes are never overwritten, and connecting a remote does not push source.")
                .font(.callout).foregroundStyle(.secondary)
            Toggle(mode == "Existing repository" ? "Connect this exact remote URL." : "Create this private GitHub repository and connect it.", isOn: $accepted)
            HStack {
                Button("Cancel", role: .cancel) { store.remoteSetup = nil }
                Spacer()
                Button(mode == "Existing repository" ? "Add remote" : "Create private repository") {
                    let args = mode == "Existing repository"
                        ? ["remote-add", project.path, remote, url.trimmingCharacters(in: .whitespacesAndNewlines)]
                        : ["github-create", project.path, owner + "/" + repository, remote]
                    store.remoteSetup = nil
                    store.action(args)
                }.buttonStyle(.borderedProminent)
                    .disabled(store.busy || !accepted || remote.isEmpty ||
                              (mode == "Existing repository" ? url.isEmpty : owner.isEmpty || repository.isEmpty))
            }
        }.padding(24).frame(width: 720)
            .onAppear {
                repository = project.name
                remote = project.remotes.contains(where: { $0.name == "origin" }) ? "archive" : "origin"
                store.invoke(["github-status"], as: GitHubStatus.self) { result in
                    login = result.login
                    accountStatus = result.message + (result.login.isEmpty ? "" : " Signed in as " + result.login + ".")
                    if owner.isEmpty { owner = store.preferences.githubOwner.isEmpty ? result.login : store.preferences.githubOwner }
                }
            }
    }
}

struct CommitSetupView: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    @LocalState var checkout = ""
    @LocalState var selected = Set<String>()
    @LocalState var plan: CommitReviewPlan?
    @LocalState var message = ""
    @LocalState var accepted = false
    var checkouts: [String] { Array(Set(project.changes.map(\.checkout))).sorted() }
    var changes: [ChangedFile] { project.changes.filter { $0.checkout == checkout } }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(plan == nil ? "Select files to commit" : "Commit reviewed source").font(.title2).fontWeight(.semibold)
            if let review = plan {
                Text(review.path).font(.caption).textSelection(.enabled)
                List(review.files, id: \.self) { Text($0).font(.callout).textSelection(.enabled) }
                TextField("Commit message", text: $message)
                Text("Only this selection will be staged and committed. Environment, key and data filenames stay local. Git hooks and signing settings still apply. This action does not push.")
                    .font(.callout).foregroundStyle(.secondary)
                Toggle("I have reviewed these source changes for the commit.", isOn: $accepted)
                HStack {
                    Button("Back") { plan = nil; accepted = false }
                    Spacer()
                    Button("Commit selection") {
                        store.commitSetup = nil
                        store.action(["commit-execute", review.id, message])
                    }.buttonStyle(.borderedProminent).disabled(store.busy || !accepted || message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            } else {
                Picker("Checkout", selection: $checkout) {
                    ForEach(checkouts, id: \.self) { Text($0).tag($0) }
                }
                Table(changes, selection: $selected) {
                    TableColumn("Status", value: \.status).width(60)
                    TableColumn("Changed file", value: \.path)
                }
                Text("Select individual source files. Other staged files must be included in this review or unstaged first. Review file contents in your editor or Terminal before confirming.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Cancel", role: .cancel) { store.commitSetup = nil }
                    Button("Open Terminal") { store.terminal(checkout) }
                    Spacer()
                    Button("Review selection…") {
                        let files = changes.filter { selected.contains($0.id) }.map(\.path)
                        store.invoke(["commit-preview", checkout] + files, as: CommitReviewPlan.self) { plan = $0 }
                    }.buttonStyle(.borderedProminent).disabled(store.busy || selected.isEmpty)
                }
            }
        }.padding(24).frame(width: 820, height: 580)
            .onAppear { checkout = checkouts.first ?? project.path }
            .onChange(of: checkout) { selected = [] }
    }
}

struct FreezeSetupView: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    @LocalState var remote = ""
    @LocalState var reviewTopic = "Git history"
    @LocalState var plan: FreezeReviewPlan?
    @LocalState var publishAccepted = false
    @LocalState var localAccepted = false
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Freeze " + project.name).font(.title2).fontWeight(.semibold)
            if let review = plan {
                Text("Publish to: " + review.destination).font(.callout).textSelection(.enabled)
                Text("\(review.checkouts.count) checkouts · \(review.localCount) local entries · \(bytes(review.localSize)) local content, plus Git recovery").font(.callout)
                Picker("Review topic", selection: $reviewTopic) {
                    Text("Git history").tag("Git history")
                    Text("Keep locally").tag("Keep locally")
                    Text("Reinstall or rebuild").tag("Reinstall or rebuild")
                    Text("Checkouts").tag("Checkouts")
                }.labelsHidden().pickerStyle(.segmented)
                Group {
                    if reviewTopic == "Git history" {
                    List {
                        Section("Publish these ordinary branches and tags") {
                            ForEach(review.publish) { ref in
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(ref.ref).font(.callout)
                                    Text(ref.sha).font(.caption).textSelection(.enabled)
                                }
                            }
                        }
                        Section("Preserve privately in local Git recovery") {
                            ForEach(review.localRefs) { ref in
                                Text(ref.ref).font(.callout).textSelection(.enabled)
                            }
                        }
                    }
                    } else if reviewTopic == "Keep locally" {
                        List(review.localFiles, id: \.self) { Text($0).font(.caption).textSelection(.enabled) }
                    } else if reviewTopic == "Reinstall or rebuild" {
                        List(review.excluded, id: \.self) { Text($0).font(.caption).textSelection(.enabled) }
                    } else {
                        List(review.checkouts, id: \.self) { Text($0).font(.callout).textSelection(.enabled) }
                    }
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
                Text(review.message + "\nVault: " + review.archiveLocation).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                Toggle("I reviewed the committed history and publication destination.", isOn: $publishAccepted)
                Toggle("Keep the local-only archive and omit the listed rebuild folders.", isOn: $localAccepted)
                HStack {
                    Button("Cancel", role: .cancel) { store.freezeSetup = nil }
                    Spacer()
                    Button("Archive & push reviewed source") {
                        store.freezeSetup = nil
                        store.action(["freeze-execute", review.id])
                    }.buttonStyle(.borderedProminent).disabled(store.busy || !publishAccepted || !localAccepted)
                }
            } else {
                Text(project.path).font(.caption).textSelection(.enabled)
                Picker("Publish destination", selection: $remote) {
                    ForEach(project.remotes, id: \.name) { Text($0.name + " — " + $0.pushURL).tag($0.name) }
                }
                VStack(alignment: .leading, spacing: 12) {
                    Text("Prepare").font(.headline)
                    Text("Commit reviewed source changes first, then close project shells, agents and applications. Ignored local files can remain.")
                    Text("Archive & publish").font(.headline)
                    Text("Store local-only files, stashes, refs and Git metadata in a private local vault. Test reconstruction and verify every reviewed remote ref.")
                    Text("Finish freezing").font(.headline)
                    Text("After verification, the Frozen projects shelf offers checkout removal as a separate action. It leaves the archive and a small marker at the original project location.")
                }.font(.callout)
                Spacer()
                HStack {
                    Button("Cancel", role: .cancel) { store.freezeSetup = nil }
                    Spacer()
                    Button("Build freeze review…") {
                        store.invoke(["freeze-preview", project.path, remote], as: FreezeReviewPlan.self) { plan = $0 }
                    }.buttonStyle(.borderedProminent).disabled(store.busy || remote.isEmpty)
                }
            }
        }.padding(24).frame(width: 900, height: 690)
            .onAppear { remote = project.remotes.first(where: { $0.name == "origin" })?.name ?? project.remotes.first?.name ?? "" }
    }
}

func archiveStateLabel(_ state: String) -> String {
    ["ready": "Ready to freeze", "frozen": "Frozen", "retiring": "Freezing",
     "removing": "Freezing", "rehydrating": "Thawing", "rehydrated": "Thawed"][state] ?? state
}

struct FrozenProjectsView: View {
    @EnvironmentObject var store: OrganizerStore
    @LocalState var selected: String?
    @LocalState var removeConfirm = false
    @LocalState var restoreConfirm = false
    @LocalState var offline = false
    var archive: FrozenArchive? { store.snapshot?.archives?.first { $0.id == selected } }
    var recoverable: Bool { ["frozen", "retiring", "removing", "rehydrating"].contains(archive?.state ?? "") }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Frozen projects").font(.title2).fontWeight(.semibold)
            Table(store.snapshot?.archives ?? [], selection: $selected) {
                TableColumn("Project", value: \.name).width(min: 170)
                TableColumn("State") { Text(archiveStateLabel($0.state)) }.width(120)
                TableColumn("Created") { Text(shortDate($0.created)) }.width(100)
                TableColumn("Git destination") { Text($0.destination).lineLimit(1).truncationMode(.middle).help($0.destination) }.width(min: 250)
            }
            if let record = archive {
                Text(record.message).font(.callout)
                Text(record.path).font(.caption).textSelection(.enabled)
                if let paths = record.paths, paths.count > 1 {
                    Text("\(paths.count) checkouts:\n" + paths.joined(separator: "\n"))
                        .font(.caption).textSelection(.enabled).lineLimit(5)
                }
            } else {
                Text("Verified archives appear here. A ready archive still has its checkouts; a frozen project has only its marker and local vault copy.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            Text("Thawing restores the frozen source state and local files. Reinstall dependencies afterward, then pull newer changes when ready. Archives remain available after restoration.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Done") { store.vaultShown = false }.keyboardShortcut(.cancelAction)
                Button("Show archive") { if let a = archive { store.reveal(a.archiveLocation) } }.disabled(archive == nil)
                if let leftovers = archive?.leftovers, !leftovers.isEmpty {
                    Button("Show recovery copies") {
                        NSWorkspace.shared.activateFileViewerSelecting(leftovers.map { URL(fileURLWithPath: $0) })
                    }
                }
                Spacer()
                Button("Remove verified checkouts…") { removeConfirm = true }
                    .disabled(store.busy || archive?.state != "ready")
                Button("Offline recovery…") { offline = true; restoreConfirm = true }
                    .disabled(store.busy || !recoverable)
                Button("Thaw from remote…") { offline = false; restoreConfirm = true }
                    .buttonStyle(.borderedProminent).disabled(store.busy || !recoverable)
            }
        }.padding(24).frame(width: 1080, height: 650)
            .confirmationDialog("Remove the verified source checkouts?", isPresented: $removeConfirm, titleVisibility: .visible) {
                Button("Finish freezing", role: .destructive) {
                    if let a = archive { store.action(["freeze-retire", a.id]) }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text((archive?.path ?? "") + "\nAll registered checkouts shown in this archive will be removed after recovery is verified again. The local-only archive and Git recovery remain. Thaw restores the project.")
            }
            .confirmationDialog(offline ? "Restore from the verified local archive?" : "Clone the remote and restore the frozen project?", isPresented: $restoreConfirm, titleVisibility: .visible) {
                Button(offline ? "Restore offline" : "Thaw") {
                    if let a = archive { store.action(["rehydrate", a.id] + (offline ? ["offline"] : [])) }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text((archive?.path ?? "") + "\nExisting files will never be overwritten. The original branches, worktrees and local settings will return.")
            }
    }
}


struct AppHelpView: View {
    @EnvironmentObject var store: OrganizerStore
    @LocalState var topic = "Getting started"
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
                        helpSection("Analyse your projects", "Analyse Projects discovers repositories and linked worktrees, measures folders and checks configured remotes. It reads project metadata and filenames without changing checkouts. Source dates and observed process activity are estimates of use.")
                        helpSection("Settings and installation", "The app bundles its backend and can be copied to Applications. Settings lets you choose the workspace root, archive and state locations, clone folder, quiet threshold, GitHub owner default and installed tool paths. Python, Git and gh remain local prerequisites.")
                        helpSection("Tasks, CLI and schedules", "Tasks saves reusable rules, project scopes, cache types and per-run limits. Plan-only is the default. Plan now scans afresh and records an exact plan; cleanup tasks can apply a reviewed cache plan. Install CLI adds ~/.local/bin/igloo with create, plan, run, apply, history, schedule and unschedule commands. Scheduled cache execution requires the saved unattended permission. Changing rules invalidates a schedule until it is re-enabled. Tasks never commit, push, freeze or remove project checkouts. Past the freeze threshold, projects are held for wizard review. Daily and weekly schedules use user LaunchAgents, local time, and only run while logged in. Busy runs skip and are recorded; source code is not required after CLI installation.")
                        helpSection("Cleanup wizard", "Choose Git activity or source-file modification age, a cache threshold and a later freeze threshold. Recent projects are kept, middle-aged projects get cache cleanup, and older projects get freezing instead. Change any project action to override age suggestions. Running-process checks always remain. Prepare and review the exact folders, refs and recovery data, then accept once to run all ready actions, including verified checkout removal for full freezes. Blockers and changed state stop individual rows; results are saved locally. Source-file age ignores dependencies, Git metadata and generated/review folders.")
                        helpSection("Explore space", "Space explorer offers sunburst, icicle and Marimekko views of project sizes and measured dependency/build types. Filter projects by age, detected activity or type, and color by type, activity or age. Select a segment to focus a project; Open project returns to its cleanup and preservation tools. Other project files is the unclassified remainder, not a claim that data is safe to delete.")
                        helpSection("Browse GitHub", "GitHub repositories lets you browse your account or an organization, search, hide existing local clones, select several repositories and clone them into a chosen project folder. Existing paths are never overwritten. Run Analyse Projects afterward to index them.")
                        helpSection("Project types and technology", "Analysis suggests project purpose separately from technology using bounded filenames and package metadata. Overview lets you correct the project type or reset it to Automatic. Catalog search and the type filter use these labels. Space explorer groups, colors and filters by project purpose, technology, age and activity. Complete technology combinations keep aggregate sizes from being counted more than once.")
                        helpSection("Current scope", "Show scope / Hide scope at the bottom-right toggles the visualization beside your work (Option-Command-I). It follows your current filters or the selected project. Choose a breakdown or open the full Space Explorer with this scope. Narrow windows retain the icon rail; the navigation button opens full labels in a popover when there is not room to expand.")
                        helpSection("Choose what to review", "The sidebar groups project views, review filters, tools and recovery. Its toolbar button collapses it to an icon rail. Quiet projects shows its age threshold below the project views. Needs preservation highlights local history, changed files, stashes and local data. Cleanup opportunities lists ignored dependencies and tool caches.")
                        helpSection("Connect GitHub", "Open a project’s Plan tab. Connect an existing remote, or create a private GitHub repository. Your gh login supplies the personal-account default; you can explicitly choose an organization. Local folder labels never choose the GitHub owner or visibility.")
                        helpSection("Commit and publish", "Commit selected files lets you review a specific checkout and file selection. Other staged files must be reviewed too. Environment, key and data filenames stay local. Git → Branches can push one reviewed commit and destination; it never force-pushes.")
                        helpSection("Clean caches", "Select exact directories in Cleanup and review a fresh preview. Active sessions, tracked files, nested repositories and local configuration can stop cleanup. Moves go to Trash and can be restored from Trash & restore history. Space is freed when Trash is emptied separately.")
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(18)
                } else if topic == "Freeze & thaw" {

                    VStack(alignment: .leading, spacing: 18) {
                        helpSection("Prepare a project", "Commit reviewed source changes, then close its shells, agents and applications. Ignored local files such as .env files and databases can remain. All registered worktrees are reviewed together.")
                        helpSection("Archive & push", "Freeze builds a review of ordinary branches and tags, local-only files, and rebuild folders. After confirmation it stores local files and full Git recovery in a private-permission local vault, tests reconstruction, pushes only the reviewed refs and verifies them remotely. Private/archive refs, stashes and reflogs stay in local recovery.")
                        helpSection("Finish freezing", "A ready archive appears in Frozen projects. Remove verified checkouts is a separate confirmation. The app verifies recovery again, rechecks current files, then removes the reviewed checkouts. A small project marker and the local vault remain.")
                        helpSection("Thaw later", "Frozen projects offers remote thawing and offline recovery. Remote thawing clones the remote and restores the frozen source state, local files and Git/worktree metadata. It does not overwrite occupied paths. Reinstall dependencies, then pull newer remote changes when ready.")
                        helpSection("Recover interruptions", "If removal or restoration is interrupted, use the same archive’s recovery action again. Already restored files are checked before continuing. Possible post-interruption edits or recovery copies are preserved for review.")
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(18)
                } else if topic == "Safety & storage" {

                    VStack(alignment: .leading, spacing: 18) {
                        helpSection("Local archive storage", "The archive location is configurable in Settings; the default is ~/.igloo/vault. They contain local-only data and Git metadata and are never uploaded. Files have private owner permissions but are not encrypted. Keep a separate secure backup of this vault if you need protection against loss of this Mac.")
                        helpSection("App state", "Runtime state is stored in the configured state folder, normally ~/.igloo. Preferences live in ~/.config/igloo/config.json. Keep the Trash journal while you may need restoration. The app’s own source can use Git setup and commits; it is protected from cache cleanup and freezing.")
                        helpSection("What is omitted", "Only reviewed ignored dependency and rebuild folders without tracked content are omitted. Recognized local configuration inside those folders is kept. Custom dependency edits need your review. External symlink targets remain external; the archive preserves the link, not its target.")
                        helpSection("What checks mean", "Unverified branch coverage may need Fetch & verify to download current remote metadata. Fetch does not switch branches or prune refs. Hash checks and recovery tests protect against changed or corrupt archives; they do not certify that committed history is free of private data.")
                        helpSection("When an action stops", "Leave source and archive copies in place. Resolve the reported active process, changed files, remote access or destination conflict, then create a fresh review or resume recovery. The app never empties Trash or force-pushes.")
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(18)
                }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
            HStack {
                Spacer()
                Button("Done") { store.helpShown = false }.keyboardShortcut(.cancelAction)
            }
        }.padding(24).frame(width: 860, height: 700)
    }
    private func helpSection(_ title: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.headline)
            Text(text).font(.callout).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        }
    }
}
