import SwiftUI
import AppKit

let projectTypeNames = ["Website", "Web app", "API / service", "Native app", "Command-line tool",
                        "Library / package", "Qlik extension", "Data / notebooks", "Scripts / automation", "Unclassified"]

func checkoutName(_ path: String, project: Project) -> String {
    path == project.path ? "Main checkout" : URL(fileURLWithPath: path).lastPathComponent
}
func checkoutForFile(_ path: String, project: Project) -> String {
    project.worktrees.map(\.path).filter { path.hasPrefix($0 + "/") }.max(by: { $0.count < $1.count }) ?? project.path
}
func fileName(_ path: String) -> String { URL(fileURLWithPath: path).lastPathComponent }
func changeName(_ status: String) -> String {
    if status == "??" { return "Untracked" }
    if status.contains("U") || ["AA", "DD"].contains(status) { return "Conflict" }
    if status.contains("D") { return "Deleted" }
    if status.contains("R") { return "Renamed" }
    if status.contains("A") { return "Added" }
    if status.contains("M") { return "Modified" }
    return "Changed"
}

struct FilePathCell: View {
    let path: String
    var base = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(fileName(path)).lineLimit(1).truncationMode(.middle)
            if relative(path, to: base) != fileName(path) {
                Text(relative(path, to: base)).font(.caption).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle)
            }
        }.padding(.vertical, 3).help(path)
    }
}
struct DetailSearch: View {
    let prompt: String
    @Binding var text: String
    var body: some View {
        TextField(prompt, text: $text).textFieldStyle(.roundedBorder)
            .accessibilityLabel(prompt)
    }
}
struct ReviewLink: View {
    let title: String
    let subtitle: String
    let count: String
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(title).font(.callout).fontWeight(.medium)
                    Text(subtitle).font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.leading)
                }
                Spacer(minLength: 8)
                Text(count).font(.callout).monospacedDigit()
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(.secondary)
            }.padding(.vertical, 8).contentShape(Rectangle())
        }.buttonStyle(.plain).accessibilityLabel(title + ", " + count + ". " + subtitle)
    }
}

