import SwiftUI

struct SavedTask: Codable, Identifiable, Equatable {
    var id = ""
    var name = ""
    var root = ""
    var vault = ""
    var mode = "review"
    var rules = WizardRules()
    var include: [String] = []
    var exclude: [String] = []
    var projectTypes: [String] = []
    var maxProjects = 5
    var maxBytes: Int64 = 10 * 1024 * 1024 * 1024
    var unattended = false
}
struct TaskRun: Decodable, Identifiable {
    var id: String; var taskID: String; var taskName: String; var started: String
    var status: String; var message: String; var planID: String?
    var ready: Int; var blocked: Int; var cacheBytes: Int64
    var outcome: ReviewOutcome?; var observed: ReviewOutcome.ObservedSpace?
}
struct TaskSchedule: Decodable {
    var frequency: String; var time: String; var weekday: Int
    var loaded: Bool; var stale: Bool
}
struct TaskItem: Decodable, Identifiable {
    var definition: SavedTask; var schedule: TaskSchedule?; var lastRun: TaskRun?; var compatible: Bool
    var id: String { definition.id }
}
struct TaskListing: Decodable {
    var tasks: [TaskItem]; var cliPath: String; var cliInstalled: Bool
}
struct TaskHistory: Decodable { var runs: [TaskRun] }
struct TaskScheduleRequest: Encodable { var frequency: String; var time: String; var weekday: Int }
func taskSpaceSummary(_ run: TaskRun) -> String {
    let estimate = run.outcome.map { "Estimate: \(bytes($0.bytesFreedEstimate))" } ?? "Estimate: not measured"
    let observed = run.observed ?? run.outcome?.observedSpace
    guard let observed else { return estimate + " · not measured" }
    return estimate + " · Freed from disk: \(measuredSpace(observed.freedBytes)) · In Trash: \(measuredSpace(observed.inTrashBytes)) · Restored from Trash: \(measuredSpace(observed.restoredBytes))"
}
struct TaskRunHistoryTable: View {
    var runs: [TaskRun]
    @Binding var selectedRun: String?
    var body: some View {
        Table(runs, selection: $selectedRun) {
            TableColumn("Started") { Text(String($0.started.prefix(16))) }.width(125)
            TableColumn("Result", value: \.status).width(105)
            TableColumn("Space") { Text(taskSpaceSummary($0)).lineLimit(2) }.width(min: 250)
            TableColumn("Summary", value: \.message)
        }
    }
}
struct TaskHistoryCaptureView: View {
    @EnvironmentObject var store: OrganizerStore
    @LocalState var selectedRun: String?
    private let runs: [TaskRun]
    init() { runs = DemoCatalog.reply(["task-history"], as: TaskHistory.self)?.runs ?? [] }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Task run history").font(.title2).fontWeight(.semibold)
            TaskRunHistoryTable(runs: runs, selectedRun: $selectedRun)
            HStack { Spacer(); Button("Done") { store.demoCaptureSheet = nil }.keyboardShortcut(.cancelAction) }
        }.padding(24).frame(width: sheetWidth(980), height: 600)
    }
}
struct TasksView: View {
    @EnvironmentObject var store: OrganizerStore
    @Environment(\.dismiss) private var dismiss
    var initialRules: WizardRules?
    @LocalState var items: [TaskItem] = []
    @LocalState var selected: String?
    @LocalState var draft = SavedTask()
    @LocalState var section = "Rules"
    @LocalState var newTask = false
    @LocalState var includeText = ""
    @LocalState var excludeText = ""
    @LocalState var gib = 10.0
    @LocalState var frequency = "weekly"
    @LocalState var weekday = 0
    @LocalState var clock = "09:00"
    @LocalState var message = ""
    @LocalState var cliInstalled = false
    @LocalState var runs: [TaskRun] = []
    @LocalState var review: WizardPlan?
    @LocalState var selectedRun: String?
    @LocalState var deleteConfirm = false
    var item: TaskItem? { items.first { $0.id == selected } }
    var dirty: Bool { newTask || assembled != item?.definition }
    var assembled: SavedTask {
        var value = draft
        value.include = lines(includeText); value.exclude = lines(excludeText)
        value.maxBytes = Int64(max(0, min(gib.isFinite ? gib : 0, 102400)) * 1073741824)
        return value
    }
    func lines(_ text: String) -> [String] { text.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty } }
    func encode<T: Encodable>(_ value: T) -> String { String(data: try! JSONEncoder().encode(value), encoding: .utf8)! }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("Tasks").font(.title2).fontWeight(.semibold)
                Spacer()
                Button(cliInstalled ? "Reinstall CLI" : "Install CLI") {
                    store.invoke(["task-install-cli"], as: Reply.self) { reply in message = reply.message ?? ""; reload() }
                }.disabled(store.busy || store.isDemo)
                Button("New task") { begin() }.disabled(store.busy || store.isDemo)
            }
            Text("Save repeatable rules, run a fresh review, or explicitly allow scheduled cache cleanup. Freeze candidates stay held for review.")
                .font(.callout).foregroundStyle(.secondary)
            HSplitView {
                List(items, selection: $selected) { task in
                    VStack(alignment: .leading, spacing: 5) {
                        Text(task.definition.name).fontWeight(.medium)
                        Text(task.definition.mode == "review" ? "Review only" : "Cache cleanup").font(.caption).foregroundStyle(.secondary)
                        if let schedule = task.schedule {
                            Text(schedule.stale ? "Schedule needs re-enabling" : schedule.loaded ? schedule.frequency.capitalized + " · " + schedule.time : "Schedule not loaded")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        if let run = task.lastRun { Text("Last run: " + run.status).font(.caption).foregroundStyle(.secondary) }
                    }.padding(.vertical, 6).tag(task.id)
                }.frame(minWidth: 150, idealWidth: 200, maxWidth: 240)
                VStack(alignment: .leading, spacing: 14) {
                    if item != nil || newTask {
                        HStack {
                            TextField("Task name", text: $draft.name).font(.headline)
                            TextField("task-id", text: $draft.id).frame(width: 185).disabled(!newTask)
                        }
                        Picker("Task section", selection: $section) {
                            ForEach(["Rules", "Schedule", "History"], id: \.self) { Text($0) }
                        }.pickerStyle(.segmented)
                        if section == "History" { historyView }
                        else { ScrollView { if section == "Rules" { rulesView } else { scheduleView } } }
                        Spacer(minLength: 0)
                        HStack {
                            Button("Review now") { planNow() }.disabled(store.busy || dirty || store.isDemo || item?.compatible != true)
                            Button("Delete task", role: .destructive) { deleteConfirm = true }
                                .disabled(store.busy || item == nil || item?.schedule != nil || store.isDemo)
                                .help("Disable an existing schedule before deleting its task.")
                            Spacer()
                            Button("Save task") {
                                store.invoke(["task-save", encode(assembled)], as: Reply.self) { value in
                                    message = value.message ?? ""; selected = draft.id; newTask = false; reload()
                                }
                            }.buttonStyle(.borderedProminent).disabled(store.busy || !dirty || draft.name.isEmpty || draft.id.isEmpty || store.isDemo)
                        }
                    } else {
                        ContentUnavailableView("Save your first task", systemImage: "checklist",
                            description: Text("Create a review-only task first, then enable execution and scheduling when its rules suit you."))
                    }
                }.padding(.leading, 18).frame(minWidth: 360, maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            }
            Divider()
            HStack {
                if store.busy { ProgressView().controlSize(.small) }
                Text(message.isEmpty ? "CLI: ~/.local/bin/igloo · Tasks: ~/.config/igloo/tasks.json" : message)
                    .font(.caption).foregroundStyle(.secondary).lineLimit(3).textSelection(.enabled)
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction).disabled(store.busy)
            }
        }.padding(24).frame(width: sheetWidth(1120), height: 780).interactiveDismissDisabled(store.busy)
            .confirmationDialog("Delete this saved task?", isPresented: $deleteConfirm, titleVisibility: .visible) {
                Button("Delete task", role: .destructive) {
                    store.invoke(["task-delete", draft.id], as: Reply.self) { value in
                        selected = nil; newTask = false; message = value.message ?? ""; reload()
                    }
                }
                Button("Cancel", role: .cancel) {}
            } message: { Text("The task definition is removed. Run history and reviews remain available.") }
            .onAppear { reload() }
            .onChange(of: selected) { if let item = item { edit(item) } }
            .sheet(item: $review, onDismiss: { reload() }) { plan in
                TaskPlanReview(plan: plan, allowApply: item?.definition.mode == "cleanup") { reload(); loadHistory() }.environmentObject(store)
            }
    }
    var rulesView: some View {
        VStack(alignment: .leading, spacing: 18) {
            Picker("Mode", selection: $draft.mode) {
                Text("Review only").tag("review"); Text("Cache cleanup").tag("cleanup")
            }
            Picker("Age measure", selection: $draft.rules.ageBasis) {
                Text("Git activity").tag("git"); Text("Project file modification").tag("files")
            }
            HStack {
                LabeledContent("Clean caches at") { TextField("Days", value: $draft.rules.cacheDays, format: .number).labelsHidden().frame(width: 65); Text("days") }
                Spacer()
                LabeledContent("Hold for freeze review at") { TextField("Days", value: $draft.rules.freezeDays, format: .number).labelsHidden().frame(width: 65); Text("days") }
            }
            HStack {
                LabeledContent("Max projects per run") { TextField("Projects", value: $draft.maxProjects, format: .number).labelsHidden().frame(width: 65) }
                Spacer()
                LabeledContent("Max cache GiB per run") { TextField("GiB", value: $gib, format: .number).labelsHidden().frame(width: 65) }
            }
            Toggle("Exclude active projects", isOn: $draft.rules.ignoreActive)
            if draft.mode == "cleanup" {
                Toggle("Allow unattended cleanup of future matching caches within these limits", isOn: $draft.unattended)
                Text("This authorizes future matching cache folders to move to Trash without per-folder confirmation. Active processes, tracked files and local configuration still block cleanup. Custom dependency edits need your review before opting in.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            DisclosureGroup("Cache types (\(draft.rules.cacheKinds.count))") {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), alignment: .leading)]) {
                    ForEach(["node_modules", ".venv", "venv", ".turbo", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next", ".nuxt", ".svelte-kit", ".parcel-cache"], id: \.self) { kind in
                        Toggle(kind, isOn: Binding(get: { draft.rules.cacheKinds.contains(kind) }, set: {
                            if $0 { draft.rules.cacheKinds.append(kind) } else { draft.rules.cacheKinds.removeAll { $0 == kind } }
                        })).toggleStyle(.checkbox)
                    }
                }.padding(.top, 8)
            }
            DisclosureGroup("Project types: all unless selected") {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 180), alignment: .leading)]) {
                    ForEach(projectTypeNames, id: \.self) { kind in
                        Toggle(kind, isOn: Binding(get: { draft.projectTypes.contains(kind) }, set: {
                            if $0 { draft.projectTypes.append(kind) } else { draft.projectTypes.removeAll { $0 == kind } }
                        })).toggleStyle(.checkbox)
                    }
                }.padding(.top, 8)
            }
            Text("Include folders").font(.headline)
            Text("One absolute path per line. Empty includes the whole configured workspace.").font(.caption).foregroundStyle(.secondary)
            TextEditor(text: $includeText).font(.callout).frame(height: 58).border(Color.secondary.opacity(0.2)).accessibilityLabel("Included folders")
            Text("Exclude folders").font(.headline)
            TextEditor(text: $excludeText).font(.callout).frame(height: 58).border(Color.secondary.opacity(0.2)).accessibilityLabel("Excluded folders")
            Text("Workspace: " + draft.root).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
        }.textFieldStyle(.roundedBorder).padding(.trailing, 8)
    }
    var scheduleView: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Run while your Mac user is logged in").font(.headline)
            Text("Calendar times use the Mac's local time. launchd can coalesce missed runs after sleep. Enabling a schedule does not run it immediately.")
                .font(.callout).foregroundStyle(.secondary)
            Picker("Frequency", selection: $frequency) { Text("Daily").tag("daily"); Text("Weekly").tag("weekly") }
            if frequency == "weekly" {
                Picker("Day", selection: $weekday) {
                    ForEach(Array(["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"].enumerated()), id: \.offset) { i, day in Text(day).tag(i) }
                }
            }
            TextField("Local time (HH:MM)", text: $clock).textFieldStyle(.roundedBorder).frame(width: 240)
            if let schedule = item?.schedule {
                Text(schedule.stale ? "Rules changed. Re-enable to authorize the saved policy." : schedule.loaded ? "LaunchAgent is loaded." : "LaunchAgent is not loaded.")
                    .font(.callout)
            }
            HStack {
                Button("Enable / update schedule") {
                    let data = encode(TaskScheduleRequest(frequency: frequency, time: clock, weekday: weekday))
                    store.invoke(["task-schedule", draft.id, data], as: Reply.self) { value in message = value.message ?? ""; reload() }
                }.disabled(store.busy || dirty || item == nil || draft.mode == "cleanup" && !draft.unattended || store.isDemo)
                Button("Disable schedule") {
                    store.invoke(["task-unschedule", draft.id], as: Reply.self) { value in message = value.message ?? ""; reload() }
                }.disabled(store.busy || item?.schedule == nil || store.isDemo)
            }
            Text("Task changes invalidate the previous schedule's policy authorization. Save edits first, then enable the schedule again. App updates do not require the source checkout: the installed CLI uses its own versioned backend copy.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }
    var historyView: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button("Refresh history") { loadHistory() }.disabled(store.busy)
            Text("Estimate is from the Review. Freed from disk, In Trash and Restored from Trash are measured; older runs show not measured.")
                .font(.caption).foregroundStyle(.secondary)
            TaskRunHistoryTable(runs: runs, selectedRun: $selectedRun)
            Button("Open saved review") {
                if let id = runs.first(where: { $0.id == selectedRun })?.planID { loadPlan(id) }
            }.disabled(store.busy || runs.first(where: { $0.id == selectedRun })?.planID == nil)
        }
    }
    func begin() {
        selected = nil; newTask = true; draft = SavedTask()
        draft.root = store.snapshot?.root ?? store.root.path; draft.vault = store.preferences.archiveRoot
        draft.rules = initialRules ?? WizardRules(cacheKinds: ["node_modules", ".venv", "venv", ".turbo", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next", ".nuxt", ".svelte-kit", ".parcel-cache"])
        includeText = ""; excludeText = ""; gib = 10; section = "Rules"
    }
    func edit(_ item: TaskItem) {
        newTask = false; draft = item.definition; includeText = draft.include.joined(separator: "\n"); excludeText = draft.exclude.joined(separator: "\n")
        gib = Double(draft.maxBytes) / 1073741824
        if let schedule = item.schedule { frequency = schedule.frequency; weekday = schedule.weekday; clock = schedule.time }
        loadHistory()
    }
    func reload() {
        store.invoke(["task-list"], as: TaskListing.self) { value in
            items = value.tasks; cliInstalled = value.cliInstalled
            if initialRules != nil && selected == nil && !newTask { begin() }
            else if let item = item { edit(item) }
            else if selected == nil && !newTask { selected = items.first?.id }
        }
    }
    func loadHistory() {
        guard let id = selected else { runs = []; return }
        store.invoke(["task-history", id], as: TaskHistory.self) { runs = $0.runs }
    }
    func planNow() {
        store.invoke(["task-run", draft.id], as: TaskRun.self) { run in
            message = run.message
            if let id = run.planID { loadPlan(id) }
        }
    }
    func loadPlan(_ id: String) {
        store.invoke(["task-plan", id], as: WizardPlan.self) { review = $0 }
    }
}
struct TaskPlanReview: View {
    @EnvironmentObject var store: OrganizerStore
    @Environment(\.dismiss) private var dismiss
    let plan: WizardPlan
    let allowApply: Bool
    let completed: () -> Void
    @LocalState var accepted = false
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Review task actions").font(.title2).fontWeight(.semibold)
            Text("Only ready cache rows can run. Held freeze candidates remain untouched.").font(.callout).foregroundStyle(.secondary)
            List(plan.rows) { row in
                VStack(alignment: .leading, spacing: 6) {
                    HStack { Text(row.name).fontWeight(.medium); Spacer(); Text(row.status.capitalized).font(.caption) }
                    Text(row.message).font(.caption).foregroundStyle(.secondary)
                    if let cleanup = row.cleanup {
                        ForEach(cleanup.items) { Text($0.path + " · " + bytes($0.size)).font(.caption).textSelection(.enabled) }
                        ForEach(cleanup.blocked) { Text("Not run: " + $0.path + " · " + $0.reason).font(.caption) }
                    }
                }.padding(.vertical, 6)
            }
            if let outcome = plan.outcome {
                ScrollView { OutcomeSummaryView(outcome: outcome) }.frame(maxHeight: 220)
            }
            if allowApply && plan.status == "review" {
                Toggle("Move the listed ready cache folders to Trash; I reviewed them for custom edits.", isOn: $accepted)
            } else { Text("This is a review-only task or an already accepted review.").font(.caption).foregroundStyle(.secondary) }
            HStack {
                Button("Close") { dismiss() }.keyboardShortcut(.cancelAction).disabled(store.busy)
                Spacer()
                Button(plan.outcome?.actionLabel ?? "Move reviewed folders to Trash") {
                    store.invoke(["task-apply", plan.id], as: TaskRun.self) { _ in dismiss(); completed() }
                }.buttonStyle(.borderedProminent).disabled(store.busy || !accepted || !allowApply || plan.status != "review" || !plan.rows.contains { $0.status == "ready" } || store.isDemo)
            }
        }.padding(24).frame(width: 850, height: 640).interactiveDismissDisabled(store.busy)
    }
}
