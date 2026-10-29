import SwiftUI
import AppKit
import UserNotifications

struct UserPreferences: Codable {
    var version = 1
    var projectsRoot: String
    var archiveRoot: String
    var stateRoot: String
    var cloneRoot: String
    var freezerBackupRoot = ""
    var freezerBackupCloudAcceptedRoot = ""
    var quietDays = 60
    var quietReminderEnabled = false
    var quietReminderWeekday = 1
    var quietReminderTime = "09:00"
    var githubOwner = ""
    var pythonExecutable: String
    var gitExecutable = "/usr/bin/git"
    var githubExecutable: String
    static var bundledPython: String? {
        #if arch(arm64)
        let architecture = "arm64"
        #else
        let architecture = "x86_64"
        #endif
        guard let resources = Bundle.main.resourceURL else { return nil }
        let path = resources.appendingPathComponent("python/" + architecture + "/bin/python3").path
        return FileManager.default.isExecutableFile(atPath: path) ? path : nil
    }
    static let appIdentifier = ConfigRoot.appIdentifier(Bundle.main.bundleIdentifier)
    /// The first read in Igloo Dev copies the release settings into the Dev folder once.
    static let file: URL = {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let folder = ConfigRoot.folder(for: appIdentifier, home: home)
        if appIdentifier != ConfigRoot.releaseIdentifier, !CommandLine.arguments.contains("--demo") {
            ConfigRoot.seed(folder, from: ConfigRoot.folder(for: ConfigRoot.releaseIdentifier, home: home))
        }
        return folder.appendingPathComponent("config.json")
    }()
    static var backendEnvironment: [String: String] {
        ProcessInfo.processInfo.environment.merging(["IGLOO_APP_ID": appIdentifier]) { $1 }
    }
    static func defaults() -> UserPreferences {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let root = home + "/Projects"
        // The bundled Python is a runtime choice. The saved default is a system Python.
        let python = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"].first { FileManager.default.isExecutableFile(atPath: $0) } ?? "/usr/bin/python3"
        let gh = ["/opt/homebrew/bin/gh", "/usr/local/bin/gh"].first { FileManager.default.isExecutableFile(atPath: $0) } ?? "/opt/homebrew/bin/gh"
        return UserPreferences(projectsRoot: root, archiveRoot: home + "/.igloo/vault",
                               stateRoot: home + "/.igloo", cloneRoot: root,
                               pythonExecutable: python, githubExecutable: gh)
    }
    static func read(ownBundle: String = Bundle.main.bundlePath) -> UserPreferences {
        let base = defaults()
        guard let data = try? Data(contentsOf: file),
              let values = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let baseData = try? JSONEncoder().encode(base),
              var merged = try? JSONSerialization.jsonObject(with: baseData) as? [String: Any] else { return base }
        for (key, value) in values { merged[key] = value }
        guard let full = try? JSONSerialization.data(withJSONObject: merged),
              var decoded = try? JSONDecoder().decode(UserPreferences.self, from: full) else { return base }
        // A bundled Python of another app copy is ignored. The file changes only when the user saves.
        if ConfigRoot.isForeignBundlePath(decoded.pythonExecutable, ownBundle: ownBundle) {
            decoded.pythonExecutable = base.pythonExecutable
        }
        return decoded
    }
    func validated() throws -> UserPreferences {
        var p = self
        func expand(_ value: String) -> String { (value as NSString).expandingTildeInPath }
        p.projectsRoot = expand(p.projectsRoot); p.archiveRoot = expand(p.archiveRoot)
        p.stateRoot = expand(p.stateRoot); p.cloneRoot = expand(p.cloneRoot)
        p.freezerBackupRoot = expand(p.freezerBackupRoot)
        p.freezerBackupCloudAcceptedRoot = expand(p.freezerBackupCloudAcceptedRoot)
        for path in [p.freezerBackupRoot, p.freezerBackupCloudAcceptedRoot] where !path.isEmpty {
            guard path.hasPrefix("/"), !path.split(separator: "/").contains("..") else {
                throw NSError(domain: "Settings", code: 1, userInfo: [NSLocalizedDescriptionKey: "Use an absolute Freezer backup path without traversal."])
            }
        }
        p.pythonExecutable = expand(p.pythonExecutable); p.gitExecutable = expand(p.gitExecutable)
        p.githubExecutable = expand(p.githubExecutable)
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let root = URL(fileURLWithPath: p.projectsRoot).standardizedFileURL.path
        var directory: ObjCBool = false
        guard p.version == 1, p.projectsRoot.hasPrefix("/"),
              FileManager.default.fileExists(atPath: root, isDirectory: &directory), directory.boolValue,
              root != "/", root != home, root != URL(fileURLWithPath: home).deletingLastPathComponent().path else {
            throw NSError(domain: "Settings", code: 1, userInfo: [NSLocalizedDescriptionKey: "Choose an existing project workspace rather than your whole home or filesystem."])
        }
        for path in [p.archiveRoot, p.stateRoot, p.cloneRoot, p.pythonExecutable, p.gitExecutable, p.githubExecutable] {
            guard path.hasPrefix("/") else {
                throw NSError(domain: "Settings", code: 1, userInfo: [NSLocalizedDescriptionKey: "Use absolute paths or paths beginning with ~."])
            }
        }
        for path in [p.archiveRoot, p.stateRoot] {
            let folder = URL(fileURLWithPath: path).standardizedFileURL.path
            guard folder != root, folder != home, folder != "/", !root.hasPrefix(folder + "/") else {
                throw NSError(domain: "Settings", code: 1, userInfo: [NSLocalizedDescriptionKey: "Archive and state storage need dedicated folders, separate from the workspace root."])
            }
        }
        guard p.cloneRoot == root || p.cloneRoot.hasPrefix(root + "/"), (1...3650).contains(p.quietDays) else {
            throw NSError(domain: "Settings", code: 1, userInfo: [NSLocalizedDescriptionKey: "The clone folder must be inside the Projects root, and quiet days must be between 1 and 3650."])
        }
        guard (0...6).contains(p.quietReminderWeekday),
              p.quietReminderTime.range(of: "^(?:[01]\\d|2[0-3]):[0-5]\\d$", options: .regularExpression) != nil else {
            throw NSError(domain: "Settings", code: 1, userInfo: [NSLocalizedDescriptionKey: "Choose a weekday and a 24-hour reminder time in HH:MM format."])
        }
        guard Self.bundledPython != nil || FileManager.default.isExecutableFile(atPath: p.pythonExecutable),
              FileManager.default.isExecutableFile(atPath: p.gitExecutable) else {
            throw NSError(domain: "Settings", code: 1, userInfo: [NSLocalizedDescriptionKey: "Choose installed Python and Git executables."])
        }
        guard p.githubOwner.isEmpty || p.githubOwner.range(of: "^[A-Za-z0-9][A-Za-z0-9-]*$", options: .regularExpression) != nil else {
            throw NSError(domain: "Settings", code: 1, userInfo: [NSLocalizedDescriptionKey: "Use a GitHub account or organization login, with no spaces."])
        }
        p.projectsRoot = root
        return p
    }
    func write() throws {
        let fm = FileManager.default
        try fm.createDirectory(at: Self.file.deletingLastPathComponent(), withIntermediateDirectories: true,
                               attributes: [.posixPermissions: 0o700])
        let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(self).write(to: Self.file, options: .atomic)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: Self.file.path)
    }
}