struct OverviewView: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    var open: (String, String) -> Void
    var historyCount: Int { project.stashes.count + project.refs.filter { !$0.verified }.count + (project.detachedHeads ?? []).count }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(project.overviewSummary.title).font(.headline)
                    Text(project.overviewSummary.detail).font(.callout).foregroundStyle(.secondary)
                }
                Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 10) {
                    fact("Remote", project.remoteStatus)
                    fact("Git activity", project.ageDays.map { "\($0) days ago" } ?? "Unknown")
                    fact("Last checked", shortDate(project.verifiedAt))
                    fact("Worktrees", "\(project.worktrees.filter(\.exists).count) present · \(project.worktrees.filter { !$0.exists }.count) missing")
                    fact("Eligible caches", bytes(project.cleanupSize))
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text("Review next").font(.headline).padding(.bottom, 6)
                    if !project.changes.isEmpty {
                        ReviewLink(title: "Uncommitted source", subtitle: "Group changes by checkout and review a commit.", count: "\(project.changes.count)") { open("Branches", "Changes") }
                        Divider()
                    }
                    if project.uncovered + project.unknown > 0 {
                        ReviewLink(title: "Branch coverage", subtitle: "\(project.uncovered) with uncovered history · \(project.unknown) unverified", count: "\(project.uncovered + project.unknown)") { open("Branches", "Branches") }
                        Divider()
                    }
                    ReviewLink(title: "Local files", subtitle: "Configuration, data and ignored entries stay outside an ordinary source push.", count: "\(project.localFiles.count)") { open("Local files", "") }
                    if historyCount > 0 {
                        Divider()
                        ReviewLink(title: "Saved local history", subtitle: "Stashes, detached commits, tags and notes.", count: "\(historyCount)") { open("Branches", "Saved history") }
                    }
                    if project.worktrees.count > 1 {
                        Divider()
                        ReviewLink(title: "Worktrees", subtitle: "Inspect changes, missing paths and locked checkouts.", count: "\(project.worktrees.count)") { open("Worktrees", "") }
                    }
                    if project.cleanupSize > 0 {
                        Divider()
                        ReviewLink(title: "Cache cleanup", subtitle: "Review eligible folders before moving them to Trash.", count: bytes(project.cleanupSize)) { open("Cleanup", "") }
                    }
                }
                Divider()
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Text("Project type").font(.headline)
                        Spacer()
                        Menu {
                            Button("Automatic: " + (project.detectedType ?? project.kind)) {
                                store.action(["categorize", project.path, "Automatic"])
                            }
                            Divider()
                            ForEach(projectTypeNames, id: \.self) { kind in
                                Button(kind) { store.action(["categorize", project.path, kind]) }
                            }
                        } label: { Text(project.kind) }.disabled(store.busy)
                    }
                    if !project.technologyText.isEmpty {
                        LabeledContent("Technology", value: project.technologyText).font(.callout)
                    }
                    Text((project.typeEvidence ?? ["Run Analyze Projects to detect the project type."]).joined(separator: " · ") +
                         ((project.detectedType ?? project.kind) != project.kind ? " · Manually chosen; automatic type: " + (project.detectedType ?? "Unclassified") : " · Best-effort suggestion; you can change it."))
                        .font(.caption).foregroundStyle(.secondary)
                }
                if !project.holds.isEmpty {
                    DisclosureGroup("All preservation checks (\(project.holds.count))") {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(project.holds, id: \.self) { Text($0).font(.callout) }
                        }.padding(.top, 8).frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                if !project.processes.isEmpty {
                    DisclosureGroup("Observed sessions (\(project.processes.count))") {
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(project.processes, id: \.self) { Text($0).font(.caption) }
                        }.padding(.top, 8).frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                Text("Sizes are allocated-byte estimates. Git dates and process working directories estimate activity; they do not prove last use.")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
        }
    }
    private func fact(_ label: String, _ value: String) -> some View {
        GridRow { Text(label).foregroundStyle(.secondary); Text(value).textSelection(.enabled) }.font(.callout)
    }
}

