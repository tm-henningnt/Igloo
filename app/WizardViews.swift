import SwiftUI
import AppKit

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
    var thawLast: WizardThawPlan?
}
struct WizardThawRef: Decodable, Identifiable {
    var path: String; var ref: String; var sha: String
    var id: String { path + ref }
}
struct WizardThawRow: Decodable, Identifiable {
    var id: String; var archiveID: String; var name: String; var path: String
    var status: String; var message: String; var destination: String
    var refs: [WizardThawRef]; var checkouts: [String]; var localFiles: [String]
    var archiveLocation: String; var restoredRoots: [String]?
}
struct WizardThawPlan: Decodable, Identifiable {
    var id: String; var created: String; var root: String; var vault: String
    var status: String; var rows: [WizardThawRow]; var message: String; var outcome: ReviewOutcome?
}

struct WizardPrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .fontWeight(.semibold)
            .foregroundStyle(isEnabled ? Color.white : Color.primary.opacity(0.28))
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .frame(minHeight: 30)
            .background(isEnabled ? Color.accentColor : Color.primary.opacity(0.07),
                        in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .stroke(Color.primary.opacity(isEnabled ? 0 : 0.08), lineWidth: 1)
            }
            .opacity(isEnabled && configuration.isPressed ? 0.86 : 1)
    }
}

