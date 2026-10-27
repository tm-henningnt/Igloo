import SwiftUI

struct SpaceSlice: Identifiable {
    var project: Project
    var kind: String
    var size: Int64
    var id: String { project.id + "|" + kind }
}

struct SpaceGroup: Identifiable {
    var id: String
    var name: String
    var slices: [SpaceSlice]
    var size: Int64
}

enum SpaceData {
    static func slices(_ project: Project, cleanupOnly: Bool = false) -> [SpaceSlice] {
        var accepted: [String] = []
        var totals: [String: Int64] = [:]
        var remaining = max(0, project.size)
        for item in project.candidates.sorted(by: { $0.path.count < $1.path.count }) {
            guard !cleanupOnly || item.eligible else { continue }
            guard !accepted.contains(where: { item.path == $0 || item.path.hasPrefix($0 + "/") }) else { continue }
            accepted.append(item.path)
            let amount = min(remaining, max(0, item.size))
            remaining -= amount
            totals[item.kind, default: 0] += amount
        }
        if !cleanupOnly { totals["Other project files", default: 0] += remaining }
        return totals.filter { $0.value > 0 }.map { SpaceSlice(project: project, kind: $0.key, size: $0.value) }
            .sorted { $0.size == $1.size ? $0.kind < $1.kind : $0.size > $1.size }
    }
    static func profile(_ project: Project) -> String {
        project.technologyText.isEmpty ? "Unknown technology" : project.technologyText
    }
    static func groups(_ slices: [SpaceSlice], by dimension: String) -> [SpaceGroup] {
        Dictionary(grouping: slices, by: {
            dimension == "Projects" ? $0.project.id : dimension == "Project type" ? $0.project.kind : profile($0.project)
        }).map { key, parts in
            SpaceGroup(id: key, name: dimension == "Projects" ? parts[0].project.name : key,
                       slices: parts, size: parts.reduce(0) { $0 + $1.size })
        }.sorted { $0.size == $1.size ? $0.id < $1.id : $0.size > $1.size }
    }
    static func age(_ p: Project) -> String {
        guard let days = p.ageDays else { return "Unknown age" }
        if days < 30 { return "Under 30 days" }
        if days < 90 { return "30–89 days" }
        if days < 365 { return "90–364 days" }
        return "1 year or older"
    }
    static func activity(_ p: Project) -> String {
        !p.activityKnown ? "Activity unknown" : p.active ? "Active session" : "No active session"
    }
}

/// Warm means recent Git activity; cold means an older preserved-work candidate.
/// Unknown metadata is neutral rather than presented as cold.
enum IglooPalette {
    static let stops: [(Double, Double, Double)] = [
        (0.76, 0.30, 0.17), (0.35, 0.48, 0.56), (0.17, 0.43, 0.62), (0.12, 0.29, 0.52)
    ]
    static func agePosition(_ key: String) -> Double? {
        switch key {
        case "Under 30 days": return 0
        case "30–89 days": return 1.0 / 3
        case "90–364 days": return 2.0 / 3
        case "1 year or older": return 1
        default: return nil
        }
    }
    static func thermal(_ position: Double) -> Color {
        let location = min(1, max(0, position)) * Double(stops.count - 1)
        let lower = min(stops.count - 2, Int(location))
        let fraction = location - Double(lower)
        let a = stops[lower], b = stops[lower + 1]
        return Color(red: a.0 + (b.0 - a.0) * fraction,
                     green: a.1 + (b.1 - a.1) * fraction,
                     blue: a.2 + (b.2 - a.2) * fraction)
    }
    static func age(_ key: String) -> Color { agePosition(key).map(thermal) ?? .gray }
    static func activity(_ key: String) -> Color {
        key == "Active session" ? thermal(0) : key == "No active session" ? thermal(0.8) : .gray
    }
    static func category(_ index: Int, count: Int) -> Color {
        Color(hue: 0.55 + Double(index) / Double(max(1, count - 1)) * 0.08,
              saturation: 0.60, brightness: 0.56 + Double(index % 3) * 0.06)
    }
}

