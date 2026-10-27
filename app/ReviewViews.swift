import SwiftUI
import AppKit

struct ReviewOutcome: Decodable {
    struct Checkouts: Decodable {
        var count: Int; var paths: [String]
        init(count: Int = 0, paths: [String] = []) { self.count = count; self.paths = paths }
        private enum CodingKeys: String, CodingKey { case count, paths }
        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            paths = try values.decodeIfPresent([String].self, forKey: .paths) ?? []
            count = try values.decodeIfPresent(Int.self, forKey: .count) ?? paths.count
        }
    }
    struct Archives: Decodable {
        var count: Int; var location: String; var paths: [String]?
        init(count: Int = 0, location: String = "", paths: [String]? = nil) {
            self.count = count; self.location = location; self.paths = paths
        }
        private enum CodingKeys: String, CodingKey { case count, location, paths }
        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            paths = try values.decodeIfPresent([String].self, forKey: .paths)
            count = try values.decodeIfPresent(Int.self, forKey: .count) ?? paths?.count ?? 0
            location = try values.decodeIfPresent(String.self, forKey: .location) ?? ""
        }
    }
    struct PushedRef: Decodable {
        var ref: String; var sha: String; var destination: String
        init(ref: String, sha: String, destination: String) { self.ref = ref; self.sha = sha; self.destination = destination }
        private enum CodingKeys: String, CodingKey { case ref, sha, destination }
        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            ref = try values.decodeIfPresent(String.self, forKey: .ref) ?? ""
            sha = try values.decodeIfPresent(String.self, forKey: .sha) ?? ""
            destination = try values.decodeIfPresent(String.self, forKey: .destination) ?? ""
        }
    }
    struct HeldItem: Decodable {
        var path: String; var reason: String
        init(path: String, reason: String) { self.path = path; self.reason = reason }
        private enum CodingKeys: String, CodingKey { case path, reason }
        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            path = try values.decodeIfPresent(String.self, forKey: .path) ?? ""
            reason = try values.decodeIfPresent(String.self, forKey: .reason) ?? ""
        }
    }
    var checkoutsRemoved: Checkouts
    var bytesFreedEstimate: Int64
    var bytesUsedEstimate: Int64
    var bytesFreed: Int64?
    var archivesWritten: Archives
    var refsPushed: [PushedRef]
    var movedToTrash: Int
    var held: [HeldItem]
    var observed: Bool

    private enum CodingKeys: String, CodingKey {
        case checkoutsRemoved, bytesFreedEstimate, bytesUsedEstimate, bytesFreed, archivesWritten, refsPushed, movedToTrash, held, observed
    }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        checkoutsRemoved = try values.decodeIfPresent(Checkouts.self, forKey: .checkoutsRemoved) ?? Checkouts()
        bytesFreedEstimate = try values.decodeIfPresent(Int64.self, forKey: .bytesFreedEstimate) ?? 0
        bytesUsedEstimate = try values.decodeIfPresent(Int64.self, forKey: .bytesUsedEstimate) ?? 0
        bytesFreed = try values.decodeIfPresent(Int64.self, forKey: .bytesFreed)
        archivesWritten = try values.decodeIfPresent(Archives.self, forKey: .archivesWritten) ?? Archives()
        refsPushed = try values.decodeIfPresent([PushedRef].self, forKey: .refsPushed) ?? []
        movedToTrash = try values.decodeIfPresent(Int.self, forKey: .movedToTrash) ?? 0
        held = try values.decodeIfPresent([HeldItem].self, forKey: .held) ?? []
        observed = try values.decodeIfPresent(Bool.self, forKey: .observed) ?? false
    }

    var actionLabel: String { actionLabel(preservedProjects: nil) }
    func actionLabel(preservedProjects: Int?) -> String {
        var actions: [String] = []
        if movedToTrash > 0 { actions.append("Move \(movedToTrash) " + (movedToTrash == 1 ? "folder" : "folders") + " to Trash") }
        if checkoutsRemoved.count > 0 {
            actions.append("Freeze \(checkoutsRemoved.count) " + (checkoutsRemoved.count == 1 ? "checkout" : "checkouts"))
        }
        let preserved = preservedProjects ?? (checkoutsRemoved.count == 0 ? archivesWritten.count : 0)
        if preserved > 0 { actions.append(preserved == 1 ? "Preserve project" : "Preserve \(preserved) projects") }
        return actions.isEmpty ? "Run reviewed actions" : actions.joined(separator: " and ")
    }
}

