import SwiftUI
import AppKit

struct PlanView: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    @Binding var hold: Bool
    @Binding var note: String
    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
            VStack(alignment: .leading, spacing: 14) {
            LifecycleControls(project: project)
            Divider()
            Toggle("Hold", isOn: $hold)
            Text("The wizard and tasks never propose cleanup or freezing for this project.").font(.callout).foregroundStyle(.secondary)
            Text("Preservation notes").font(.headline)
            TextEditor(text: $note).font(.body).frame(minHeight: 140).border(Color.secondary.opacity(0.25))
                .accessibilityLabel("Preservation notes")
            Text("Before removing a whole checkout: preserve local changes and stashes, review branch and tag history, archive important ignored files, then verify the backup. Keep active sessions intact.")
                .font(.callout).foregroundStyle(.secondary)
            }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            HStack {
                Button("Export project report…") { store.export(project) }
                Spacer()
                Button("Save") {
                    store.invoke(["hold", project.id, hold ? "true" : "false"], as: Reply.self) { _ in
                        store.action(["note", project.id, note])
                    }
                }
                    .buttonStyle(.borderedProminent).disabled(store.busy)
            }.padding(12).background(.bar)
        }
    }
}

struct LifecycleControls: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    @LocalState var selectedMode: PreservationMode?
    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text("Git setup & freezing").font(.headline)
            HStack {
                Button("Connect remote…") { store.remoteSetup = project }
                Button("Commit selected files…") { store.commitSetup = project }
                    .disabled(project.changes.isEmpty)
                Button("Preserve…") { selectedMode = .preserve }
                    .help(project.git ? "Preserve this project and keep its checkout." : "Igloo can preserve Git projects only.")
                Button("Freeze…") { selectedMode = .freeze }
                    .help(project.git ? "Freeze this project after verification." : "Igloo can preserve Git projects only.")
            }.disabled(store.busy || !project.git)
            Text(store.busy ? "Working…" : (project.git ? "Preserve keeps the checkout. Freeze removes it after verification." : "Igloo can preserve Git projects only."))
                .font(.caption).foregroundStyle(.secondary)
        }.sheet(item: $selectedMode) { selected in
            FreezeSetupView(project: project, initialMode: selected.rawValue, onClose: { selectedMode = nil }).environmentObject(store)
        }
    }
}

enum PreservationMode: String, Identifiable {
    case preserve, freeze
    var id: String { rawValue }
}

struct RemoteSetupView: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    var onClose: (() -> Void)? = nil
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
                Button("Cancel", role: .cancel) { if let onClose { onClose() } else { store.remoteSetup = nil } }
                Spacer()
                Button(mode == "Existing repository" ? "Add remote" : "Create private repository") {
                    let args = mode == "Existing repository"
                        ? ["remote-add", project.path, remote, url.trimmingCharacters(in: .whitespacesAndNewlines)]
                        : ["github-create", project.path, owner + "/" + repository, remote]
                    if let onClose { onClose() } else { store.remoteSetup = nil }
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
    var onClose: (() -> Void)? = nil
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
                        if let onClose { onClose() } else { store.commitSetup = nil }
                        store.action(["commit-execute", review.id, message])
                    }.buttonStyle(.borderedProminent).disabled(store.busy || !accepted || message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            } else {
                if checkouts.isEmpty {
                    Text("There are no changed files to commit. For an empty repository, add a file and create an initial commit in Terminal, then return and refresh.")
                        .font(.callout).foregroundStyle(.secondary)
                }
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
                    Button("Cancel", role: .cancel) { if let onClose { onClose() } else { store.commitSetup = nil } }
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

struct FreezeBlockerReply: Decodable { var blockers: [FreezeBlocker] }
struct PreservationReview: Decodable {
    var id: String; var mode: String; var held: Bool; var destination: String
    var publish: [PublishRef]; var localRefs: [PublishRef]; var checkouts: [String]
    var localCount: Int; var localSize: Int64; var localFiles: [String]; var excluded: [String]
    var archiveLocation: String; var message: String; var outcome: ReviewOutcome
}
struct PreservationResult: Decodable {
    var state: String; var message: String; var outcome: ReviewOutcome
}
struct DemoPreservationCatalog: Decodable {
    struct DemoProject: Decodable {
        var id: String; var blockers: [FreezeBlocker]; var hold: Bool
        private enum CodingKeys: String, CodingKey { case id, blockers, hold }
        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            id = try values.decode(String.self, forKey: .id)
            blockers = try values.decodeIfPresent([FreezeBlocker].self, forKey: .blockers) ?? []
            hold = try values.decodeIfPresent(Bool.self, forKey: .hold) ?? false
        }
    }
    struct Outcomes: Decodable { var preserve: ReviewOutcome; var freeze: ReviewOutcome }
    var projects: [DemoProject]; var reviewOutcomes: Outcomes
    static func load() -> DemoPreservationCatalog? {
        guard let url = Bundle.main.url(forResource: "demo-catalog", withExtension: "json"),
              let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(Self.self, from: data)
    }
}