struct CleanupWizardView: View {
    @EnvironmentObject var store: OrganizerStore
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.controlActiveState) private var controlActiveState
    var captureReview = false
    var captureResult = false
    var captureView: String?
    @LocalState var rules = WizardRules()
    @LocalState var kinds: [String] = []
    @LocalState var proposals: [WizardProposal] = []
    @LocalState var phase = 0
    @LocalState var plan: WizardPlan?
    @LocalState var last: WizardPlan?
    @LocalState var selected: String?
    @LocalState var approved = false
    @LocalState var scanIssues = 0
    @LocalState var scanning = false
    @LocalState var taskEditorShown = false
    @LocalState var reviewTopic = "Summary"
    @LocalState var mode = "start"
    @LocalState var thawPhase = 0
    @LocalState var thawArchives: [FrozenArchive] = []
    @LocalState var thawSelection: Set<String> = []
    @LocalState var thawPlan: WizardThawPlan?
    @LocalState var thawLast: WizardThawPlan?
    @LocalState var selectedThawID: String?
    @LocalState var approvedThawRows: Set<String> = []
    let actions = ["keep", "cleanup", "archive", "freeze"]
    init(captureReview: Bool = false, captureResult: Bool = false, captureView: String? = nil,
         startMakeSpace: Bool = false) {
        self.captureReview = captureReview
        self.captureResult = captureResult
        self.captureView = captureView
        let example: WizardPlan? = captureResult ? DemoCatalog.reply(["wizard-result"], as: WizardPlan.self)
            : (captureReview ? DemoCatalog.reply(["wizard-state"], as: WizardState.self)?.last : nil)
        let start = captureView == "wizard-start"
        let selection = captureView == "wizard-thaw-selection"
        let thawResult = captureView == "wizard-thaw-result"
        let thawCapture = captureView?.hasPrefix("wizard-thaw-") == true && !selection && !start
        let exampleThaw: WizardThawPlan? = thawCapture ? DemoCatalog.reply(
            [thawResult ? "wizard-thaw-execute" : "wizard-thaw-prepare", captureView ?? ""], as: WizardThawPlan.self) : nil
        let focusID = ["wizard-thaw-occupied": "demo-beacon", "wizard-thaw-remote": "demo-tidemark",
                       "wizard-thaw-corrupt": "demo-cedar", "wizard-thaw-not-frozen": "demo-maproom",
                       "wizard-thaw-rehydrating": "demo-icegarden", "wizard-thaw-ready": "demo-icegarden"][captureView ?? ""]
        _phase = LocalState(initialValue: captureResult ? 3 : (captureReview ? 2 : 0))
        _mode = LocalState(initialValue: startMakeSpace ? "make-space"
            : ((captureReview || captureResult) ? "make-space" : (start ? "start" : ((thawCapture || selection) ? "thaw" : "start"))))
        _plan = LocalState(initialValue: example)
        _selected = LocalState(initialValue: example?.rows.first?.id)
        _thawPhase = LocalState(initialValue: thawResult ? 2 : (thawCapture ? 1 : 0))
        _thawPlan = LocalState(initialValue: exampleThaw)
        _selectedThawID = LocalState(initialValue: focusID ?? exampleThaw?.rows.first?.id)
        _thawArchives = LocalState(initialValue: selection ? DemoCatalog.wizardThawArchives() : [])
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
    func inclusionBinding(for row: Binding<WizardProposal>) -> Binding<Bool> {
        Binding(get: { row.wrappedValue.action != "keep" }, set: { included in
            if included {
                if row.wrappedValue.action == "keep" {
                    row.wrappedValue.action = (row.wrappedValue.ageDays ?? rules.cacheDays) >= rules.freezeDays ? "freeze" : "cleanup"
                }
            } else {
                row.wrappedValue.action = "keep"
            }
        })
    }
    var reviewRow: WizardRow? { plan?.rows.first { $0.id == selected } }
    var ready: Int { plan?.rows.filter { $0.status == "ready" }.count ?? 0 }
    var outcomeActionLabel: String {
        guard let plan, let outcome = plan.outcome else { return "Run reviewed actions" }
        let preserved = plan.rows.filter { $0.status == "ready" && $0.action == "archive" }.count
        return outcome.actionLabel(preservedProjects: preserved)
    }
    private var compactReview: Bool { sheetWidth(1120) < 1000 }
    // The backend refuses a freeze age at or below the cleanup age.
    private var ruleSummary: String {
        let cache = rules.cacheDays, freeze = rules.freezeDays
        if freeze <= cache { return "The freeze threshold must be later than the cleanup threshold." }
        let range = freeze - 1 == cache ? "At \(cache) days" : "From \(cache) to \(freeze - 1) days"
        return "Under \(cache) days: leave unchanged. \(range): clean selected cache types. At \(freeze) days and older: propose freezing instead."
    }
    private var wizardSheetHeight: CGFloat {
        if mode == "thaw" { return compactReview ? 660 : 700 }
        if finishedWizard { return compactReview ? 500 : 560 }
        return compactReview ? 700 : 780
    }
    private var stepTitle: String {
        if mode == "start" { return "Choose a path" }
        if mode == "thaw" { return ["Select projects", "Thaw plan", "Results"][min(thawPhase, 2)] }
        return ["Rules", "Choose actions", "Review", "Results"][min(phase, 3)]
    }
    private var selectedThawRow: WizardThawRow? { thawPlan?.rows.first { $0.id == selectedThawID } }
    private var readyToThaw: Int { thawPlan?.rows.filter { $0.status == "ready" }.count ?? 0 }
    private var allReadyThawRowsReviewed: Bool {
        let readyIDs = Set((thawPlan?.rows ?? []).filter { $0.status == "ready" }.map(\.id))
        return !readyIDs.isEmpty && readyIDs.isSubset(of: approvedThawRows)
    }
    private var thawBlockedReasonColor: Color {
        colorScheme == .dark ? .orange : Color(red: 0.55, green: 0.23, blue: 0.00)
    }
    private var selectedRowTextColor: Color { Color(nsColor: .selectedMenuItemTextColor) }
    private var finishedThaw: Bool { mode == "thaw" && thawPhase == 2 }
    private var isThawExecuting: Bool {
        store.busy || (store.isDemo && captureView == "wizard-thaw-rehydrating")
    }
    private var activeThawRow: WizardThawRow? {
        guard isThawExecuting, let rows = thawPlan?.rows else { return nil }
        if let running = rows.first(where: { $0.status == "running" }) { return running }
        if captureView == "wizard-thaw-rehydrating" { return rows.first { $0.status == "ready" } }
        return rows.first { $0.status == "ready" && store.status.localizedCaseInsensitiveContains($0.name) }
            ?? rows.first { $0.status == "ready" }
    }
    private var finishedWizard: Bool { mode == "make-space" && phase == 3 }
    private var closeButton: some View {
        Button(finishedWizard || finishedThaw ? "Done" : "Close") {
            store.wizardShown = false
            store.wizardMakeSpace = false
            store.load()
        }.disabled(isThawExecuting).keyboardShortcut(.cancelAction)
    }
    private var thawProgressMessage: String {
        if captureView == "wizard-thaw-rehydrating", let row = activeThawRow {
            return "Thawing \(row.name)…"
        }
        return store.status
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(mode == "thaw" ? "Thaw projects" : "Cleanup wizard").font(.title2).fontWeight(.semibold)
                Spacer()
                Text(stepTitle).font(.headline).foregroundStyle(.secondary)
            }
            Group {
                if mode == "start" { startStep }
                else if mode == "thaw" { thawContent }
                else if phase == 0 { ruleStep }
                else if phase == 1 { proposalStep }
                else { reviewStep }
            }
        }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .padding(24).frame(width: sheetWidth(1120), height: wizardSheetHeight, alignment: .topLeading)
            .background(.background)
            .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous)
                .stroke(Color.primary.opacity(0.16), lineWidth: 1))
            .interactiveDismissDisabled(isThawExecuting)
            .sheetErrorAlert(store)
            .sheet(isPresented: $taskEditorShown) { TasksView(initialRules: rules).environmentObject(store) }
            .onChange(of: store.wizardMakeSpace) {
                guard store.wizardMakeSpace else { return }
                mode = "make-space"
                phase = 0
                plan = nil
                thawPhase = 0
                thawPlan = nil
                store.wizardMakeSpace = false
            }
            .onAppear {
                store.wizardMakeSpace = false
                loadWizardState()
            }
            // A wizard opened during the launch catalog load waits for that load, then reads the saved rules.
            .onChange(of: store.busy) {
                if !store.busy && kinds.isEmpty { loadWizardState() }
            }
    }
    private func loadWizardState() {
        store.invoke(["wizard-state"], as: WizardState.self) { value in
            rules = value.rules; kinds = value.cacheKinds; last = value.last; thawLast = value.thawLast
            if captureReview, let review = value.last { plan = review; selected = review.rows.first?.id; phase = 2 }
        }
    }
    var startStep: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Choose what you want to do with your projects.").font(.callout).foregroundStyle(.secondary)
            HStack(alignment: .top, spacing: 16) {
                Button { mode = "make-space"; phase = 0 } label: {
                    VStack(alignment: .leading, spacing: 12) {
                        Label("Make space", systemImage: "wand.and.stars").font(.title3).fontWeight(.semibold)
                        Text("Review cleanup and Freeze suggestions for your Projects folder.")
                            .font(.callout).multilineTextAlignment(.leading).foregroundStyle(.secondary)
                    }.frame(maxWidth: .infinity, minHeight: 150, alignment: .leading).padding(16)
                }.buttonStyle(.bordered).accessibilityIdentifier("wizard.start.makeSpace")
                Button { enterThaw() } label: {
                    VStack(alignment: .leading, spacing: 12) {
                        Label("Thaw projects", systemImage: "sun.max").font(.title3).fontWeight(.semibold)
                        Text("Restore several Frozen projects from their remotes with one Thaw plan.")
                            .font(.callout).multilineTextAlignment(.leading).foregroundStyle(.secondary)
                    }.frame(maxWidth: .infinity, minHeight: 150, alignment: .leading).padding(16)
                }.buttonStyle(.borderedProminent).accessibilityIdentifier("wizard.start.thaw")
            }
            Spacer()
            HStack { Spacer(); closeButton }
        }
    }
    func enterThaw() {
        mode = "thaw"
        thawPhase = 0
        thawPlan = nil
        thawSelection = []
        approvedThawRows = []
        if store.isDemo {
            thawArchives = DemoCatalog.wizardThawArchives().filter { $0.state == "frozen" }
        } else {
            store.invoke(["snapshot"], as: Snapshot.self) { value in
                thawArchives = (value.archives ?? []).filter { $0.state == "frozen" }
            }
        }
    }
    func prepareThaw(_ archiveIDs: [String]? = nil) {
        let ids = archiveIDs ?? thawArchives.filter { thawSelection.contains($0.id) }.map(\.id)
        store.invoke(["wizard-thaw-prepare", encoded(ids)], as: WizardThawPlan.self) { value in
            thawPlan = value
            selectedThawID = value.rows.first?.id
            approvedThawRows = []
            thawPhase = 1
        }
    }
    func thawApprovalBinding(for row: WizardThawRow) -> Binding<Bool> {
        Binding(get: { approvedThawRows.contains(row.id) }, set: { approved in
            if approved { approvedThawRows.insert(row.id) }
            else { approvedThawRows.remove(row.id) }
        })
    }
    func statusSymbol(_ status: String) -> String {
        switch status {
        case "done": return "checkmark.circle.fill"
        case "ready": return "circle"
        case "running": return "arrow.trianglehead.2.clockwise"
        default: return "exclamationmark.circle"
        }
    }
    @ViewBuilder var thawContent: some View {
        if thawPhase == 0 { thawSelectionStep }
        else if thawPhase == 1 { thawReviewStep }
        else { thawResultsStep }
    }
    var thawSelectionStep: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Select Frozen projects to restore from their remotes. Hold does not apply. Offline recovery remains available per project in Freezer.")
                .font(.callout).foregroundStyle(.secondary)
            Text("Frozen projects").font(.headline).accessibilityAddTraits(.isHeader)
            if thawArchives.filter({ $0.state == "frozen" }).isEmpty {
                ContentUnavailableView("No Frozen projects", systemImage: "snowflake",
                                       description: Text("Frozen projects appear here after Freeze completes."))
            } else {
                List {
                    ForEach(thawArchives.filter { $0.state == "frozen" }) { archive in
                        Toggle(isOn: Binding(get: { thawSelection.contains(archive.id) }, set: { selected in
                            if selected { thawSelection.insert(archive.id) }
                            else { thawSelection.remove(archive.id) }
                        })) {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(archive.name).fontWeight(.medium)
                                Text("Frozen \(shortDate(archive.created)) · \(archive.destination)")
                                    .font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                                    .help(archive.destination)
                                Text(archive.path).font(.caption).textSelection(.enabled).lineLimit(1).truncationMode(.middle)
                                    .help(archive.path)
                            }.padding(.vertical, 5)
                        }.toggleStyle(.checkbox)
                            .tint(colorScheme == .dark ? Color(red: 0.42, green: 0.72, blue: 1) : .accentColor)
                            .accessibilityLabel("\(archive.name), Frozen \(shortDate(archive.created)), remote \(archive.destination), original path \(archive.path)")
                            .accessibilityHint("Select this Frozen project for the Thaw plan.")
                            .accessibilityIdentifier("wizard.thaw.archive.\(archive.id)")
                    }
                }.listStyle(.inset)
                    .scrollIndicators(.visible)
                    .frame(minHeight: compactReview ? 300 : 340, maxHeight: .infinity)
                    .accessibilityIdentifier("wizard.thaw.selection")
            }
            HStack {
                Button("Back") { mode = "start" }
                    .accessibilityIdentifier("wizard.thaw.back")
                if let previous = thawLast, previous.status != "review" {
                    Button("Previous Thaw results") {
                        thawPlan = previous
                        selectedThawID = previous.rows.first?.id
                        thawPhase = 2
                        approvedThawRows = []
                    }
                }
                Spacer()
                Text("\(thawSelection.count) selected").font(.caption).foregroundStyle(.secondary)
                Button("Create Thaw plan") { prepareThaw() }
                    .buttonStyle(WizardPrimaryButtonStyle()).disabled(store.busy || thawSelection.isEmpty)
                    .accessibilityIdentifier("wizard.thaw.prepare")
                closeButton
            }
        }
    }
    var thawReviewStep: some View {
        VStack(alignment: .leading, spacing: compactReview ? 6 : 10) {
            Text(thawPlan?.message ?? "").font(.callout).foregroundStyle(.secondary)
            if isThawExecuting {
                VStack(alignment: .leading, spacing: 8) {
                    Text(thawProgressMessage).font(.callout).fontWeight(.medium)
                    ProgressView().progressViewStyle(.linear)
                        .tint(colorScheme == .light ? Color(red: 0.08, green: 0.34, blue: 0.78) : .accentColor)
                        .frame(height: 10)
                        .background(Color.primary.opacity(0.10), in: Capsule())
                        .overlay(Capsule().stroke(Color.primary.opacity(0.18), lineWidth: 1))
                }.accessibilityIdentifier("wizard.thaw.progress")
            } else if thawPlan?.status == "running" {
                Text("Thaw was interrupted. Completed rows remain restored; unfinished rows will not be replayed. Use Freezer recovery to inspect and finish any partial restore.")
                    .font(.callout).foregroundStyle(.orange)
            }
            thawRowsAndDetails
            if let row = selectedThawRow, row.status == "ready", thawPlan?.status == "review", !isThawExecuting {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Expected outcome for \(row.name)").font(.headline)
                    Text("Checkouts to restore: \(row.checkouts.count)")
                    Text("Remote refs to restore: \(row.refs.count)")
                }.font(.caption)
            }
            if thawPlan?.status == "review" && !isThawExecuting {
                Text("A failed restore may leave part of a project in place. Its Freezer archive remains available.")
                    .font(.caption).foregroundStyle(.secondary)
                Text("Igloo restores from the remote and Freezer. It does not install dependencies or run project code. Review expires in ten minutes.")
                    .font(.caption).foregroundStyle(.secondary)
                if let row = selectedThawRow, row.status == "ready" {
                    Toggle("I reviewed the remote refs, original paths and local-only files shown for \(row.name).",
                           isOn: thawApprovalBinding(for: row))
                        .accessibilityIdentifier("wizard.thaw.acceptance")
                } else {
                    Text("Select each ready project to review and confirm its restore details.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                HStack {
                    Button("Change selection") { thawPhase = 0; thawPlan = nil; approvedThawRows = [] }
                    Spacer()
                    Text("\(readyToThaw) ready · \(thawPlan?.rows.filter { $0.status == "blocked" }.count ?? 0) blocked")
                        .font(.caption)
                    closeButton
                    Button("Thaw \(readyToThaw) ready \(readyToThaw == 1 ? "project" : "projects")") {
                        guard let id = thawPlan?.id else { return }
                        store.invoke(["wizard-thaw-execute", id], as: WizardThawPlan.self) { value in
                            thawPlan = value
                            thawLast = value
                            selectedThawID = value.rows.first?.id
                            thawPhase = 2
                            approvedThawRows = []
                            store.load()
                        }
                    }.buttonStyle(WizardPrimaryButtonStyle()).disabled(store.busy || !allReadyThawRowsReviewed)
                        .accessibilityIdentifier("wizard.thaw.execute")
                }
            } else if !isThawExecuting {
                HStack {
                    Text("\(thawPlan?.rows.filter { $0.status == "done" }.count ?? 0) completed · \(thawPlan?.rows.filter { ["failed", "blocked", "running"].contains($0.status) }.count ?? 0) need attention")
                        .font(.callout)
                    Spacer()
                    closeButton
                    Button("Start a new Thaw plan") { enterThaw() }
                        .accessibilityIdentifier("wizard.thaw.newPlan")
                }
            }
        }
    }
    var thawRowsAndDetails: some View {
        HSplitView {
            List(thawPlan?.rows ?? [], selection: $selectedThawID) { row in
                VStack(alignment: .leading, spacing: 4) {
                    Text(row.name).fontWeight(.medium)
                    if isThawExecuting && row.id == activeThawRow?.id && ["ready", "running"].contains(row.status) {
                        HStack(spacing: 5) {
                            ProgressView().controlSize(.small)
                            Text("Thawing")
                        }.font(.caption).foregroundStyle(.secondary)
                    } else if isThawExecuting && row.status == "ready" {
                        Text("Waiting").font(.caption).foregroundStyle(.secondary)
                    } else if row.status == "ready" && thawPlan?.status == "review" {
                        Label(approvedThawRows.contains(row.id) ? "Reviewed" : "Review details",
                              systemImage: approvedThawRows.contains(row.id) ? "checkmark.circle.fill" : "circle")
                            .font(.caption).foregroundStyle(row.id == selectedThawID && controlActiveState == .key ? selectedRowTextColor :
                                (approvedThawRows.contains(row.id) ? Color.accentColor : Color.secondary))
                    } else if row.status == "blocked" {
                        Text("Blocked · \(shortThawReason(row.message))").font(.caption).lineLimit(1).truncationMode(.tail)
                            .foregroundStyle(row.id == selectedThawID && controlActiveState == .key ? selectedRowTextColor : thawBlockedReasonColor)
                    } else {
                        Label(row.status.capitalized, systemImage: statusSymbol(row.status)).font(.caption)
                    }
                }.padding(.vertical, 5).tag(row.id)
                    .accessibilityIdentifier("wizard.thaw.row.\(row.id)")
            }.frame(minWidth: 150, idealWidth: 205, maxWidth: 260)
            ScrollView {
                // Selectable text keeps its old string when only the row changes, so each row gets its own detail view.
                if let row = selectedThawRow {
                    thawDetails(row).frame(maxWidth: .infinity, alignment: .leading).id(row.id)
                } else {
                    ContentUnavailableView("Choose a project", systemImage: "list.bullet.clipboard")
                }
            }.padding(.leading, 12).frame(minWidth: 365, maxWidth: .infinity, maxHeight: .infinity)
        }.frame(minHeight: thawRowsMinimumHeight, maxHeight: .infinity).layoutPriority(1)
    }
    // Room for every row up to six (four in compact), so no row is cut; the list takes any extra height.
    private var thawRowsMinimumHeight: CGFloat {
        let visible = CGFloat(min(thawPlan?.rows.count ?? 0, compactReview ? 4 : 6))
        return max(compactReview ? 200 : 240, visible * 52 + 16)
    }
    @ViewBuilder func thawDetails(_ row: WizardThawRow) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(row.name).font(.title3).fontWeight(.semibold)
            if isThawExecuting && row.id == activeThawRow?.id {
                Text("Thawing").font(.callout).foregroundStyle(.secondary)
            } else if row.status == "ready" {
                Text(row.message).font(.callout)
                Text("Remote").font(.headline)
                Text(row.destination.isEmpty ? "Unavailable" : row.destination).font(.caption).textSelection(.enabled)
                Text("Remote refs to restore").font(.headline)
                if row.refs.isEmpty { Text("No refs recorded").font(.caption).foregroundStyle(.secondary) }
                ForEach(row.refs) { ref in
                    let hash = shortGitHash(ref.sha)
                    Text(ref.ref + (hash.isEmpty ? "" : " · " + hash)).font(.caption).textSelection(.enabled)
                }
                if !row.archiveLocation.isEmpty {
                    Text("Freezer archive").font(.headline)
                    Text(row.archiveLocation).font(.caption).textSelection(.enabled)
                }
                Text("Original paths").font(.headline)
                ForEach(row.checkouts, id: \.self) { Text($0).font(.caption).textSelection(.enabled) }
                Text("Local-only files to restore").font(.headline)
                if row.localFiles.isEmpty { Text("None recorded").font(.caption).foregroundStyle(.secondary) }
                ForEach(row.localFiles, id: \.self) {
                    Text(relative($0, to: row.path)).font(.caption).textSelection(.enabled)
                }
            } else if row.status == "done" {
                Text(row.message).font(.callout)
                if !row.localFiles.isEmpty {
                    Text("Local-only files restored").font(.headline)
                    ForEach(row.localFiles, id: \.self) {
                        Text(relative($0, to: row.path)).font(.caption).textSelection(.enabled)
                    }
                }
            } else {
                Text(row.message).font(.callout).foregroundStyle(row.status == "blocked" ? thawBlockedReasonColor : Color.primary)
                if row.status == "blocked" {
                    Text("This project and its Freezer archive are untouched.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if !row.path.isEmpty, row.restoredRoots?.isEmpty != false {
                    Text("Original path: \(row.path)").font(.caption).textSelection(.enabled)
                }
                if let roots = row.restoredRoots, !roots.isEmpty {
                    Text("Already restored roots").font(.headline)
                    ForEach(roots, id: \.self) { Text($0).font(.caption).textSelection(.enabled) }
                }
                thawBlockerActions(row)
            }
        }
    }
    func shortThawReason(_ message: String) -> String {
        let value = message.lowercased()
        if value.contains("occupied") { return "Path occupied" }
        if value.contains("remote") { return "Remote unavailable" }
        if value.contains("checksum") || value.contains("archive") { return "Archive needs attention" }
        if value.contains("no longer frozen") || value.contains("not frozen") { return "No longer Frozen" }
        if value.contains("overlap") { return "Paths overlap another selection" }
        if value.contains("interrupted") || value.contains("rehydrat") { return "Recovery needed" }
        return message
    }
    @ViewBuilder func thawBlockerActions(_ row: WizardThawRow) -> some View {
        let reason = row.message.lowercased()
        if reason.contains("remote") {
            Button("Check remote again") { prepareThaw(thawPlan?.rows.map(\.archiveID) ?? [row.archiveID]) }
                .accessibilityIdentifier("wizard.thaw.checkRemote")
        } else if reason.contains("occupied") || reason.contains("parent folder") {
            Button("Show path in Finder") { store.reveal(row.path) }
                .accessibilityIdentifier("wizard.thaw.showPath")
        } else if reason.contains("overlap") {
            Button("Change selection") { thawPhase = 0; thawPlan = nil; approvedThawRows = [] }
                .accessibilityIdentifier("wizard.thaw.changeSelection")
        } else {
            Button("Open Freezer") {
                store.wizardShown = false
                DispatchQueue.main.async { store.vaultShown = true }
            }.accessibilityIdentifier("wizard.thaw.openFreezer")
        }
    }
    var thawResultsStep: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(thawPlan?.message ?? "Inspect each Thaw result.").font(.callout).foregroundStyle(.secondary)
            if thawPlan?.status == "running" {
                Text("This run was interrupted. Its recorded rows will not be replayed.")
                    .font(.callout).foregroundStyle(.orange)
            }
            thawRowsAndDetails
            if let outcome = thawPlan?.outcome {
                ScrollView { OutcomeSummaryView(outcome: outcome, includeThaw: true, showSpace: false) }
                    .frame(maxHeight: compactReview ? 125 : 155)
            }
            HStack {
                Text("\(thawPlan?.rows.filter { $0.status == "done" }.count ?? 0) completed · \(thawPlan?.rows.filter { ["failed", "blocked", "running"].contains($0.status) }.count ?? 0) need attention")
                    .font(.callout)
                Spacer()
                closeButton
                Button("Start a new Thaw plan") { enterThaw() }
                    .accessibilityIdentifier("wizard.thaw.newPlan")
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
            Text(ruleSummary).font(.callout)
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
                Button("Back") { mode = "start" }
                if let previous = last {
                    Button(previous.status == "review" ? "Open saved review" : "Previous batch results") {
                        plan = previous; selected = previous.rows.first?.id; approved = false
                        phase = previous.status == "review" ? 2 : 3
                    }
                }
                Button("Save rules as a task…") { taskEditorShown = true }.disabled(store.busy || store.isDemo)
                Spacer()
                if scanning || store.busy { SheetProgressRow(text: store.busy ? store.status : "Scanning projects…") }
                Button("Scan projects & suggest actions") {
                    guard !store.busy else { return }
                    scanning = true
                    store.invoke(["wizard-scan", encoded(rules)], as: WizardScan.self, failed: { _ in scanning = false }) { value in
                        scanning = false
                        proposals = value.rows; scanIssues = value.scanIssues; phase = 1
                        store.load()
                    }
                }.buttonStyle(WizardPrimaryButtonStyle()).disabled(store.busy || scanning || rules.freezeDays <= rules.cacheDays || rules.cacheDays < 1 || rules.freezeDays > 3650)
                closeButton
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
                            Toggle("Include \(row.name)", isOn: inclusionBinding(for: $row))
                                .labelsHidden().toggleStyle(.checkbox)
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
            }.listStyle(.inset).frame(maxHeight: .infinity, alignment: .top)
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
                }.buttonStyle(WizardPrimaryButtonStyle()).disabled(store.busy || !proposals.contains { $0.action != "keep" })
                closeButton
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
                            .font(.caption)
                    }.padding(.vertical, 5).tag(row.id)
                }.frame(minWidth: 160, idealWidth: 220, maxWidth: 280)
                VStack(alignment: .leading, spacing: 12) {
                    if let row = reviewRow {
                        Text(row.name).font(.caption).textSelection(.enabled)
                        if !row.message.isEmpty { Text(row.message).font(.callout) }
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
            }.frame(minHeight: finishedWizard ? (compactReview ? 150 : 185) : (compactReview ? 190 : 260),
                    maxHeight: finishedWizard ? (compactReview ? 180 : 220) : (compactReview ? 250 : 360))
            if let outcome = plan?.outcome {
                ScrollView {
                    OutcomeSummaryView(outcome: outcome,
                                       inTrashQualifier: finishedWizard ? "can be restored" : nil,
                                       showRestoredSpace: !finishedWizard,
                                       showNetSpaceChange: finishedWizard,
                                       netSpaceLabel: "Net space change after the archive copy",
                                       heading: finishedWizard ? "Total outcome for both actions" : nil,
                                       spaceExplanation: finishedWizard
                                           ? "Freed from disk is the total change on the drive. In Trash is the size of folders that can be restored."
                                           : nil)
                }
                    // Fill the fixed sheet height so the footer stays at the bottom.
                    .frame(maxHeight: .infinity)
            }
            if phase == 2 {
                Text("Acceptance covers every ready row. Cache folders move to Trash. Freeze archives local files and Git metadata, pushes only the listed ordinary refs, verifies recovery and the remote, then removes the listed checkouts. Private recovery stays on this Mac and is not encrypted. Blocked rows will not run. Review expires after ten minutes.")
                    .font(.caption).foregroundStyle(.secondary)
                Toggle("I reviewed the ready actions, cache edits, publication refs, local recovery and checkout removals.", isOn: $approved)
                HStack {
                    Button("Change actions") { phase = 1; approved = false }.disabled(store.busy || proposals.isEmpty)
                    Spacer()
                    Text("\(ready) ready · \(plan?.rows.filter { $0.status == "blocked" }.count ?? 0) blocked").font(.caption)
                    closeButton
                    Button(outcomeActionLabel) {
                        guard let id = plan?.id else { return }
                        store.invoke(["wizard-execute", id], as: WizardPlan.self) { value in
                            plan = value; last = value; phase = 3; approved = false; store.load()
                        }
                    }.buttonStyle(WizardPrimaryButtonStyle()).disabled(store.busy || store.isDemo || !approved || ready == 0)
                }
            } else {
                HStack {
                    Text("\(plan?.rows.filter { $0.status == "done" }.count ?? 0) completed · \(plan?.rows.filter { ["failed", "partial", "running"].contains($0.status) }.count ?? 0) need attention").font(.callout)
                    Spacer()
                    closeButton
                    Button("Start a new review") { phase = 0; plan = nil; approved = false }
                }
            }
        }
    }
    @ViewBuilder func details(_ row: WizardRow) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if let cleanup = row.cleanup {
                Text("\(row.status == "done" ? "Moved" : "Move") \(cleanup.items.count) \(cleanup.items.count == 1 ? "folder" : "folders") to Trash · \(bytes(cleanup.totalSize))").font(.headline)
                if let results = row.results {
                    Divider()
                    ForEach(Array(results.enumerated()), id: \.offset) { _, item in
                        let size = item.size.map { " · " + bytes($0) } ?? ""
                        OutcomeSummaryRow(label: item.ok ? "Moved to Trash: " + item.path + size : "Not moved: " + item.path + " · " + item.message)
                            .font(.caption)
                    }
                } else {
                    ForEach(cleanup.items) { item in Text("Folder moved to Trash: " + item.path + " · " + bytes(item.size)).font(.caption).textSelection(.enabled) }
                    ForEach(cleanup.blocked) { item in Text("Not run: " + item.path + "\n" + item.reason).font(.caption) }
                }
            }
            if let freeze = row.freeze {
                if reviewTopic == "Summary" {
                    Text(row.action == "freeze" ? "Freeze" : "Preserve").font(.headline)
                    Text("Publish to: " + freeze.destination).textSelection(.enabled)
                    Text("\(freeze.checkouts.count) checkouts · \(freeze.localCount) local entries · \(bytes(freeze.localSize)) plus Git recovery")
                    if !freeze.archiveLocation.isEmpty {
                        Text("Freezer: " + freeze.archiveLocation).font(.caption).textSelection(.enabled)
                    }
                    Text(freeze.message).font(.caption)
                    Text("Local stashes, reflogs and private refs stay in local Git recovery. Source changes must be committed separately before freezing.").font(.callout)
                } else if reviewTopic == "Publication" {
                    Text("Publish these ordinary branches/tags").font(.headline)
                    ForEach(freeze.publish) { ref in
                        let hash = shortGitHash(ref.sha)
                        Text(ref.ref + (hash.isEmpty ? "" : "\n" + hash)).font(.caption).textSelection(.enabled)
                    }
                    Text("Preserve these refs locally").font(.headline)
                    ForEach(freeze.localRefs) { ref in Text(ref.ref).font(.caption) }
                } else if reviewTopic == "Local recovery" {
                    ForEach(freeze.localFiles, id: \.self) { Text($0).font(.caption).textSelection(.enabled) }
                } else {
                    let checkoutMessage = row.status == "done"
                        ? (row.action == "freeze" ? "Checkouts removed after verification" : "Checkouts remained in place")
                        : (row.action == "freeze" ? "Remove these checkouts after verification" : "Leave these checkouts in place")
                    Text(checkoutMessage).font(.headline)
                    ForEach(freeze.checkouts, id: \.self) { Text("Checkout: " + ($0 == row.path ? row.name : $0)).font(.caption).textSelection(.enabled) }
                    Text("Omit these rebuild folders from recovery").font(.headline)
                    ForEach(freeze.excluded, id: \.self) { Text($0).font(.caption).textSelection(.enabled) }
                }
            }
        }
    }
}
