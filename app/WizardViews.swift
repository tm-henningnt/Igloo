import SwiftUI

struct WizardRules: Codable, Equatable {
    var cacheDays = 30
    var freezeDays = 180
    var ignoreActive = true
    var ageBasis = "git"
    var cacheKinds: [String] = []
}
struct WizardChoice: Encodable {
    var path: String
    var action: String
    var remote: String
}
struct WizardProposal: Decodable, Identifiable {
    var id: String; var path: String; var name: String; var ageDays: Int?
    var action: String; var reason: String; var active: Bool; var activityKnown: Bool
    var size: Int64; var cacheSize: Int64; var cacheCount: Int
    var changedCount: Int; var localCount: Int; var branchCount: Int; var stashCount: Int
    var remotes: [Remote]; var remote: String
}
struct WizardScan: Decodable {
    var rules: WizardRules; var generated: String; var rows: [WizardProposal]; var scanIssues: Int
}
struct WizardRequest: Encodable {
    var rules: WizardRules; var choices: [WizardChoice]
}
struct WizardRow: Decodable, Identifiable {
    var id: String; var path: String; var name: String; var action: String; var status: String; var message: String
    var cleanup: CleanupPlan?; var freeze: FreezeReviewPlan?
    var archiveID: String?; var results: [ActionResult]?
}
struct WizardPlan: Decodable, Identifiable {
    var id: String; var created: String; var root: String; var vault: String; var rules: WizardRules
    var status: String; var rows: [WizardRow]; var message: String; var outcome: ReviewOutcome?
}
struct WizardState: Decodable {
    var rules: WizardRules; var cacheKinds: [String]; var last: WizardPlan?
}