struct ProjectGitView: View {
    let project: Project
    @Binding var section: String
    var body: some View {
        if !project.git {
            ContentUnavailableView("No root Git repository", systemImage: "folder.badge.questionmark",
                                   description: Text("Review this folder's files before archiving or removing it."))
        } else {
            VStack(spacing: 0) {
                Picker("Source control section", selection: $section) {
                    ForEach(["Changes", "Branches", "Saved history", "Remotes"], id: \.self) { Text($0) }
                }.pickerStyle(.segmented).labelsHidden().padding(16)
                Divider()
                Group {
                    switch section {
                    case "Changes": ChangesView(project: project)
                    case "Saved history": SavedHistoryView(project: project)
                    case "Remotes": RemotesView(project: project)
                    default: BranchesView(project: project)
                    }
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }
}
struct ChangesView: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    @LocalState var search = ""
    @LocalState var checkout = ""
    @LocalState var selected: String?
    var rows: [ChangedFile] {
        project.changes.filter {
            (checkout.isEmpty || $0.checkout == checkout) &&
            (search.isEmpty || ($0.path + " " + $0.checkout + " " + changeName($0.status)).localizedCaseInsensitiveContains(search))
        }.sorted { $0.checkout == $1.checkout ? $0.path < $1.path : $0.checkout < $1.checkout }
    }
    var file: ChangedFile? { rows.first { $0.id == selected } }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                DetailSearch(prompt: "Find a changed file or checkout", text: $search)
                if Set(project.changes.map(\.checkout)).count > 1 {
                    Picker("Checkout", selection: $checkout) {
                        Text("All checkouts").tag("")
                        ForEach(Array(Set(project.changes.map(\.checkout))).sorted(), id: \.self) {
                            Text(checkoutName($0, project: project)).tag($0)
                        }
                    }.labelsHidden().frame(maxWidth: 200)
                }
            }
            Text("\(rows.count) of \(project.changes.count) changes").font(.caption).foregroundStyle(.secondary)
            Table(rows, selection: $selected) {
                TableColumn("File") { FilePathCell(path: $0.path) }.width(min: 180)
                TableColumn("Change") { Text(changeName($0.status)).help($0.status) }.width(85)
                TableColumn("Checkout") { Text(checkoutName($0.checkout, project: project)).lineLimit(1).help($0.checkout) }.width(min: 110)
            }.overlay {
                if rows.isEmpty {
                    ContentUnavailableView(project.changes.isEmpty ? "No uncommitted source changes" : "No matching changes",
                        systemImage: "doc.badge.checkmark",
                        description: Text(project.changes.isEmpty ? "Ignored configuration and data are listed in Local files." : "Try another checkout or search."))
                }
            }
            if let file = file {
                Text(file.checkout + "/" + file.path + "\nGit status: " + file.status).font(.caption).textSelection(.enabled)
            }
            HStack {
                Button("Show selected") { if let f = file { store.reveal(f.checkout + "/" + f.path) } }.disabled(file == nil)
                Spacer()
                Button("Review source commit…") { store.commitSetup = project }.disabled(store.busy || project.changes.isEmpty)
            }
        }.padding(16)
    }
}
struct BranchesView: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    @LocalState var selected: String?
    @LocalState var remoteName = ""
    @LocalState var search = ""
    @LocalState var scope = "All branches"
    var rows: [Branch] {
        project.branches.filter {
            (search.isEmpty || ($0.name + " " + $0.upstream).localizedCaseInsensitiveContains(search)) &&
            (scope == "All branches" || (scope == "Needs review" && ($0.uncovered != 0 || $0.privateHistory)) ||
             (scope == "Covered remotely" && $0.uncovered == 0) || (scope == "Private history" && $0.privateHistory))
        }.sorted {
            let a = $0.uncovered == 0 && !$0.privateHistory ? 1 : 0
            let b = $1.uncovered == 0 && !$1.privateHistory ? 1 : 0
            return a == b ? $0.name.localizedStandardCompare($1.name) == .orderedAscending : a < b
        }
    }
    var branch: Branch? { rows.first { $0.id == selected } }
    var remote: Remote? { project.remotes.first { $0.name == remoteName } ?? project.remotes.first }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                DetailSearch(prompt: "Find a branch or upstream", text: $search)
                Picker("Coverage", selection: $scope) {
                    ForEach(["All branches", "Needs review", "Covered remotely", "Private history"], id: \.self) { Text($0) }
                }.labelsHidden().frame(maxWidth: 185)
            }
            Text("\(rows.count) of " + noun(project.branches.count, "branch", "branches") + " · \(project.uncovered) uncovered · \(project.unknown) unverified")
                .font(.caption).foregroundStyle(.secondary)
            Table(rows, selection: $selected) {
                TableColumn("Branch") { row in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(row.name).lineLimit(1).truncationMode(.middle).help(row.name)
                        if row.privateHistory { Text("Private / archive history").font(.caption).foregroundStyle(.secondary) }
                    }
                }.width(min: 180)
                TableColumn("Remote coverage", value: \.coverage).width(min: 145)
                TableColumn("Updated") { Text(shortDate($0.date)) }.width(85)
            }.overlay {
                if rows.isEmpty { ContentUnavailableView("No matching branches", systemImage: "arrow.triangle.branch", description: Text("Try a different coverage filter or search.")) }
            }
            if let b = branch {
                VStack(alignment: .leading, spacing: 5) {
                    Text(b.name).font(.callout).fontWeight(.medium).textSelection(.enabled)
                    Text("Upstream: " + (b.upstream.isEmpty ? "None" : b.upstream) + " · Cached state: " + (b.tracking.isEmpty ? "—" : b.tracking)).font(.caption)
                    let hash = shortGitHash(b.sha)
                    if !hash.isEmpty { Text(hash).font(.caption).textSelection(.enabled) }
                }
            }
            DisclosureGroup("How branch coverage works") {
                Text("Coverage compares local commits with currently advertised remote history. Unverified history may need Fetch & verify. Cached ahead/behind can be stale. Private and archive branches require manual review.")
                    .font(.caption).foregroundStyle(.secondary).padding(.top, 6)
            }.font(.caption)
            HStack {
                Picker("Destination", selection: $remoteName) { ForEach(project.remotes, id: \.name) { Text($0.name).tag($0.name) } }.frame(maxWidth: 230)
                Spacer()
                Button("Push selected branch…") {
                    if let b = branch, let r = remote { store.pushRequest = PushRequest(project: project, branch: b, remote: r) }
                }.disabled(store.busy || branch == nil || branch?.privateHistory == true || branch?.uncovered == 0 || remote == nil)
            }
        }.padding(16).onAppear { remoteName = project.remotes.first?.name ?? "" }
    }
}

