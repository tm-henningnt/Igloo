import SwiftUI

struct OrganizerNavigation: View {
    @EnvironmentObject var store: OrganizerStore
    var compact = false
    private let projects = [("All projects", "folder"), ("Quiet projects", "moon"), ("Active sessions", "bolt")]
    private let reviews = [("Needs preservation", "tray.full"), ("Cleanup opportunities", "externaldrive.badge.minus"),
                           ("No remote", "network.slash"), ("No Git metadata", "folder.badge.questionmark"), ("On hold", "archivebox")]
    var body: some View {
        if compact {
            VStack(spacing: 8) {
                ScrollView {
                    VStack(spacing: 8) {
                        railFilters(projects)
                        Divider().padding(.horizontal, 10)
                        railFilters(reviews)
                        Divider().padding(.horizontal, 10)
                        railButton("Tasks", "checklist") { store.tasksShown = true }
                        railButton("Cleanup wizard", "wand.and.stars") { store.wizardShown = true }
                        railButton("Space explorer", "chart.pie") { store.explorerProjectIDs = nil; store.spaceShown = true }
                        railButton("GitHub repositories", "network") { store.repositoriesShown = true }
                        Divider().padding(.horizontal, 10)
                        railButton("Freezer", "snowflake") { store.vaultShown = true }
                        railButton("Trash & restore history", "clock.arrow.circlepath") { store.historyShown = true }
                    }.padding(.top, 10)
                }
                railButton("How to use", "questionmark.circle") { store.helpShown = true }
                    .padding(.bottom, 10)
            }.frame(width: 48).background(.bar).disabled(store.busy)
        } else {
            VStack(spacing: 0) {
                List(selection: $store.filter) {
                    Section("Projects") { filterRows(projects) }
                    Section("Needs review") { filterRows(reviews) }
                    Section("Tools") {
                        Button { store.tasksShown = true } label: { Label("Tasks", systemImage: "checklist").frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle()) }
                        Button { store.wizardShown = true } label: { Label("Cleanup wizard", systemImage: "wand.and.stars").frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle()) }
                        Button { store.explorerProjectIDs = nil; store.spaceShown = true } label: { Label("Space explorer", systemImage: "chart.pie").frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle()) }
                        Button { store.repositoriesShown = true } label: { Label("GitHub repositories", systemImage: "network").frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle()) }
                    }.buttonStyle(.plain)
                    Section("Recovery") {
                        Button { store.vaultShown = true } label: { Label("Freezer", systemImage: "snowflake").frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle()) }
                        Button { store.historyShown = true } label: { Label("Trash & restore history", systemImage: "clock.arrow.circlepath").frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle()) }
                    }.buttonStyle(.plain)
                }.listStyle(.sidebar)
                if store.filter == "Quiet projects" {
                    Divider()
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Quiet after").font(.caption).foregroundStyle(.secondary)
                        Stepper("\(store.quietDays) days", value: $store.quietDays, in: 1...3650)
                            .onChange(of: store.quietDays) { store.persistQuietDays() }
                            .help("Estimated from Git activity. Configure independent cleanup and freeze ages in the wizard.")
                    }.padding(12)
                }
                Divider()
                Button { store.helpShown = true } label: {
                    Label("How to use", systemImage: "questionmark.circle")
                        .frame(maxWidth: .infinity, alignment: .leading)
                }.buttonStyle(.plain).padding(14)
            }.navigationTitle("Projects").navigationSplitViewColumnWidth(min: 210, ideal: 224, max: 270)
                .disabled(store.busy)
        }
    }
    @ViewBuilder private func filterRows(_ items: [(String, String)]) -> some View {
        ForEach(items, id: \.0) { name, icon in Label(name, systemImage: icon).tag(name) }
    }
    @ViewBuilder private func railFilters(_ items: [(String, String)]) -> some View {
        ForEach(items, id: \.0) { name, icon in
            railButton(name, icon, selected: store.filter == name) { store.filter = name }
        }
    }
    private func railButton(_ title: String, _ icon: String, selected: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon).symbolRenderingMode(.monochrome)
                .frame(width: 34, height: 30).contentShape(Rectangle())
                .background(selected ? Color.primary.opacity(0.10) : Color.clear, in: RoundedRectangle(cornerRadius: 6))
        }.buttonStyle(.plain).foregroundStyle(selected ? .primary : .secondary)
            .help(title).accessibilityLabel(title)
    }
}

// ProjectRow supplies the blockers decoded from the catalog and the project's Hold state.
struct ProjectBlockerBadges: View {
    let blockers: [FreezeBlocker]
    let held: Bool
    private func label(_ code: String) -> String {
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
    var body: some View {
        HStack(spacing: 5) {
            if let first = blockers.first {
                HStack(spacing: 4) {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange).accessibilityHidden(true)
                    Text("Blocked: " + label(first.code) + (blockers.count > 1 ? " +\(blockers.count - 1)" : ""))
                        .lineLimit(1).truncationMode(.tail)
                        .help(blockers.map(\.message).joined(separator: "\n"))
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel(Text("Blocked: " + blockers.map(\.message).joined(separator: " ")))
            }
            if held { Text("On hold").fixedSize().accessibilityLabel(Text("On hold")) }
        }.font(.caption)
    }
}
