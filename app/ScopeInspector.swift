import SwiftUI

struct ScopeInspector: View {
    @EnvironmentObject var store: OrganizerStore
    @LocalState var basis = "Filtered projects"
    @LocalState var dimension = "Age"
    var projects: [Project] { basis == "Selected project" ? store.selected.map { [$0] } ?? [] : store.visible }
    var total: Int64 { projects.reduce(0) { $0 + $1.size } }
    var cacheBytes: Int64 { projects.flatMap { SpaceData.slices($0, cleanupOnly: true) }.reduce(0) { $0 + $1.size } }
    func groupName(_ p: Project) -> String {
        switch dimension {
        case "Technology": return SpaceData.profile(p)
        case "Age": return SpaceData.age(p)
        case "Activity": return SpaceData.activity(p)
        default: return p.kind
        }
    }
    var groups: [(name: String, bytes: Int64)] {
        var totals: [String: Int64] = [:]
        if dimension == "Folder type" {
            for project in projects {
                for slice in SpaceData.slices(project) { totals[slice.kind, default: 0] += slice.size }
            }
        } else {
            for project in projects { totals[groupName(project), default: 0] += project.size }
        }
        let result: [(name: String, bytes: Int64)] = totals.filter { $0.value > 0 }.map { (name: $0.key, bytes: $0.value) }
        return result.sorted { $0.bytes == $1.bytes ? $0.name < $1.name : $0.bytes > $1.bytes }
    }
    var keys: [String] {
        if dimension == "Folder type" {
            return Array(Set((store.snapshot?.projects ?? []).flatMap { SpaceData.slices($0) }.map(\.kind))).sorted()
        }
        return Array(Set((store.snapshot?.projects ?? []).map(groupName))).sorted()
    }
    func color(_ name: String) -> Color {
        if dimension == "Age" { return IglooPalette.age(name) }
        if dimension == "Activity" { return IglooPalette.activity(name) }
        if name == "Other project files" || name.hasPrefix("Unknown") { return .gray }
        return IglooPalette.category(keys.firstIndex(of: name) ?? 0, count: keys.count)
    }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("Current scope").font(.headline)
                Picker("Scope", selection: $basis) {
                    Text("Filtered projects").tag("Filtered projects")
                    Text("Selected project").tag("Selected project")
                }.labelsHidden()
                Text(basis == "Selected project" ? store.selected?.name ?? "No project selected" :
                     noun(projects.count, "project") + " · " + store.filter)
                    .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                Picker("Break down by", selection: $dimension) {
                    ForEach(["Project type", "Technology", "Age", "Activity", "Folder type"], id: \.self) { Text($0) }
                }
                if dimension == "Age" || dimension == "Activity" {
                    Text(dimension == "Age" ? "Warm: recent Git activity · Cold: older projects. Gray: unknown." :
                         "Warm: active session · Cold: no active session. Gray: unknown.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if total > 0 {
                    ZStack {
                        ForEach(Array(groups.enumerated()), id: \.element.name) { i, group in
                            let start = Double(groups.prefix(i).reduce(Int64(0)) { $0 + $1.bytes }) / Double(total)
                            let end = start + Double(group.bytes) / Double(total)
                            SpaceSector(start: start, end: end, inner: 0.69, outer: 0.98)
                                .fill(color(group.name))
                                .help(group.name + " · " + bytes(group.bytes))
                                .accessibilityLabel(group.name + ", " + bytes(group.bytes))
                        }
                        VStack(spacing: 4) {
                            Text(bytes(total)).font(.title3).fontWeight(.semibold).monospacedDigit()
                            Text("allocated").font(.caption).foregroundStyle(.secondary)
                        }
                    }.frame(height: 205)
                    VStack(spacing: 10) {
                        ForEach(Array(groups.prefix(6)), id: \.name) { group in
                            HStack(spacing: 7) {
                                Circle().fill(color(group.name)).frame(width: 7, height: 7)
                                Text(group.name).lineLimit(1).truncationMode(.middle).help(group.name)
                                Spacer(minLength: 4)
                                Text(bytes(group.bytes)).monospacedDigit()
                            }.font(.caption)
                        }
                        if groups.count > 6 {
                            HStack {
                                Text("Other \(groups.count - 6) groups")
                                Spacer()
                                Text(bytes(groups.dropFirst(6).reduce(0) { $0 + $1.bytes }))
                            }.font(.caption).foregroundStyle(.secondary)
                        }
                    }
                } else {
                    ContentUnavailableView("No measured space", systemImage: "chart.pie",
                        description: Text("Choose a project or adjust the list filters.")).frame(height: 190)
                }
                Divider()
                VStack(spacing: 11) {
                    fact("Projects", "\(projects.count)")
                    fact("Eligible caches", bytes(cacheBytes))
                    fact("Active", "\(projects.filter(\.active).count)")
                    fact("Quiet", "\(projects.filter { !$0.active && ($0.ageDays ?? -1) >= store.quietDays }.count)")
                    fact("Needs preservation", "\(projects.filter(\.needsPreservation).count)")
                        .help("Counts projects with local work not verified on their remote, including uncommitted changes, stashes, uncovered branches, detached commits, unverified refs and local files. Non-Git folders are included because their contents are not on a remote.")
                    fact("Uncommitted work", "\(projects.filter { !$0.changes.isEmpty }.count)")
                }
                Button("Open full Space Explorer") {
                    store.explorerProjectIDs = Set(projects.map(\.id))
                    store.spaceShown = true
                }.disabled(projects.isEmpty)
                Text("Updates with the current filters and search. Sizes and activity are catalog estimates. Technology combinations count each project once.")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(16)
        }.background(Color(nsColor: .windowBackgroundColor))
    }
    private func fact(_ name: String, _ value: String) -> some View {
        HStack { Text(name).foregroundStyle(.secondary); Spacer(); Text(value).monospacedDigit() }.font(.callout)
    }
}