struct SavedHistoryRow: Identifiable {
    var id: String; var name: String; var kind: String; var sha: String; var status: String; var path: String
}
struct SavedHistoryView: View {
    let project: Project
    @LocalState var search = ""
    @LocalState var selected: String?
    var rows: [SavedHistoryRow] {
        let stashes = project.stashes.enumerated().map { i, sha in
            SavedHistoryRow(id: "stash:\(i):" + sha, name: "stash@{\(i)}", kind: "Stash", sha: sha, status: "Local recovery", path: "")
        }
        let refs = project.refs.map {
            SavedHistoryRow(id: "ref:" + $0.name, name: $0.name, kind: "Tag / note", sha: $0.sha, status: $0.verified ? "Verified remotely" : "Needs preservation", path: "")
        }
        let heads = (project.detachedHeads ?? []).map {
            SavedHistoryRow(id: "head:" + $0.path, name: checkoutName($0.path, project: project), kind: "Detached", sha: $0.sha,
                            status: $0.uncovered.map { $0 == 0 ? "Covered remotely" : "\($0) commits to preserve" } ?? "Unverified", path: $0.path)
        }
        return (stashes + refs + heads).filter { search.isEmpty || ($0.name + " " + $0.kind + " " + $0.status).localizedCaseInsensitiveContains(search) }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            DetailSearch(prompt: "Find a stash, tag, note or detached checkout", text: $search)
            Table(rows, selection: $selected) {
                TableColumn("Kind", value: \.kind).width(95)
                TableColumn("Name", value: \.name).width(min: 170)
                TableColumn("Preservation", value: \.status).width(min: 140)
            }.overlay {
                if rows.isEmpty { ContentUnavailableView("No matching saved history", systemImage: "tray", description: Text("Stashes, refs and detached checkouts appear here when present.")) }
            }
            if let row = rows.first(where: { $0.id == selected }) {
                let hash = shortGitHash(row.sha)
                if !hash.isEmpty { Text(hash).font(.caption).textSelection(.enabled) }
                if !row.path.isEmpty { Text(row.path).font(.caption).textSelection(.enabled) }
            }
            Text("An ordinary source push does not preserve stashes or reflogs. Freezing includes them in local Git recovery; private and archive refs stay local.")
                .font(.caption).foregroundStyle(.secondary)
        }.padding(16)
    }
}
struct RemotesView: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                if project.remotes.isEmpty {
                    ContentUnavailableView("No remote connected", systemImage: "network.slash", description: Text("Connect an existing repository or create a private GitHub repository."))
                }
                ForEach(project.remotes, id: \.name) { remote in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(remote.name).font(.headline)
                        LabeledContent("Fetch") { Text(remote.url).textSelection(.enabled) }.font(.callout)
                        LabeledContent("Push") { Text(remote.pushURL).textSelection(.enabled) }.font(.callout)
                    }
                    Divider()
                }
                Button("Connect remote…") { store.remoteSetup = project }.disabled(store.busy)
                Text("Last verified: " + shortDate(project.verifiedAt) + " · " + project.remoteStatus).font(.caption).foregroundStyle(.secondary)
            }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