/// The configuration folder of each build. The release keeps its 0.1.0 folder.
/// Every other build, including an unbundled binary, uses the Dev folder.
enum ConfigRoot {
    static let releaseIdentifier = "local.igloo"
    static let devIdentifier = "local.igloo.dev"
    /// Files that the first Dev read copies from the release folder.
    static let seededFiles = ["config.json", "wizard.json"]

    static func appIdentifier(_ bundleIdentifier: String?) -> String {
        bundleIdentifier == releaseIdentifier ? releaseIdentifier : devIdentifier
    }
    static func folder(for identifier: String, home: URL) -> URL {
        home.appendingPathComponent(identifier == releaseIdentifier ? ".config/igloo" : ".config/igloo.dev")
    }
    static func displayPath(_ url: URL) -> String {
        (url.path as NSString).abbreviatingWithTildeInPath
    }
    /// The .app folder that contains path, or nil.
    static func appBundle(of path: String) -> String? {
        let parts = URL(fileURLWithPath: (path as NSString).expandingTildeInPath).resolvingSymlinksInPath().pathComponents
        guard let index = parts.indices.dropLast().first(where: { parts[$0].hasSuffix(".app") && parts[$0 + 1] == "Contents" })
        else { return nil }
        return NSString.path(withComponents: Array(parts[...index]))
    }
    static func isForeignBundlePath(_ path: String, ownBundle: String) -> Bool {
        guard let bundle = appBundle(of: path) else { return false }
        return bundle != URL(fileURLWithPath: ownBundle).resolvingSymlinksInPath().path
    }
    /// Copies the release settings once. A Dev folder that exists is never changed.
    static func seed(_ folder: URL, from release: URL) {
        let fm = FileManager.default
        guard !fm.fileExists(atPath: folder.path) else { return }
        do {
            try fm.createDirectory(at: folder, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            for name in seededFiles {
                let source = release.appendingPathComponent(name), target = folder.appendingPathComponent(name)
                guard var data = try? Data(contentsOf: source) else { continue }
                if name == "config.json" {
                    guard var values = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
                    values.removeValue(forKey: "pythonExecutable")
                    data = try JSONSerialization.data(withJSONObject: values, options: [.prettyPrinted, .sortedKeys])
                }
                try data.write(to: target, options: .atomic)
                try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: target.path)
            }
        } catch {
            // A failed copy leaves the Dev folder with defaults. The release folder is only read.
        }
    }
}