struct FreezeSetupView: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    var initialMode = "freeze"
    var onClose: (() -> Void)? = nil
    var captureStage: String? = nil
    var captureBlocker: String? = nil
    @LocalState var mode = "freeze"
    @LocalState var remote = ""
    @LocalState var reviewTopic = "Git history"
    @LocalState var plan: PreservationReview?
    @LocalState var result: PreservationResult?
    @LocalState var blockers: [FreezeBlocker]?
    @LocalState var fixingRemote = false
    @LocalState var fixingCommit = false
    @LocalState var refreshPending = false
    var currentProject: Project { store.snapshot?.projects.first(where: { $0.id == project.id }) ?? project }
    var shownBlockers: [FreezeBlocker]? {
        let values = blockers ?? (store.isDemo ? DemoPreservationCatalog.load()?.projects.first(where: { $0.id == project.id })?.blockers : nil)
        guard let captureBlocker, let values else { return values }
        return values.filter { $0.code == captureBlocker }
    }
    func close() { if let onClose { onClose() } else { store.freezeSetup = nil } }
    func refreshBlockers() {
        guard !store.busy else { refreshPending = true; return }
        refreshPending = false
        blockers = nil
        let remotes = currentProject.remotes
        if !remotes.contains(where: { $0.name == remote }) { remote = remotes.first(where: { $0.name == "origin" })?.name ?? remotes.first?.name ?? "" }
        if store.isDemo {
            blockers = DemoPreservationCatalog.load()?.projects.first(where: { $0.id == project.id })?.blockers
            return
        }
        store.invoke(["freeze-blockers", project.path], as: FreezeBlockerReply.self) { blockers = $0.blockers }
    }
    func demoReview(mode selectedMode: String, remote selectedRemote: String) -> PreservationReview? {
        guard let catalog = DemoPreservationCatalog.load() else { return nil }
        let destination = currentProject.remotes.first(where: { $0.name == selectedRemote })?.pushURL
            ?? currentProject.remotes.first?.pushURL ?? "Example remote"
        var demoOutcome = selectedMode == "freeze" ? catalog.reviewOutcomes.freeze : catalog.reviewOutcomes.preserve
        let checkoutPaths = currentProject.worktrees.map(\.path)
        demoOutcome.checkoutsRemoved = ReviewOutcome.Checkouts(count: selectedMode == "freeze" ? checkoutPaths.count : 0,
                                                                paths: selectedMode == "freeze" ? checkoutPaths : [])
        demoOutcome.refsPushed = currentProject.branches.map {
            ReviewOutcome.PushedRef(ref: $0.name, sha: $0.sha, destination: destination)
        }
        return PreservationReview(id: "demo-review", mode: selectedMode,
            held: catalog.projects.first(where: { $0.id == project.id })?.hold ?? false,
            destination: destination,
            publish: currentProject.branches.map { PublishRef(ref: $0.name, sha: $0.sha) },
            localRefs: [], checkouts: checkoutPaths,
            localCount: currentProject.localFiles.count, localSize: currentProject.localFiles.reduce(0) { $0 + $1.size },
            localFiles: currentProject.localFiles.map(\.path), excluded: [],
            archiveLocation: "/Users/demo/Project Recovery", message: "Fictional demo review; no project files will change.",
            outcome: demoOutcome)
    }
    var captureReview: PreservationReview? {
        guard store.isDemo, let captureStage, ["review", "result", "failure"].contains(captureStage) else { return nil }
        return demoReview(mode: initialMode, remote: remote)
    }
    var captureResult: PreservationResult? {
        guard let captureStage, ["result", "failure"].contains(captureStage), let review = captureReview else { return nil }
        var outcome = review.outcome
        outcome.observed = true; outcome.bytesFreed = 0
        let preserved = captureStage == "failure" || initialMode == "preserve"
        if preserved { outcome.checkoutsRemoved = ReviewOutcome.Checkouts(count: 0, paths: []) }
        if captureStage == "failure" {
            outcome.held = [ReviewOutcome.HeldItem(path: project.path, reason: "Verification stopped.")]
        }
        return PreservationResult(state: preserved ? "ready" : "frozen",
            message: captureStage == "failure" ? "Demo only: Freeze stopped; the project is Preserved and its checkouts are still in place." : "Demo only: no files were changed.",
            outcome: outcome)
    }
    func buildReview() {
        if store.isDemo { plan = demoReview(mode: mode, remote: remote); return }
        store.invoke(["freeze-preview", project.path, remote, mode], as: PreservationReview.self) { plan = $0 }
    }
    func executeReview(_ review: PreservationReview) {
        guard store.isDemo else {
            store.invoke(["freeze-execute", review.id, review.mode], as: PreservationResult.self) { result = $0 }
            return
        }
        let state = review.mode == "freeze" ? "frozen" : "ready"
        let message = "Demo only: no files were changed."
        let record = FrozenArchive(id: "demo-result-" + project.id, name: project.name, path: project.path,
                                   state: state, created: "2026-10-01T10:00:00+00:00", destination: review.destination,
                                   message: message, archiveLocation: review.archiveLocation,
                                   checkouts: review.checkouts.count, paths: review.checkouts, leftovers: [])
        if var snapshot = store.snapshot {
            var archives = snapshot.archives ?? []
            archives.removeAll { $0.id == record.id }
            archives.append(record); snapshot.archives = archives; store.snapshot = snapshot
        }
        result = PreservationResult(state: state, message: message, outcome: review.outcome)
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(((captureStage == nil ? mode : initialMode) == "freeze" ? "Freeze " : "Preserve ") + project.name).font(.title2).fontWeight(.semibold)
            if let result = result ?? captureResult {
                if result.message.localizedCaseInsensitiveContains("Freeze stopped") {
                    Text("Freeze stopped").font(.headline).foregroundStyle(.orange)
                    Text("Freeze stopped. The project is Preserved and its checkouts are still in place.")
                        .font(.callout).foregroundStyle(.orange)
                    Text("Next: review the recovery history before trying Freeze again.")
                        .font(.callout).foregroundStyle(.orange)
                } else {
                    Text(archiveStateLabel(result.state)).font(.headline)
                    Text(result.message).font(.callout)
                }
                ScrollView { OutcomeSummaryView(outcome: result.outcome) }.frame(maxHeight: 220)
                HStack { Spacer(); Button("Done") { close(); if !store.isDemo { store.load() } }.keyboardShortcut(.cancelAction) }
            } else if let review = plan ?? captureReview {
                if review.held { Text("This project is on Hold").foregroundStyle(.orange) }
                Text("Publish to: " + review.destination).font(.callout).textSelection(.enabled)
                Picker("Review topic", selection: $reviewTopic) {
                    Text("Git history").tag("Git history"); Text("Keep locally").tag("Keep locally")
                    Text("Reinstall or rebuild").tag("Reinstall or rebuild"); Text("Checkouts").tag("Checkouts")
                }.labelsHidden().pickerStyle(.segmented)
                Group {
                    if reviewTopic == "Git history" {
                        List {
                            Section("Publish these ordinary branches and tags") {
                                ForEach(review.publish) { ref in Text(ref.ref + "\n" + ref.sha).textSelection(.enabled) }
                            }
                            if !review.localRefs.isEmpty {
                                Section("Preserve privately in local Git recovery") {
                                    ForEach(review.localRefs) { ref in Text(ref.ref).textSelection(.enabled) }
                                }
                            }
                        }
                    } else if reviewTopic == "Keep locally" {
                        List(review.localFiles, id: \.self) { Text($0).textSelection(.enabled) }
                    } else if reviewTopic == "Reinstall or rebuild" {
                        List(review.excluded, id: \.self) { Text($0).textSelection(.enabled) }
                    } else {
                        List(review.checkouts, id: \.self) { Text($0).textSelection(.enabled) }
                    }
                }.font(.caption).frame(maxWidth: .infinity, maxHeight: .infinity)
                ScrollView { OutcomeSummaryView(outcome: review.outcome) }.frame(maxHeight: 220)
                Text(review.message).font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Back") { plan = nil; refreshBlockers() }.disabled(store.busy)
                    Spacer()
                    Button(review.outcome.actionLabel(preservedProjects: review.mode == "preserve" ? 1 : nil)) {
                        executeReview(review)
                    }.buttonStyle(.borderedProminent).disabled(store.busy)
                }
            } else {
                Picker("Action", selection: $mode) { Text("Preserve").tag("preserve"); Text("Freeze").tag("freeze") }.pickerStyle(.segmented)
                Text(project.path).font(.caption).textSelection(.enabled)
                if currentProject.hold == true { Text("This project is on Hold").foregroundStyle(.orange) }
                Text("Requirements").font(.headline).accessibilityAddTraits(.isHeader)
                if let blockers = shownBlockers {
                    if blockers.isEmpty { Text("No blockers found.") }
                    ForEach(blockers) { blocker in
                        HStack(alignment: .top) {
                            Text(blocker.message).frame(maxWidth: .infinity, alignment: .leading)
                                .accessibilityElement(children: .combine)
                                .accessibilityLabel(Text(blocker.message))
                            if blocker.code == "no-files" {
                                Button("Open in Finder") { NSWorkspace.shared.open(URL(fileURLWithPath: project.path)) }.disabled(store.isDemo)
                                Button("Open Terminal") { store.terminal(project.path) }.disabled(store.isDemo)
                            }
                            if blocker.fix == "connect-remote" { Button("Connect remote…") { fixingRemote = true }.disabled(store.isDemo) }
                            if blocker.fix == "commit" { Button("Commit selected files…") { fixingCommit = true }.disabled(store.isDemo) }
                        }.font(.callout)
                            .accessibilityElement(children: .contain)
                            .accessibilityLabel(Text("Requirement: " + blocker.message))
                    }
                } else if store.busy {
                    Text("Checking requirements…").foregroundStyle(.secondary)
                } else {
                    HStack { Text("Requirements could not be checked."); Button("Try again") { refreshBlockers() } }
                }
                if !currentProject.remotes.isEmpty {
                    Picker("Publish destination", selection: $remote) {
                        ForEach(currentProject.remotes, id: \.name) { Text($0.name + " — " + $0.pushURL).tag($0.name) }
                    }
                }
                Spacer()
                HStack {
                    Button("Cancel", role: .cancel) { close() }
                    Spacer()
                    Button("Build review…") {
                        buildReview()
                    }.buttonStyle(.borderedProminent).disabled(store.busy || shownBlockers == nil || !(shownBlockers?.isEmpty ?? false) || remote.isEmpty)
                }
            }
        }.padding(24).frame(width: sheetWidth(900), height: 690)
            .onAppear {
                mode = initialMode; refreshBlockers()
            }
            .onChange(of: store.busy) { if !store.busy && refreshPending { refreshBlockers() } }
            .onChange(of: currentProject.remotes) {
                if !currentProject.remotes.contains(where: { $0.name == remote }) {
                    remote = currentProject.remotes.first(where: { $0.name == "origin" })?.name ?? currentProject.remotes.first?.name ?? ""
                }
            }
            .sheet(isPresented: $fixingRemote, onDismiss: { refreshPending = true; if !store.busy { refreshBlockers() } }) {
                RemoteSetupView(project: currentProject, onClose: { fixingRemote = false }).environmentObject(store)
            }
            .sheet(isPresented: $fixingCommit, onDismiss: { refreshPending = true; if !store.busy { refreshBlockers() } }) {
                CommitSetupView(project: currentProject, onClose: { fixingCommit = false }).environmentObject(store)
            }
    }
}