struct SpaceSector: Shape {
    var start: Double
    var end: Double
    var inner: CGFloat
    var outer: CGFloat
    func path(in rect: CGRect) -> Path {
        let c = CGPoint(x: rect.midX, y: rect.midY)
        let radius = min(rect.width, rect.height) / 2
        let a = Angle.radians(start * 2 * .pi - .pi / 2)
        let b = Angle.radians(end * 2 * .pi - .pi / 2)
        var p = Path()
        p.addArc(center: c, radius: radius * outer, startAngle: a, endAngle: b, clockwise: false)
        p.addArc(center: c, radius: radius * inner, startAngle: b, endAngle: a, clockwise: true)
        p.closeSubpath()
        return p
    }
}

struct SpaceView: View {
    var initialProjectIDs: Set<String>? = nil
    @LocalState var restrictToInitialScope = true
    @EnvironmentObject var store: OrganizerStore
    @LocalState var layout = "Sunburst"
    @LocalState var colorBy = "Age"
    @LocalState var groupBy = "Projects"
    @LocalState var projectKind = "All project types"
    @LocalState var technology = "All technologies"
    @LocalState var technologyProfile = ""
    @LocalState var activity = "All activity"
    @LocalState var age = "All ages"
    @LocalState var kind = "All types"
    @LocalState var search = ""
    @LocalState var cleanupOnly = false
    @LocalState var breakdown = "Projects"
    @LocalState var focused: String?
    @LocalState var hovered: String?
    let ages = ["All ages", "Under 30 days", "30–89 days", "90–364 days", "1 year or older", "Unknown age"]
    var projects: [Project] {
        (store.snapshot?.projects ?? []).filter {
            (search.isEmpty || ($0.name + " " + $0.category + " " + $0.kind + " " + $0.technologyText).localizedCaseInsensitiveContains(search)) &&
            (activity == "All activity" || SpaceData.activity($0) == activity) &&
            (age == "All ages" || SpaceData.age($0) == age) &&
            (initialProjectIDs == nil || !restrictToInitialScope || initialProjectIDs!.contains($0.id)) &&
            (focused == nil || $0.id == focused) &&
            (projectKind == "All project types" || $0.kind == projectKind) &&
            (technology == "All technologies" || ($0.languages ?? []).contains(technology)) &&
            (technologyProfile.isEmpty || profile($0) == technologyProfile)
        }
    }
    var slices: [SpaceSlice] { projects.flatMap { SpaceData.slices($0, cleanupOnly: cleanupOnly) }.filter { kind == "All types" || $0.kind == kind } }
    var kinds: [String] { Array(Set((store.snapshot?.projects ?? []).flatMap { SpaceData.slices($0) }.map(\.kind))).sorted() }
    var total: Int64 { slices.reduce(0) { $0 + $1.size } }
    var grouped: [(project: Project, slices: [SpaceSlice], size: Int64)] {
        projects.compactMap { p in
            let parts = slices.filter { $0.project.id == p.id }
            let size = parts.reduce(Int64(0)) { $0 + $1.size }
            return size > 0 ? (p, parts, size) : nil
        }.sorted { $0.size == $1.size ? $0.project.id < $1.project.id : $0.size > $1.size }
    }
    var projectKinds: [String] { Array(Set((store.snapshot?.projects ?? []).map(\.kind))).sorted() }
    var technologies: [String] { Array(Set((store.snapshot?.projects ?? []).flatMap { $0.languages ?? [] })).sorted() }
    func profile(_ project: Project) -> String { SpaceData.profile(project) }
    var chartGroups: [SpaceGroup] { SpaceData.groups(slices, by: groupBy) }
    func focusGroup(_ id: String) {
        if groupBy == "Projects" { focused = id }
        else if groupBy == "Project type" { projectKind = id }
        else { technologyProfile = id }
    }
    func label(_ s: SpaceSlice) -> String {
        switch colorBy {
        case "Age": return SpaceData.age(s.project)
        case "Activity": return SpaceData.activity(s.project)
        case "Project type": return s.project.kind
        case "Technology": return profile(s.project)
        default: return s.kind
        }
    }
    func keyColor(_ key: String) -> Color {
        if colorBy == "Age" { return IglooPalette.age(key) }
        if colorBy == "Activity" { return IglooPalette.activity(key) }
        if key == "Other project files" || key.hasPrefix("Unknown") { return .gray }
        return IglooPalette.category(colorKeys.firstIndex(of: key) ?? 0, count: colorKeys.count)
    }
    var typeTotals: [(kind: String, size: Int64)] {
        Dictionary(grouping: slices, by: { breakdown == "Project types" ? $0.project.kind : breakdown == "Technology" ? profile($0.project) : $0.kind })
            .map { (kind: $0.key, size: $0.value.reduce(0) { $0 + $1.size }) }.sorted { $0.size > $1.size }
    }
    var colorKeys: [String] {
        switch colorBy {
        case "Age": return Array(ages.dropFirst())
        case "Activity": return ["Active session", "No active session", "Activity unknown"]
        case "Project type": return projectKinds
        case "Technology": return Array(Set((store.snapshot?.projects ?? []).map(profile))).sorted()
        default: return kinds
        }
    }
    func color(_ s: SpaceSlice) -> Color {
        keyColor(label(s))
    }
    func caption(_ s: SpaceSlice) -> String {
        s.project.name + " · " + s.kind + " · " + bytes(s.size) + " · " +
        SpaceData.activity(s.project) + " · " + SpaceData.age(s.project)
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("Space explorer").font(.title2).fontWeight(.semibold)
                if initialProjectIDs != nil { Toggle("Keep current scope", isOn: $restrictToInitialScope).toggleStyle(.checkbox) }
                Spacer()
                Picker("Layout", selection: $layout) {
                    ForEach(["Sunburst", "Icicle", "Marimekko"], id: \.self) { Text($0) }
                }.pickerStyle(.segmented).frame(width: 330)
            }
            HStack {
                TextField("Find project, type or technology", text: $search).frame(maxWidth: 300)
                Picker("Group by", selection: $groupBy) {
                    ForEach(["Projects", "Project type", "Technology"], id: \.self) { Text($0) }
                }.frame(width: 240)
                Spacer()
                Picker("Color by", selection: $colorBy) {
                    ForEach(["Folder type", "Project type", "Technology", "Activity", "Age"], id: \.self) { Text($0) }
                }.frame(width: 220)
            }.controlSize(.small)
            HStack {
                Picker("Activity", selection: $activity) { ForEach(["All activity", "Active session", "No active session", "Activity unknown"], id: \.self) { Text($0) } }.labelsHidden()
                Picker("Age", selection: $age) { ForEach(ages, id: \.self) { Text($0) } }.labelsHidden()
                Picker("Project type", selection: $projectKind) { ForEach(["All project types"] + projectKinds, id: \.self) { Text($0) } }.labelsHidden()
                Picker("Technology", selection: $technology) { ForEach(["All technologies"] + technologies, id: \.self) { Text($0) } }.labelsHidden()
                Picker("Folder type", selection: $kind) { ForEach(["All types"] + kinds, id: \.self) { Text($0) } }.labelsHidden()
            }.controlSize(.small)
            HStack {
                Text(bytes(total)).font(.title).monospacedDigit()
                Text("in \(grouped.count) projects").foregroundStyle(.secondary)
                Toggle("Cleanup candidates only", isOn: $cleanupOnly).toggleStyle(.checkbox)
                Spacer()
                Button("Reset filters") {
                    restrictToInitialScope = false; focused = nil; projectKind = "All project types"; technology = "All technologies"; technologyProfile = ""
                    kind = "All types"; search = ""; age = "All ages"; activity = "All activity"; cleanupOnly = false
                }
            }
            HStack(alignment: .top, spacing: 24) {
                VStack(alignment: .leading, spacing: 12) {
                    if total == 0 {
                        ContentUnavailableView("No measured space matches", systemImage: "chart.pie",
                            description: Text("Adjust the filters or run Analyse Projects."))
                    } else {
                        chart.frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                    Text(hovered.flatMap { id in slices.first { $0.id == id } }.map(caption) ??
                         "Select a segment to focus its project. Use Open project for cleanup and preservation.")
                        .font(.callout).foregroundStyle(.secondary).lineLimit(2).frame(height: 38, alignment: .leading)
                    legend
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
                VStack(alignment: .leading, spacing: 10) {
                    Picker("Breakdown", selection: $breakdown) {
                        ForEach(["Projects", "Folder types", "Project types", "Technology"], id: \.self) { Text($0) }
                    }.pickerStyle(.menu)
                    if breakdown != "Projects" {
                        List(typeTotals, id: \.kind) { item in
                            VStack(alignment: .leading, spacing: 6) {
                                Button(item.kind) {
                                    if breakdown == "Project types" { projectKind = item.kind }
                                    else if breakdown == "Technology" { technologyProfile = item.kind }
                                    else { kind = item.kind }
                                }.buttonStyle(.link)
                                Text(bytes(item.size) + " · " + String(format: "%.1f%%", Double(item.size) / Double(max(1,total)) * 100)).font(.caption).monospacedDigit()
                            }.padding(.vertical, 6)
                        }
                    } else {
                    List(grouped, id: \.project.id) { group in
                        VStack(alignment: .leading, spacing: 5) {
                            HStack {
                                Button(group.project.name) { focused = group.project.id }.buttonStyle(.link).lineLimit(1)
                                Spacer()
                                Text(bytes(group.size)).monospacedDigit()
                            }
                            Text("\(group.project.kind) · \(SpaceData.age(group.project))").font(.caption).foregroundStyle(.secondary)
                            HStack {
                                Text(String(format: "%.1f%%", Double(group.size) / Double(max(1,total)) * 100)).font(.caption)
                                Spacer()
                                Button("Open project") {
                                    store.filter = "All projects"; store.search = ""; store.typeFilter = "All types"
                                    store.selection = group.project.id; store.spaceShown = false
                                }.controlSize(.small)
                            }
                        }.padding(.vertical, 5)
                    }.listStyle(.inset)
                    }
                }.frame(width: 300)
            }
            Divider()
            Text(explanation).font(.caption).foregroundStyle(.secondary)
            HStack {
                Text("Allocated bytes from catalog · " + shortDate(store.snapshot?.generated ?? "")).font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Done") { store.spaceShown = false }.keyboardShortcut(.cancelAction)
            }
        }.padding(24).frame(width: sheetWidth(1180), height: 780)
    }
    var explanation: String {
        let geometry = layout == "Sunburst" ? "Inner ring: selected groups. Outer ring: project folder slices. Angle shows bytes." :
            layout == "Icicle" ? "Top row: selected groups. Bottom row: project folder slices. Width shows bytes." :
            "Column width shows group bytes; folder-slice heights show composition, so area shows bytes."
        return geometry + " Age colors run warm for recent Git activity to cold for older projects; gray means unknown. Category colors use an ice-blue scale and carry no age meaning. Types are measured dependency/build and review folders; Other project files is the remainder, including source, Git metadata and local data. Type does not imply safe to delete. Age estimates Git activity; activity reflects detected working directories. Technology groups use full detected combinations so each project is counted once. Freezer and unassigned workspace files are outside these project totals."
    }
    var legend: some View {
        ViewThatFits {
            HStack(spacing: 14) { legendItems }
            ScrollView(.horizontal) { HStack(spacing: 14) { legendItems } }
        }.frame(height: 24)
    }
    @ViewBuilder var legendItems: some View {
        ForEach(colorKeys.filter { key in slices.contains { label($0) == key } }, id: \.self) { key in
            HStack(spacing: 5) {
                Circle().fill(keyColor(key)).frame(width: 8, height: 8)
                Text(key).font(.caption)
            }
        }
    }
    private func groupSector(id: String, name: String, start: Double, fraction: Double, index: Int, size: Int64) -> some View {
        let sector = SpaceSector(start: start, end: start + fraction, inner: 0.24, outer: 0.57)
        return sector.fill(IglooPalette.category(index, count: chartGroups.count))
            .overlay(sector.stroke(Color(nsColor: .windowBackgroundColor), lineWidth: 1))
            .contentShape(sector)
            .onTapGesture { focusGroup(id) }
            .help(name + " · " + bytes(size))
            .accessibilityLabel(name + ", " + bytes(size))
            .accessibilityAddTraits(.isButton)
            .accessibilityAction { focusGroup(id) }
    }
    @ViewBuilder var chart: some View {
        GeometryReader { proxy in
            let groups = chartGroups
            let denominator = Double(max(1, total))
            ZStack(alignment: .topLeading) {
                ForEach(Array(groups.enumerated()), id: \.element.id) { index, group in
                    let before = Double(groups.prefix(index).reduce(Int64(0)) { $0 + $1.size }) / denominator
                    let fraction = Double(group.size) / denominator
                    if layout == "Sunburst" {
                        groupSector(id: group.id, name: group.name, start: before, fraction: fraction, index: index, size: group.size)
                    } else if layout == "Icicle" {
                        Text(proxy.size.width * fraction > 65 ? group.name : "").font(.caption).lineLimit(1)
                            .frame(width: max(0, proxy.size.width * fraction - 2), height: 50)
                            .background(Color.accentColor.opacity(0.15))
                            .clipped().offset(x: proxy.size.width * before)
                            .onTapGesture { focusGroup(group.id) }
                            .help(group.name)
                    }
                    ForEach(Array(group.slices.enumerated()), id: \.element.id) { partIndex, slice in
                        let prior = Double(group.slices.prefix(partIndex).reduce(Int64(0)) { $0 + $1.size })
                        let part = Double(slice.size) / Double(group.size)
                        if layout == "Sunburst" {
                            let start = before + prior / denominator
                            SpaceSector(start: start, end: start + Double(slice.size) / denominator, inner: 0.58, outer: 0.96)
                                .fill(color(slice))
                                .overlay(SpaceSector(start: start, end: start + Double(slice.size) / denominator, inner: 0.58, outer: 0.96).stroke(.background, lineWidth: 1))
                                .contentShape(SpaceSector(start: start, end: start + Double(slice.size) / denominator, inner: 0.58, outer: 0.96))
                                .onTapGesture { focused = slice.project.id }
                                .onHover { hovered = $0 ? slice.id : nil }
                                .help(caption(slice))
                                .accessibilityLabel(caption(slice)).accessibilityAddTraits(.isButton)
                                .accessibilityAction { focused = slice.project.id }
                        } else {
                            let icicle = layout == "Icicle"
                            let w = proxy.size.width * (icicle ? Double(slice.size) / denominator : fraction)
                            let h = icicle ? proxy.size.height - 54 : proxy.size.height * part
                            let x = proxy.size.width * (before + (icicle ? prior / denominator : 0))
                            let y = icicle ? 54 : proxy.size.height * prior / Double(group.size)
                            Button { focused = slice.project.id } label: {
                                VStack(alignment: .leading, spacing: 4) {
                                    if w > 85 && h > 45 {
                                        Text(icicle ? slice.kind : slice.project.name).font(.caption).fontWeight(.semibold)
                                        Text(bytes(slice.size)).font(.caption2)
                                    }
                                }.padding(w > 85 ? 8 : 0).frame(width: max(0,w-1), height: max(0,h-1), alignment: .topLeading)
                                    .foregroundStyle(.white).background(color(slice)).clipped()
                            }.buttonStyle(.plain).offset(x: x, y: y)
                                .onHover { hovered = $0 ? slice.id : nil }.help(caption(slice))
                                .accessibilityLabel(caption(slice))
                        }
                    }
                }
            }
        }
    }
}