struct BackupLocation: Identifiable {
    var url: URL
    var title: String
    var provider: String?
    var id: String { url.path }
}

enum BackupLocations {
    private static func normalized(_ url: URL, resolve: Bool) -> String {
        let standardized = url.standardizedFileURL
        let path = (resolve ? standardized.resolvingSymlinksInPath() : standardized).path
        let prefix = "/system/volumes/data/"
        let folded = path.lowercased()
        return folded.hasPrefix(prefix) ? "/" + String(folded.dropFirst(prefix.count)) : folded
    }
    private static func contains(_ path: String, in parent: String) -> Bool {
        path == parent || path.hasPrefix(parent + "/")
    }
    static func provider(for url: URL) -> String? {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let paths = [normalized(url, resolve: false), normalized(url, resolve: true)]
        let mobile = home.appendingPathComponent("Library/Mobile Documents")
        let mobileParents = [normalized(mobile, resolve: false), normalized(mobile, resolve: true)]
        if paths.contains(where: { path in mobileParents.contains(where: { contains(path, in: $0) }) }) { return "iCloud Drive" }
        let cloudURL = home.appendingPathComponent("Library/CloudStorage")
        let cloudParents = [normalized(cloudURL, resolve: false), normalized(cloudURL, resolve: true)]
        for path in paths {
            guard let cloud = cloudParents.first(where: { contains(path, in: $0) }) else { continue }
            let relative = String(path.dropFirst(cloud.count)).split(separator: "/").first.map(String.init) ?? ""
            let parts = relative.split(separator: "-", maxSplits: 1).map(String.init)
            let provider = parts.first ?? ""
            switch provider.lowercased() {
            case "googledrive": return "Google Drive"
            case "onedrive": return "OneDrive"
            case "dropbox": return "Dropbox"
            case "box": return "Box"
            default:
                if parts.count > 1 && !provider.contains("@") && provider.range(of: "^[a-z0-9_ ]+$", options: .regularExpression) != nil {
                    return provider.capitalized
                }
                return "Cloud storage"
            }
        }
        return nil
    }
    static func identity(for url: URL) -> String {
        url.standardizedFileURL.resolvingSymlinksInPath().path.lowercased()
    }
    static func detected() -> [BackupLocation] {
        let fm = FileManager.default
        let keys: [URLResourceKey] = [.volumeIsInternalKey, .volumeIsLocalKey, .volumeNameKey]
        let disks = (fm.mountedVolumeURLs(includingResourceValuesForKeys: keys, options: [.skipHiddenVolumes]) ?? []).compactMap { url -> BackupLocation? in
            guard let values = try? url.resourceValues(forKeys: Set(keys)), values.volumeIsInternal == false,
                  values.volumeIsLocal == true, url.path != "/" else { return nil }
            return BackupLocation(url: url, title: values.volumeName ?? "External disk", provider: nil)
        }
        let home = fm.homeDirectoryForCurrentUser
        let iCloud = home.appendingPathComponent("Library/Mobile Documents/com~apple~CloudDocs")
        var result = disks
        if fm.fileExists(atPath: iCloud.path) { result.append(BackupLocation(url: iCloud, title: "iCloud Drive", provider: "iCloud Drive")) }
        let cloud = home.appendingPathComponent("Library/CloudStorage")
        for url in (try? fm.contentsOfDirectory(at: cloud, includingPropertiesForKeys: [.isDirectoryKey], options: [.skipsHiddenFiles])) ?? [] {
            guard (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true,
                  let provider = provider(for: url) else { continue }
            result.append(BackupLocation(url: url, title: provider, provider: provider))
        }
        return result
    }
}

struct AppSettingsView: View {
    @EnvironmentObject var store: OrganizerStore
    @LocalState var draft = UserPreferences.read()
    @LocalState var message = ""
    @LocalState var backupLocations: [BackupLocation] = []
    @LocalState var pendingCloud: BackupLocation?
    @LocalState var confirmCloud = false
    /// Igloo Dev names its own folder, so a tester can see that the release settings stay unchanged.
    var settingsLocation: String {
        if store.isDemo || UserPreferences.appIdentifier == ConfigRoot.releaseIdentifier {
            return "Settings live in ~/.config/igloo/config.json."
        }
        return "Igloo Dev settings live in " + ConfigRoot.displayPath(UserPreferences.file)
            + ". The installed Igloo keeps its settings in ~/.config/igloo."
    }
    var backupTitle: String {
        if draft.freezerBackupRoot.isEmpty { return "None" }
        let url = URL(fileURLWithPath: draft.freezerBackupRoot)
        return BackupLocations.provider(for: url) ?? backupLocations.first(where: { $0.url.path == url.path })?.title ?? "Chosen folder"
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Igloo Settings").font(.title2).fontWeight(.semibold)
            Form {
                pathField("Projects root", value: $draft.projectsRoot)
                pathField("Freezer", value: $draft.archiveRoot)
                HStack {
                    Text("Freezer backup folder")
                    Spacer()
                    Menu(backupTitle) {
                        Button("None") { draft.freezerBackupRoot = ""; draft.freezerBackupCloudAcceptedRoot = "" }
                        ForEach(backupLocations) { location in Button(location.title) { chooseBackup(location) } }
                        Divider()
                        Button("Choose…") { chooseBackupFolder() }
                    }.accessibilityLabel(Text("Freezer backup folder, " + backupTitle))
                }
                pathField("Runtime state", value: $draft.stateRoot)
                pathField("Default clone folder", value: $draft.cloneRoot)
                Stepper("Quiet project threshold: \(draft.quietDays) days", value: $draft.quietDays, in: 1...3650)
                Section("Quiet-project reminder") {
                    Toggle("Remind me about quiet projects", isOn: $draft.quietReminderEnabled)
                        .onChange(of: draft.quietReminderEnabled) { _, enabled in
                            guard enabled, !store.isDemo else { return }
                            UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, error in
                                DispatchQueue.main.async {
                                    if let error {
                                        message = "Notification permission could not be requested: \(error.localizedDescription)"
                                    } else if granted {
                                        message = "Notification permission is ready. Save settings to enable the reminder."
                                    } else {
                                        message = "Notifications are disabled. You can allow them in System Settings."
                                    }
                                }
                            }
                        }
                    if draft.quietReminderEnabled {
                        Picker("Weekday", selection: $draft.quietReminderWeekday) {
                            Text("Sunday").tag(0)
                            Text("Monday").tag(1)
                            Text("Tuesday").tag(2)
                            Text("Wednesday").tag(3)
                            Text("Thursday").tag(4)
                            Text("Friday").tag(5)
                            Text("Saturday").tag(6)
                        }
                        TextField("Time (24-hour HH:MM)", text: $draft.quietReminderTime)
                        Text("Igloo checks the saved catalog and sends one notification when projects pass the Freeze age. It does not change project files.")
                            .font(.caption).foregroundStyle(.secondary)
                            .frame(maxWidth: 420, alignment: .leading).fixedSize(horizontal: false, vertical: true)
                    }
                }
                TextField("GitHub owner default", text: $draft.githubOwner, prompt: Text("Blank uses your authenticated personal account"))
                DisclosureGroup("Installed tools") {
                    if UserPreferences.bundledPython != nil { Text("Python is included with Igloo.").foregroundStyle(.secondary) }
                    else { TextField("Python executable", text: $draft.pythonExecutable) }
                    TextField("Git executable", text: $draft.gitExecutable)
                    TextField("GitHub CLI executable", text: $draft.githubExecutable)
                }
            }
            Text(settingsLocation + " Changing storage paths selects another store; existing data stays at its old location.")
                .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            if !message.isEmpty { Text(message).font(.callout) }
            HStack {
                Button("Reload") { draft = store.isDemo ? store.preferences : UserPreferences.read(); message = "" }
                Spacer()
                Button("Save settings") {
                    do {
                        let valid = try draft.validated()
                        try valid.write()
                        draft = valid; store.preferences = valid; store.quietDays = valid.quietDays
                        store.selection = nil
                        store.syncReminderAgent { error in
                            if let error {
                                message = "Settings saved, but the reminder schedule could not be updated: \(error)"
                            } else {
                                store.load()
                                message = "Saved. Run Analyze Projects after changing workspaces."
                            }
                        }
                    } catch { message = error.localizedDescription }
                }.buttonStyle(WizardPrimaryButtonStyle()).disabled(store.busy || store.isDemo)
            }
        }.padding(24).frame(width: 760)
            .onAppear {
                draft = store.preferences
                backupLocations = store.isDemo
                    ? [BackupLocation(url: URL(fileURLWithPath: "/Users/demo/Library/CloudStorage/Example-account"),
                                      title: "Cloud storage", provider: "Cloud storage")]
                    : BackupLocations.detected()
                if store.isDemo && DemoCapture.options?.view == "settings-cloud" {
                    pendingCloud = BackupLocation(url: URL(fileURLWithPath: "/Users/demo/Library/CloudStorage/Example-account"),
                                                  title: "Cloud storage", provider: "Cloud storage")
                    confirmCloud = true
                }
            }
            .confirmationDialog("Confirm cloud backup", isPresented: $confirmCloud, titleVisibility: .visible) {
                Button("Allow unencrypted upload") {
                    if let location = pendingCloud {
                        draft.freezerBackupRoot = location.url.path
                        draft.freezerBackupCloudAcceptedRoot = BackupLocations.identity(for: location.url)
                    }
                    pendingCloud = nil
                }
                Button("Cancel", role: .cancel) { pendingCloud = nil }
            } message: {
                Text("Backups include local-only files such as environment and key files. They are uploaded unencrypted to " + (pendingCloud?.provider ?? "cloud storage") + ".")
            }
    }
    private func chooseBackup(_ location: BackupLocation) {
        let identity = BackupLocations.identity(for: location.url)
        if location.provider != nil && identity != draft.freezerBackupCloudAcceptedRoot {
            pendingCloud = location; confirmCloud = true
        } else {
            draft.freezerBackupRoot = location.url.path
            if location.provider == nil { draft.freezerBackupCloudAcceptedRoot = "" }
        }
    }
    private func chooseBackupFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true; panel.canChooseFiles = false; panel.canCreateDirectories = true
        if panel.runModal() == .OK, let url = panel.url {
            let provider = BackupLocations.provider(for: url)
            chooseBackup(BackupLocation(url: url, title: provider ?? "Chosen folder", provider: provider))
        }
    }
    private func pathField(_ title: String, value: Binding<String>) -> some View {
        HStack {
            TextField(title, text: value)
            Button("Choose…") {
                let panel = NSOpenPanel()
                panel.canChooseDirectories = true; panel.canChooseFiles = false; panel.canCreateDirectories = true
                panel.directoryURL = URL(fileURLWithPath: (value.wrappedValue as NSString).expandingTildeInPath)
                if panel.runModal() == .OK, let url = panel.url { value.wrappedValue = url.path }
            }
        }
    }
}