func archiveStateLabel(_ state: String) -> String {
    ["ready": "Preserved", "frozen": "Frozen", "retiring": "Freezing",
     "removing": "Freezing", "rehydrating": "Thawing", "rehydrated": "Thawed"][state] ?? state
}

struct FreezerBackupRow: Decodable, Identifiable {
    var id: String; var copyID: String; var backedUp: Bool; var lastVerified: String?
    var missing: Bool; var status: String; var reason: String
    var description: String {
        if backedUp { return "Backed up · verified " + (lastVerified.map(shortDate) ?? "date unknown") }
        let detail = reason.lowercased()
        if detail.contains("changed") || detail.contains("does not match") { return "Backup copy changed" }
        return "Not backed up: " + (reason.isEmpty ? "No verified copy" : reason)
    }
}
struct FreezerBackupStatus: Decodable {
    var configured: Bool; var available: Bool; var reason: String
    var archives: [FreezerBackupRow]; var orphans: [FreezerBackupRow]
    var cloud: Bool; var acceptanceRoot: String; var cloudAccepted: Bool
}

struct FreezerBackupEnvelope: Decodable { var backup: FreezerBackupStatus }
struct DemoBackupEnvelope: Decodable { var backupStatus: FreezerBackupStatus }

struct FrozenProjectsView: View {
    @EnvironmentObject var store: OrganizerStore
    @LocalState var selected: String?
    @LocalState var removeConfirm = false
    @LocalState var restoreConfirm = false
    @LocalState var offline = false
    @LocalState var backup: FreezerBackupStatus?
    @LocalState var removeBackup: FreezerBackupRow?
    @LocalState var confirmBackupRemove = false
    @LocalState var refreshBackupWhenIdle = false
    var archive: FrozenArchive? { store.snapshot?.archives?.first { $0.id == selected } }
    var shownBackup: FreezerBackupStatus? {
        if let backup { return backup }
        guard store.isDemo, let url = Bundle.main.url(forResource: "demo-catalog", withExtension: "json"),
              let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(DemoBackupEnvelope.self, from: data).backupStatus
    }
    var recoverable: Bool { ["frozen", "retiring", "removing", "rehydrating"].contains(archive?.state ?? "") }
    private func loadBackup() {
        guard !store.busy else { refreshBackupWhenIdle = true; return }
        refreshBackupWhenIdle = false
        if store.isDemo {
            if let url = Bundle.main.url(forResource: "demo-catalog", withExtension: "json"),
               let data = try? Data(contentsOf: url),
               let fixture = try? JSONDecoder().decode(DemoBackupEnvelope.self, from: data) {
                backup = fixture.backupStatus
            }
            return
        }
        store.invoke(["vault-list"], as: FreezerBackupEnvelope.self) { backup = $0.backup }
    }
    private func runBackup(_ command: String) {
        refreshBackupWhenIdle = true
        store.invoke([command], as: FreezerBackupStatus.self) { backup = $0 }
    }
    @ViewBuilder private func archiveTable(_ archives: [FrozenArchive]) -> some View {
        if sheetWidth(1080) < 850 {
            Table(archives, selection: $selected) {
                TableColumn("Project", value: \.name).width(min: 140)
                TableColumn("State") { Text(archiveStateLabel($0.state)) }.width(90)
                TableColumn("Backup") { archive in
                    Text(shownBackup?.archives.first(where: { $0.id == archive.id })?.description ?? "Backup status unavailable")
                        .lineLimit(1).help(shownBackup?.archives.first(where: { $0.id == archive.id })?.reason ?? "")
                }.width(min: 150)
            }
        } else {
            Table(archives, selection: $selected) {
                TableColumn("Project", value: \.name).width(min: 170)
                TableColumn("State") { Text(archiveStateLabel($0.state)) }.width(120)
                TableColumn("Backup") { archive in
                    Text(shownBackup?.archives.first(where: { $0.id == archive.id })?.description ?? "Backup status unavailable")
                        .lineLimit(1).help(shownBackup?.archives.first(where: { $0.id == archive.id })?.reason ?? "")
                }.width(min: 170)
                TableColumn("Created") { Text(shortDate($0.created)) }.width(100)
                TableColumn("Git destination") { Text($0.destination).lineLimit(1).truncationMode(.middle).help($0.destination) }.width(min: 250)
            }
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Freezer").font(.title2).fontWeight(.semibold)
            Text("Preserved").font(.headline)
            archiveTable((store.snapshot?.archives ?? []).filter { $0.state == "ready" })
            Text("Frozen").font(.headline)
            archiveTable((store.snapshot?.archives ?? []).filter { $0.state == "frozen" })
            if !(store.snapshot?.archives ?? []).filter({ !["ready", "frozen"].contains($0.state) }).isEmpty {
                Text("Other states").font(.headline)
                archiveTable((store.snapshot?.archives ?? []).filter { !["ready", "frozen"].contains($0.state) })
            }
            if let record = archive {
                Text(record.message).font(.callout)
                Text(record.path).font(.caption).textSelection(.enabled)
                if let paths = record.paths, paths.count > 1 {
                    Text("\(paths.count) checkouts:\n" + paths.joined(separator: "\n"))
                        .font(.caption).textSelection(.enabled).lineLimit(5)
                }
            } else {
                Text("Verified archives appear here. A preserved project still has its checkouts; a frozen project has only its marker and local Freezer copy.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            Text("Thawing restores the frozen source state and local files. Reinstall dependencies afterward, then pull newer changes when ready. Archives remain available after restoration.")
                .font(.caption).foregroundStyle(.secondary)
            if let backup = shownBackup, !backup.orphans.isEmpty {
                Text("No longer in the Freezer").font(.headline)
                ScrollView {
                    ForEach(backup.orphans) { copy in
                        HStack {
                            Text("Copy " + String(copy.copyID.prefix(8)) + " · " + copy.description)
                            Spacer()
                            Button("Remove from backup…") { removeBackup = copy; confirmBackupRemove = true }
                                .disabled(store.busy || store.isDemo)
                                .accessibilityLabel(Text("Remove backup copy " + copy.copyID))
                        }.font(.caption)
                    }
                }.frame(maxHeight: 90)
            }
            HStack {
                Button("Back up now") { runBackup("backup-run") }.disabled(store.busy || store.isDemo || shownBackup?.configured != true)
                Button("Verify backup") { runBackup("backup-verify") }.disabled(store.busy || store.isDemo || shownBackup?.configured != true)
            }
            HStack {
                Button("Done") { store.vaultShown = false }.keyboardShortcut(.cancelAction)
                Button("Show archive") { if let a = archive { store.reveal(a.archiveLocation) } }.disabled(archive == nil)
                if let leftovers = archive?.leftovers, !leftovers.isEmpty {
                    Button("Show recovery copies") {
                        NSWorkspace.shared.activateFileViewerSelecting(leftovers.map { URL(fileURLWithPath: $0) })
                    }
                }
                Spacer()
                Button("Freeze…") { removeConfirm = true }
                    .disabled(store.busy || store.isDemo || archive?.state != "ready")
                Button("Offline recovery…") { offline = true; restoreConfirm = true }
                    .disabled(store.busy || store.isDemo || !recoverable)
                Button("Thaw from remote…") { offline = false; restoreConfirm = true }
                    .buttonStyle(.borderedProminent).disabled(store.busy || store.isDemo || !recoverable)
            }
        }.padding(24).frame(width: sheetWidth(1080), height: 650)
            .onAppear { loadBackup() }
            .onChange(of: store.busy) { if !store.busy && refreshBackupWhenIdle { loadBackup() } }
            .confirmationDialog("Remove this copy from the backup?", isPresented: $confirmBackupRemove, titleVisibility: .visible) {
                Button("Remove from backup", role: .destructive) {
                    guard let copy = removeBackup, !store.busy else { return }
                    removeBackup = nil; refreshBackupWhenIdle = true
                    store.invoke(["backup-remove", copy.copyID], as: Reply.self) { _ in }
                }.disabled(store.busy)
                Button("Cancel", role: .cancel) { removeBackup = nil }
            } message: { Text("This removes backup copy " + String((removeBackup?.copyID ?? "").prefix(8)) + ". It cannot be undone.") }
            .confirmationDialog("Remove the verified source checkouts?", isPresented: $removeConfirm, titleVisibility: .visible) {
                Button("Freeze", role: .destructive) {
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