struct CleanupWizardView: View {
    @EnvironmentObject var store: OrganizerStore
    var captureReview = false
    @LocalState var rules = WizardRules()
    @LocalState var kinds: [String] = []
    @LocalState var proposals: [WizardProposal] = []
    @LocalState var phase = 0
    @LocalState var plan: WizardPlan?
    @LocalState var last: WizardPlan?
    @LocalState var selected: String?
    @LocalState var approved = false
    @LocalState var scanIssues = 0
    @LocalState var taskEditorShown = false
    @LocalState var reviewTopic = "Summary"
    let actions = ["keep", "cleanup", "archive", "freeze"]
    init(captureReview: Bool = false) {
        self.captureReview = captureReview
        let example: WizardPlan? = captureReview ? DemoCatalog.reply(["wizard-state"], as: WizardState.self)?.last : nil
        _phase = LocalState(initialValue: captureReview ? 2 : 0)
        _plan = LocalState(initialValue: example)
        _selected = LocalState(initialValue: example?.rows.first?.id)
    }
    func actionName(_ action: String) -> String {
        switch action {
        case "cleanup": return "Clean caches"
        case "archive": return "Preserve"
        case "freeze": return "Freeze"
        default: return "Leave unchanged"
        }
    }
    func encoded<T: Encodable>(_ value: T) -> String {
        String(data: try! JSONEncoder().encode(value), encoding: .utf8)!
    }
    var reviewRow: WizardRow? { plan?.rows.first { $0.id == selected } }
    var ready: Int { plan?.rows.filter { $0.status == "ready" }.count ?? 0 }
    var outcomeActionLabel: String {
        guard let plan, let outcome = plan.outcome else { return "Run reviewed actions" }
        let preserved = plan.rows.filter { $0.status == "ready" && $0.action == "archive" }.count
        return outcome.actionLabel(preservedProjects: preserved)
    }
    private var compactReview: Bool { sheetWidth(1120) < 1000 }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Cleanup wizard").font(.title2).fontWeight(.semibold)
                Spacer()
                Text(["Rules", "Choose actions", "Review", "Results"][phase]).font(.headline).foregroundStyle(.secondary)
            }
            if phase == 0 { ruleStep }
            else if phase == 1 { proposalStep }
            else { reviewStep }
            Divider()
            HStack {
                if store.busy { ProgressView().controlSize(.small); Text(store.status).font(.caption).lineLimit(1) }
                Spacer()
                Button(phase == 3 ? "Done" : "Close") {
                    store.wizardShown = false
                    store.load()
                }.disabled(store.busy).keyboardShortcut(.cancelAction)
            }
        }.padding(24).frame(width: sheetWidth(1120), height: compactReview ? 700 : 780)
            .interactiveDismissDisabled(store.busy)
            .sheet(isPresented: $taskEditorShown) { TasksView(initialRules: rules).environmentObject(store) }
            .onAppear {
                store.invoke(["wizard-state"], as: WizardState.self) { value in
                    rules = value.rules; kinds = value.cacheKinds; last = value.last
                    if captureReview, let review = value.last { plan = review; selected = review.rows.first?.id; phase = 2 }
                }
            }
    }
    var ruleStep: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("Choose the defaults. Review every proposed action after a fresh scan.")
                .font(.callout).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 16) {
                Picker("Measure age using", selection: $rules.ageBasis) {
                    Text("Git activity (default)").tag("git")
                    Text("Project file modification").tag("files")
                }.frame(width: 440)
                Toggle("Exclude projects with active sessions from suggestions", isOn: $rules.ignoreActive)
                HStack {
                    Text("Start cache cleanup at").frame(width: 235, alignment: .leading)
                    TextField("Cleanup days", value: $rules.cacheDays, format: .number).labelsHidden().textFieldStyle(.roundedBorder).frame(width: 85)
                    Text("days")
                    Stepper("Cleanup age", value: $rules.cacheDays, in: 1...3649).labelsHidden().fixedSize()
                }
                HStack {
                    Text("Start freezing at").frame(width: 235, alignment: .leading)
                    TextField("Freeze days", value: $rules.freezeDays, format: .number).labelsHidden().textFieldStyle(.roundedBorder).frame(width: 85)
                    Text("days")
                    Stepper("Freeze age", value: $rules.freezeDays, in: 2...3650).labelsHidden().fixedSize()
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
            Text("Under \(rules.cacheDays) days: leave unchanged. From \(rules.cacheDays) to \(max(rules.cacheDays, rules.freezeDays - 1)) days: clean selected cache types. At \(rules.freezeDays) days and older: propose freezing instead.")
                .font(.callout)
            Text("Cache types to clean").font(.headline)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 200), alignment: .leading)], alignment: .leading, spacing: 12) {
                ForEach(kinds, id: \.self) { kind in
                    Toggle(kind, isOn: Binding(get: { rules.cacheKinds.contains(kind) }, set: { value in
                        if value { rules.cacheKinds.append(kind) } else { rules.cacheKinds.removeAll { $0 == kind } }
                    })).toggleStyle(.checkbox)
                }
            }
            Text("Age is an estimate. File age ignores dependencies, Git metadata and generated/review folders; copies and checkouts can reset file times. Unknown age stays untouched by default. Explicit project choices can override age rules, but running processes and preservation checks still hold unsafe actions.")
                .font(.callout).foregroundStyle(.secondary)
            Spacer()
            HStack {
                if let previous = last {
                    Button(previous.status == "review" ? "Open saved review" : "Previous batch results") {
                        plan = previous; selected = previous.rows.first?.id; approved = false
                        phase = previous.status == "review" ? 2 : 3
                    }
                }
                Button("Save rules as a task…") { taskEditorShown = true }.disabled(store.busy || store.isDemo)
                Spacer()
                Button("Scan projects & suggest actions") {
                    store.invoke(["wizard-scan", encoded(rules)], as: WizardScan.self) { value in
                        proposals = value.rows; scanIssues = value.scanIssues; phase = 1
                        store.load()
                    }
                }.buttonStyle(.borderedProminent).disabled(store.busy || rules.freezeDays <= rules.cacheDays || rules.cacheDays < 1 || rules.freezeDays > 3650)
            }
        }
    }
    var proposalStep: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("These are suggestions. Change any action before preparing the exact review. Nothing has been cleaned, pushed or frozen.")
                .font(.callout).foregroundStyle(.secondary)
            HStack {
                Text("\(proposals.filter { $0.action != "keep" }.count) actions · \(proposals.filter { $0.action == "keep" }.count) unchanged")
                Spacer()
                Button("Leave all unchanged") { for i in proposals.indices { proposals[i].action = "keep" } }
            }.font(.callout)
            List {
                ForEach($proposals) { $row in
                    VStack(alignment: .leading, spacing: 5) {
                        HStack(alignment: .top) {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(row.name).fontWeight(.medium)
                                Text((row.ageDays.map { "\($0) days" } ?? "Unknown age") +
                                     " · " + bytes(row.size) + (row.active ? " · Active session" : ""))
                                    .font(.caption).foregroundStyle(.secondary)
                            }.frame(maxWidth: .infinity, alignment: .leading)
                            Picker("Action for " + row.name, selection: $row.action) {
                                ForEach(actions, id: \.self) { Text(actionName($0)).tag($0) }
                            }.labelsHidden().frame(width: 290)
                            if row.action == "freeze" || row.action == "archive" {
                                Picker("Remote for " + row.name, selection: $row.remote) {
                                    ForEach(row.remotes, id: \.name) { Text($0.name).tag($0.name) }
                                }.labelsHidden().frame(width: 100)
                            }
                        }
                        Text(row.reason).font(.caption).foregroundStyle(.secondary)
                        Text("\(row.cacheCount) eligible caches (\(bytes(row.cacheSize))) · \(row.changedCount) changed source entries · \(row.branchCount) \(row.branchCount == 1 ? "branch" : "branches") to review · \(row.stashCount) stashes · \(row.localCount) local file matches")
                            .font(.caption).foregroundStyle(.secondary)
                    }.padding(.vertical, 7)
                }
            }.listStyle(.inset)
            if scanIssues > 0 {
                Text("\(scanIssues) scan issues. Incomplete or unavailable state can block preparation.").font(.caption).foregroundStyle(.orange)
            }
            HStack {
                Button("Back to rules") { phase = 0 }
                Spacer()
                Button("Build review") {
                    let request = WizardRequest(rules: rules, choices: proposals.map {
                        WizardChoice(path: $0.path, action: $0.action, remote: $0.remote)
                    })
                    store.invoke(["wizard-prepare", encoded(request)], as: WizardPlan.self) { value in
                        plan = value; selected = value.rows.first?.id; approved = false; phase = 2
                    }
                }.buttonStyle(.borderedProminent).disabled(store.busy || !proposals.contains { $0.action != "keep" })
            }
        }
    }
    var reviewStep: some View {
        VStack(alignment: .leading, spacing: compactReview ? 6 : 10) {
            Text(plan?.message ?? "").font(.callout).foregroundStyle(.secondary)
            if plan?.status == "running" {
                Text("This batch was interrupted. Inspect recorded results, archives and Trash history; start a fresh review for remaining work. It will not run twice.")
                    .font(.callout).foregroundStyle(.orange)
            }
            HSplitView {
                List(plan?.rows ?? [], selection: $selected) { row in
                    VStack(alignment: .leading, spacing: 5) {
                        Text(row.name).fontWeight(.medium)
                        Text(actionName(row.action)).font(.caption)
                        Label(row.status.capitalized, systemImage: row.status == "done" ? "checkmark.circle" : row.status == "ready" ? "circle" : "exclamationmark.circle")
                            .font(.caption).foregroundStyle(row.status == "done" ? Color.green : Color.secondary)
                    }.padding(.vertical, 5).tag(row.id)
                }.frame(minWidth: 160, idealWidth: 220, maxWidth: 280)
                VStack(alignment: .leading, spacing: 12) {
                    if let row = reviewRow {
                        Text(row.path).font(.caption).textSelection(.enabled)
                        Text(row.message).font(.callout)
                        Picker("Details", selection: $reviewTopic) {
                            ForEach(["Summary", "Publication", "Local recovery", "Omissions & paths"], id: \.self) { Text($0) }
                        }.labelsHidden().pickerStyle(.segmented)
                            .accessibilityIdentifier("wizard.reviewTopic")
                        ScrollView {
                            details(row).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    } else {
                        ContentUnavailableView("Choose an action", systemImage: "list.bullet.clipboard")
                    }
                }.padding(.leading, 16).frame(minWidth: 400, maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            }.frame(minHeight: compactReview ? 190 : 260, maxHeight: compactReview ? 250 : 360)
            if let outcome = plan?.outcome {
                ScrollView { OutcomeSummaryView(outcome: outcome) }
                    .frame(maxHeight: compactReview ? 150 : 220)
            }
            if phase == 2 {
                Text("Acceptance covers every ready row. Cache folders move to Trash. Freeze archives local files and Git metadata, pushes only the listed ordinary refs, verifies recovery and the remote, then removes the listed checkouts. Private recovery stays on this Mac and is not encrypted. Blocked rows will not run. Review expires after ten minutes.")
                    .font(.caption).foregroundStyle(.secondary)
                Toggle("I reviewed the ready actions, cache edits, publication refs, local recovery and checkout removals.", isOn: $approved)
                HStack {
                    Button("Change actions") { phase = 1; approved = false }.disabled(store.busy || proposals.isEmpty)
                    Spacer()
                    Text("\(ready) ready · \(plan?.rows.filter { $0.status == "blocked" }.count ?? 0) blocked").font(.caption)
                    Button(outcomeActionLabel) {
                        guard let id = plan?.id else { return }
                        store.invoke(["wizard-execute", id], as: WizardPlan.self) { value in
                            plan = value; last = value; phase = 3; approved = false; store.load()
                        }
                    }.buttonStyle(.borderedProminent).disabled(store.busy || store.isDemo || !approved || ready == 0)
                }
            } else {
                HStack {
                    Text("\(plan?.rows.filter { $0.status == "done" }.count ?? 0) completed · \(plan?.rows.filter { ["failed", "partial", "running"].contains($0.status) }.count ?? 0) need attention").font(.callout)
                    Spacer()
                    Button("Start a new review") { phase = 0; plan = nil; approved = false }
                }
            }
        }
    }
    @ViewBuilder func details(_ row: WizardRow) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if let cleanup = row.cleanup {
                Text("Move \(cleanup.items.count) \(cleanup.items.count == 1 ? "folder" : "folders") to Trash · \(bytes(cleanup.totalSize))").font(.headline)
                ForEach(cleanup.items) { item in Text(item.path + " · " + bytes(item.size)).font(.caption).textSelection(.enabled) }
                ForEach(cleanup.blocked) { item in Text("Not run: " + item.path + "\n" + item.reason).font(.caption) }
                if let results = row.results {
                    Divider()
                    ForEach(Array(results.enumerated()), id: \.offset) { _, item in Text(item.path + "\n" + item.message).font(.caption) }
                }
            }
            if let freeze = row.freeze {
                if reviewTopic == "Summary" {
                    Text(row.action == "freeze" ? "Freeze" : "Preserve").font(.headline)
                    Text("Publish to: " + freeze.destination).textSelection(.enabled)
                    Text("\(freeze.checkouts.count) checkouts · \(freeze.localCount) local entries · \(bytes(freeze.localSize)) plus Git recovery")
                    Text("Freezer: " + freeze.archiveLocation).font(.caption).textSelection(.enabled)
                    Text(freeze.message).font(.caption)
                    Text("Local stashes, reflogs and private refs stay in local Git recovery. Source changes must be committed separately before freezing.").font(.callout)
                } else if reviewTopic == "Publication" {
                    Text("Publish these ordinary branches/tags").font(.headline)
                    ForEach(freeze.publish) { ref in Text(ref.ref + "\n" + ref.sha).font(.caption).textSelection(.enabled) }
                    Text("Preserve these refs locally").font(.headline)
                    ForEach(freeze.localRefs) { ref in Text(ref.ref).font(.caption) }
                } else if reviewTopic == "Local recovery" {
                    ForEach(freeze.localFiles, id: \.self) { Text($0).font(.caption).textSelection(.enabled) }
                } else {
                    Text(row.action == "freeze" ? "Remove these checkouts after verification" : "Leave these checkouts in place").font(.headline)
                    ForEach(freeze.checkouts, id: \.self) { Text($0).font(.caption).textSelection(.enabled) }
                    Text("Omit these rebuild folders from recovery").font(.headline)
                    ForEach(freeze.excluded, id: \.self) { Text($0).font(.caption).textSelection(.enabled) }
                }
            }
        }
    }
}