struct GitHubRepository: Decodable, Identifiable {
    var id: String; var name: String; var owner: String; var slug: String; var url: String
    var isPrivate: Bool; var archived: Bool; var description: String; var pushedAt: String
    var alreadyLocal: Bool; var localPaths: [String]
}
struct RepositoryListing: Decodable {
    var repositories: [GitHubRepository]; var owner: String; var truncated: Bool; var message: String
}
struct CloneItem: Decodable, Identifiable {
    var slug: String; var path: String; var url: String
    var id: String { slug }
}
struct CloneReview: Decodable {
    var id: String; var created: String; var parent: String; var items: [CloneItem]
}
struct CloneReply: Decodable { var clones: [ActionResult]; var message: String }

struct RemoteRepositoriesView: View {
    @EnvironmentObject var store: OrganizerStore
    @LocalState var owner = ""
    @LocalState var owners: [String] = []
    @LocalState var repositories: [GitHubRepository] = []
    @LocalState var selected = Set<String>()
    @LocalState var search = ""
    @LocalState var hideLocal = true
    @LocalState var parent = ""
    @LocalState var message = ""
    @LocalState var plan: CloneReview?
    @LocalState var confirmed = false
    @LocalState var results: [ActionResult] = []
    @LocalState var progress = ""
    var captureLoading = false
    var visible: [GitHubRepository] {
        repositories.filter {
            (!hideLocal || !$0.alreadyLocal) && (search.isEmpty || ($0.slug + " " + $0.description).localizedCaseInsensitiveContains(search))
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("GitHub repositories").font(.title2).fontWeight(.semibold)
            if let review = plan {
                Text("Clone \(review.items.count) repositories into " + review.parent).font(.callout)
                Table(review.items) {
                    TableColumn("Repository", value: \.slug).width(min: 250)
                    TableColumn("New local path", value: \.path).width(min: 300)
                }
                Text("Existing folders are never overwritten. These are normal clones; dependencies and project commands will not run.")
                    .font(.callout).foregroundStyle(.secondary)
                Toggle("Clone these repositories into the listed new folders.", isOn: $confirmed)
                HStack {
                    Button("Back") { plan = nil; confirmed = false }
                    if store.busy { SheetProgressRow(text: store.status) }
                    Spacer()
                    Button("Clone selected repositories") {
                        store.invoke(["clone-execute", review.id], as: CloneReply.self) { reply in
                            results = reply.clones; message = reply.message; plan = nil; selected = []
                            let paths = Set(reply.clones.filter(\.ok).map(\.path))
                            for i in repositories.indices {
                                let path = (review.items.first { $0.slug == repositories[i].slug })?.path ?? ""
                                if paths.contains(path) { repositories[i].alreadyLocal = true; repositories[i].localPaths.append(path) }
                            }
                            store.load()
                        }
                    }.buttonStyle(WizardPrimaryButtonStyle()).disabled(store.busy || !confirmed)
                }
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        TextField("GitHub account or organization", text: $owner)
                        if !owners.isEmpty {
                            Menu("My accounts") { ForEach(owners, id: \.self) { value in Button(value) { owner = value } } }
                        }
                        Button("Load repositories") { loadRepositories() }.disabled(store.busy || !progress.isEmpty || owner.isEmpty)
                    }
                    if progress.isEmpty {
                        Text("Choose an account or organization.").font(.caption).foregroundStyle(.secondary)
                    } else {
                        SheetProgressRow(text: progress)
                    }
                }
                HStack {
                    TextField("Search repositories", text: $search)
                    Toggle("Hide already cloned", isOn: $hideLocal)
                        .help("This filter hides repositories that already have a local clone. Turn it off to show them.")
                }
                Table(visible, selection: $selected) {
                    TableColumn("Repository", value: \.slug).width(min: 240)
                    TableColumn("Visibility") { Text($0.isPrivate ? "Private" : "Public") }.width(90)
                    TableColumn("Local state") { Text($0.alreadyLocal ? "Already cloned" : $0.archived ? "Archived remote" : "Not cloned here") }.width(130)
                    TableColumn("Description", value: \.description).width(min: 250)
                }.overlay {
                    if visible.isEmpty && !progress.isEmpty {
                        ProgressView(progress).accessibilityIdentifier("repositories.loading")
                    } else if visible.isEmpty {
                        ContentUnavailableView(emptyTitle, systemImage: "network",
                                               description: Text(emptyDescription))
                    }
                }
                LabeledContent("Clone folder") {
                    HStack {
                        TextField("Local parent folder", text: $parent)
                        Button("Choose…") {
                            let panel = NSOpenPanel()
                            panel.canChooseDirectories = true; panel.canChooseFiles = false
                            panel.directoryURL = URL(fileURLWithPath: parent)
                            if panel.runModal() == .OK, let url = panel.url { parent = url.path }
                        }
                    }
                }
                Text(message).font(.caption).foregroundStyle(.secondary)
                if !results.isEmpty {
                    Text(results.map { ($0.ok ? "Cloned: " : "Not cloned: ") + $0.path }.joined(separator: "\n")).font(.caption).lineLimit(4)
                }
                HStack {
                    Button("Done") { store.repositoriesShown = false }.keyboardShortcut(.cancelAction)
                    if store.busy && progress.isEmpty { SheetProgressRow(text: store.status) }
                    Spacer()
                    Text("\(visible.count) shown · \(selected.count) selected").font(.caption)
                    Button("Review clones…") {
                        let slugs = visible.filter { selected.contains($0.id) }.map(\.slug)
                        store.invoke(["clone-preview", parent] + slugs, as: CloneReview.self) { plan = $0 }
                    }.buttonStyle(WizardPrimaryButtonStyle()).disabled(store.busy || selected.isEmpty)
                        .help("\(selected.count) selected. Select one or more repositories to enable this review.")
                }
            }
        }.padding(24).frame(width: sheetWidth(1120), height: 720)
            .sheetErrorAlert(store)
            .onAppear {
                parent = store.preferences.cloneRoot
                owner = store.preferences.githubOwner
                if captureLoading { progress = "Loading repositories for \(owner)…"; return }
                guard !store.busy else { return }
                progress = "Checking the GitHub CLI…"
                store.invoke(["github-status"], as: GitHubStatus.self, failed: { _ in progress = "" }) { status in
                    progress = ""
                    owners = status.owners ?? [status.login]
                    if owner.isEmpty { owner = status.login }
                    message = status.message
                    if !owner.isEmpty { loadRepositories() }
                }
            }
    }
    private var emptyTitle: String {
        if repositories.isEmpty { return "No repositories loaded" }
        if !search.isEmpty { return "No matching repositories" }
        if hideLocal { return "All repositories are already cloned" }
        return "No repositories found"
    }
    private var emptyDescription: String {
        if repositories.isEmpty {
            return owner.isEmpty ? "Choose an account or organization, then load its repositories." : "Choose Load repositories to see this account or organization's repositories."
        }
        if !search.isEmpty { return "Clear the search field to see the loaded repositories." }
        if hideLocal { return "Turn off Hide already cloned to show these repositories." }
        return "Load repositories for another account or organization."
    }
    private func loadRepositories() {
        guard !store.busy else { return }
        progress = "Loading repositories for \(owner)…"
        store.invoke(["github-repos", owner], as: RepositoryListing.self, failed: { _ in progress = "" }) { listing in
            progress = ""
            repositories = listing.repositories; selected = []; results = []
            message = listing.message + (listing.truncated ? " Showing the first 1,000 repositories." : "")
        }
    }
}