struct OutcomeSummaryRow: View {
    let label: String
    var body: some View {
        Text(label).textSelection(.enabled)
            .accessibilityElement(children: .combine)
            .accessibilityLabel(Text(label))
    }
}

struct OutcomeSummaryView: View {
    let outcome: ReviewOutcome
    private var countQualifier: String { outcome.observed ? "observed" : "estimate" }
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(outcome.observed ? "Outcome" : "Expected outcome").font(.headline).accessibilityAddTraits(.isHeader)
            OutcomeSummaryRow(label: "Checkouts removed: \(outcome.checkoutsRemoved.count) (\(countQualifier))")
            ForEach(outcome.checkoutsRemoved.paths, id: \.self) { OutcomeSummaryRow(label: $0) }
            if outcome.observed, let freed = outcome.bytesFreed {
                if freed < 0 {
                    OutcomeSummaryRow(label: "Space used: \(bytes(freed == Int64.min ? Int64.max : -freed)) (observed)")
                } else {
                    OutcomeSummaryRow(label: "Space freed: \(bytes(freed)) (observed)")
                }
            } else {
                OutcomeSummaryRow(label: "Space freed: about \(bytes(outcome.bytesFreedEstimate)) (estimate)")
            }
            OutcomeSummaryRow(label: "Additional space used: about \(bytes(outcome.bytesUsedEstimate)) (estimate)")
            OutcomeSummaryRow(label: "Archives written: \(outcome.archivesWritten.count) (\(countQualifier)) · Freezer: \(outcome.archivesWritten.location)")
            ForEach(outcome.archivesWritten.paths ?? [], id: \.self) { OutcomeSummaryRow(label: $0) }
            OutcomeSummaryRow(label: "Refs pushed: \(outcome.refsPushed.count) (\(countQualifier))")
            ForEach(Array(outcome.refsPushed.enumerated()), id: \.offset) { _, ref in
                OutcomeSummaryRow(label: ref.ref + " · " + ref.sha + " → " + ref.destination)
            }
            OutcomeSummaryRow(label: "Folders moved to Trash: \(outcome.movedToTrash) (\(countQualifier))")
            OutcomeSummaryRow(label: "Not run: \(outcome.held.count) (\(countQualifier))")
            ForEach(Array(outcome.held.enumerated()), id: \.offset) { _, item in
                OutcomeSummaryRow(label: item.path + " · " + item.reason)
            }
        }.font(.caption).frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct CleanupReview: View {
    @EnvironmentObject var store: OrganizerStore
    let plan: CleanupPlan
    @LocalState var reviewed = false
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if plan.held == true { Text("This project is on Hold").foregroundStyle(.orange) }
            Text("Review cleanup · " + URL(fileURLWithPath: plan.items.first?.owner ?? plan.items.first?.path ?? "Project").lastPathComponent)
                .font(.title2).fontWeight(.semibold)
            if let outcome = plan.outcome { ScrollView { OutcomeSummaryView(outcome: outcome) }.frame(maxHeight: 120) }
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
                Button(plan.outcome?.actionLabel ?? "Move \(plan.items.count) \(plan.items.count == 1 ? "folder" : "folders") to Trash") {
                    let id = plan.id
                    store.plan = nil
                    store.action(["trash", id])
                }.buttonStyle(.borderedProminent).disabled(plan.items.isEmpty || !reviewed || store.busy || store.isDemo)
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
        }.padding(24).frame(width: sheetWidth(880), height: 520)
        .confirmationDialog("Restore this directory to its original location?", isPresented: $restoreConfirm, titleVisibility: .visible) {
            Button("Restore") { if let i = item { store.action(["restore", i.id]) } }
            Button("Cancel", role: .cancel) {}
        } message: { Text(item?.original ?? "") }
    }
}