struct WorktreesView: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    @LocalState var selected: String?
    @LocalState var search = ""
    @LocalState var scope = "All worktrees"
    var rows: [Worktree] {
        project.worktrees.filter {
            (search.isEmpty || ($0.path + " " + $0.branch).localizedCaseInsensitiveContains(search)) &&
            (scope == "All worktrees" || scope == "Changed" && $0.changes > 0 || scope == "Missing" && !$0.exists || scope == "Locked" && $0.locked)
        }.sorted { $0.changes == $1.changes ? $0.path < $1.path : $0.changes > $1.changes }
    }
    var tree: Worktree? { rows.first { $0.id == selected } }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                DetailSearch(prompt: "Find a worktree or branch", text: $search)
                Picker("State", selection: $scope) { ForEach(["All worktrees", "Changed", "Missing", "Locked"], id: \.self) { Text($0) } }.labelsHidden().frame(maxWidth: 160)
            }
            Text("\(rows.count) of " + noun(project.worktrees.count, "registered worktree")).font(.caption).foregroundStyle(.secondary)
            Table(rows, selection: $selected) {
                TableColumn("Checkout") { FilePathCell(path: $0.path, base: store.root.path) }.width(min: 190)
                TableColumn("Branch") { Text($0.branch).lineLimit(1).truncationMode(.middle).help($0.branch) }.width(min: 120)
                TableColumn("State") { Text(!$0.exists ? "Missing" : $0.locked ? "Locked" : $0.changes > 0 ? "\($0.changes) changes" : "Clean*") }.width(90)
            }.overlay {
                if rows.isEmpty { ContentUnavailableView("No matching worktrees", systemImage: "folder", description: Text("Try another state filter or search.")) }
            }
            if let tree = tree { Text(tree.path).font(.caption).textSelection(.enabled) }
            HStack {
                if rows.contains(where: { $0.exists && !$0.locked && $0.changes == 0 }) {
                    Text("*Clean covers tracked/untracked state. Ignored files can still need preservation.").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Show selected") { if let t = tree { store.reveal(t.path) } }.disabled(tree?.exists != true)
            }
        }.padding(16)
    }
}
struct LocalFilesView: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    @LocalState var mode = "Config & data"
    @LocalState var search = ""
    @LocalState var scope = "All matches"
    @LocalState var selected: String?
    var files: [LocalFile] {
        project.localFiles.filter {
            (search.isEmpty || $0.path.localizedCaseInsensitiveContains(search)) &&
            (scope == "All matches" || scope == "Local only" && !$0.tracked && !$0.template ||
             scope == "Tracked" && $0.tracked || scope == "Templates" && $0.template)
        }
    }
    var ignored: [IgnoredFile] { project.ignoredFiles.filter { search.isEmpty || ($0.path + " " + $0.checkout).localizedCaseInsensitiveContains(search) } }
    var selectedPath: String? {
        mode == "Config & data" ? files.first { $0.id == selected }?.path :
        ignored.first { $0.id == selected }.map { $0.checkout + "/" + $0.path }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Picker("File collection", selection: $mode) {
                Text("Config & data (\(project.localFiles.count))").tag("Config & data")
                Text("Ignored entries (\(project.ignoredFiles.count))").tag("Ignored")
            }.pickerStyle(.segmented).labelsHidden()
            HStack {
                DetailSearch(prompt: "Find a filename or checkout", text: $search)
                if mode == "Config & data" {
                    Picker("Git status", selection: $scope) {
                        ForEach(["All matches", "Local only", "Tracked", "Templates"], id: \.self) { Text($0) }
                    }.labelsHidden().frame(maxWidth: 160)
                }
            }
            if mode == "Config & data" {
                Text("\(files.count) of \(project.localFiles.count) filename matches").font(.caption).foregroundStyle(.secondary)
                Table(files, selection: $selected) {
                    TableColumn("File") { FilePathCell(path: $0.path, base: checkoutForFile($0.path, project: project)) }.width(min: 200)
                    TableColumn("Checkout") { Text(checkoutName(checkoutForFile($0.path, project: project), project: project)).lineLimit(1).truncationMode(.middle) }.width(min: 110)
                    TableColumn("Git state") { file in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(file.tracked ? "Tracked" : file.ignored ? "Ignored" : "Local")
                            if file.template { Text("Template").font(.caption).foregroundStyle(.secondary) }
                        }
                    }.width(80)
                }.overlay {
                    if files.isEmpty { ContentUnavailableView("No matching local files", systemImage: "doc", description: Text("Try another status filter or search. Filename checks do not inspect file values.")) }
                }
            } else {
                Text("\(ignored.count) of \(project.ignoredFiles.count) ignored entries").font(.caption).foregroundStyle(.secondary)
                Table(ignored, selection: $selected) {
                    TableColumn("Ignored entry") { FilePathCell(path: $0.path) }.width(min: 240)
                    TableColumn("Checkout") { Text(checkoutName($0.checkout, project: project)).lineLimit(1).help($0.checkout) }.width(min: 120)
                }.overlay {
                    if ignored.isEmpty { ContentUnavailableView("No matching ignored entries", systemImage: "doc", description: Text("Try another filename or checkout.")) }
                }
            }
            HStack {
                if let path = selectedPath { Text(path).font(.caption).lineLimit(2).textSelection(.enabled) }
                Spacer()
                Button("Show selected") { if let path = selectedPath { store.reveal(path) } }.disabled(selectedPath == nil)
            }
            Text("Filename checks only; values and secret contents are not displayed. Ignored entries may include valuable source assets, data or credentials.")
                .font(.caption).foregroundStyle(.secondary)
        }.padding(16).onChange(of: mode) { selected = nil }
    }
}
struct CleanupView: View {
    @EnvironmentObject var store: OrganizerStore
    let project: Project
    @LocalState var selected = Set<String>()
    @LocalState var search = ""
    @LocalState var scope = "All folders"
    var rows: [Candidate] {
        project.candidates.filter {
            (search.isEmpty || ($0.path + " " + $0.kind).localizedCaseInsensitiveContains(search)) &&
            (scope == "All folders" || scope == "Eligible" && $0.eligible || scope == "Held for review" && !$0.eligible)
        }.sorted { $0.size > $1.size }
    }
    var chosen: [Candidate] { project.candidates.filter { selected.contains($0.id) } }
    var selectedSize: Int64 {
        chosen.filter { item in !chosen.contains { $0.id != item.id && item.path.hasPrefix($0.path + "/") } }
            .reduce(0) { $0 + $1.size }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                DetailSearch(prompt: "Find a cache or output folder", text: $search)
                Picker("Eligibility", selection: $scope) { ForEach(["All folders", "Eligible", "Held for review"], id: \.self) { Text($0) } }.labelsHidden().frame(maxWidth: 175)
            }
            HStack {
                Text(noun(rows.count, "folder") + " · \(bytes(project.cleanupSize)) eligible").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Select visible eligible") { selected.formUnion(rows.filter(\.eligible).map(\.id)) }.disabled(store.busy)
                if !selected.isEmpty { Button("Clear") { selected = [] } }
            }
            Table(rows, selection: $selected) {
                TableColumn("Directory") { FilePathCell(path: $0.path, base: project.path) }.width(min: 190)
                TableColumn("Size") { Text(bytes($0.size)).monospacedDigit() }.width(85)
                TableColumn("Review") { Text($0.eligible ? "Eligible cache" : $0.reason).lineLimit(2).help($0.reason) }.width(min: 160)
            }.overlay {
                if rows.isEmpty { ContentUnavailableView("No matching cleanup folders", systemImage: "externaldrive", description: Text("Try another filter or refresh the project. Active projects are held for review.")) }
            }
            Text("Preview rechecks ignored/tracked state, running sessions and local configuration. Files move to Trash with a restore record.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Text("\(chosen.count) selected · \(bytes(selectedSize))").font(.callout).monospacedDigit()
                Spacer()
                Button("Review selected cleanup…") { store.preview(chosen.map(\.id).sorted()) }
                    .buttonStyle(WizardPrimaryButtonStyle()).disabled(store.busy || chosen.isEmpty)
            }
        }.padding(16)
    }
}
