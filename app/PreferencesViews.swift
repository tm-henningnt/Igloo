import SwiftUI
import AppKit

struct UserPreferences: Codable {
    var version = 1
    var projectsRoot: String
    var archiveRoot: String
    var stateRoot: String
    var cloneRoot: String
    var quietDays = 60
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
    static var file: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/igloo/config.json")
    }
    static func defaults() -> UserPreferences {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let root = home + "/Projects"
        let python = bundledPython ?? ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"].first { FileManager.default.isExecutableFile(atPath: $0) } ?? "/usr/bin/python3"
        let gh = ["/opt/homebrew/bin/gh", "/usr/local/bin/gh"].first { FileManager.default.isExecutableFile(atPath: $0) } ?? "/opt/homebrew/bin/gh"
        return UserPreferences(projectsRoot: root, archiveRoot: home + "/.igloo/vault",
                               stateRoot: home + "/.igloo", cloneRoot: root,
                               pythonExecutable: python, githubExecutable: gh)
    }
    static func read() -> UserPreferences {
        let base = defaults()
        guard let data = try? Data(contentsOf: file),
              let values = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let baseData = try? JSONEncoder().encode(base),
              var merged = try? JSONSerialization.jsonObject(with: baseData) as? [String: Any] else { return base }
        for (key, value) in values { merged[key] = value }
        guard let full = try? JSONSerialization.data(withJSONObject: merged),
              let decoded = try? JSONDecoder().decode(UserPreferences.self, from: full) else { return base }
        return decoded
    }
    func validated() throws -> UserPreferences {
        var p = self
        func expand(_ value: String) -> String { (value as NSString).expandingTildeInPath }
        p.projectsRoot = expand(p.projectsRoot); p.archiveRoot = expand(p.archiveRoot)
        p.stateRoot = expand(p.stateRoot); p.cloneRoot = expand(p.cloneRoot)
        p.pythonExecutable = Self.bundledPython ?? expand(p.pythonExecutable); p.gitExecutable = expand(p.gitExecutable)
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
        guard FileManager.default.isExecutableFile(atPath: p.pythonExecutable),
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

struct AppSettingsView: View {
    @EnvironmentObject var store: OrganizerStore
    @LocalState var draft = UserPreferences.read()
    @LocalState var message = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Igloo Settings").font(.title2).fontWeight(.semibold)
            Form {
                pathField("Projects root", value: $draft.projectsRoot)
                pathField("Archive vault", value: $draft.archiveRoot)
                pathField("Runtime state", value: $draft.stateRoot)
                pathField("Default clone folder", value: $draft.cloneRoot)
                Stepper("Quiet project threshold: \(draft.quietDays) days", value: $draft.quietDays, in: 1...3650)
                TextField("GitHub owner default", text: $draft.githubOwner, prompt: Text("Blank uses your authenticated personal account"))
                DisclosureGroup("Installed tools") {
                    if UserPreferences.bundledPython != nil { Text("Python is included with Igloo.").foregroundStyle(.secondary) }
                    else { TextField("Python executable", text: $draft.pythonExecutable) }
                    TextField("Git executable", text: $draft.gitExecutable)
                    TextField("GitHub CLI executable", text: $draft.githubExecutable)
                }
            }
            Text("Settings live in ~/.config/igloo/config.json. Changing storage paths selects another store; existing data stays at its old location.")
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
                        store.selection = nil; store.load()
                        message = "Saved. Run Analyse Projects after changing workspaces."
                    } catch { message = error.localizedDescription }
                }.buttonStyle(.borderedProminent).disabled(store.busy || store.isDemo)
            }
        }.padding(24).frame(width: 760).onAppear { draft = store.preferences }
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
    @LocalState var message = "Choose an account or organization."
    @LocalState var plan: CloneReview?
    @LocalState var confirmed = false
    @LocalState var results: [ActionResult] = []
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
                    }.buttonStyle(.borderedProminent).disabled(store.busy || !confirmed)
                }
            } else {
                HStack {
                    TextField("GitHub account or organization", text: $owner)
                    if !owners.isEmpty {
                        Menu("My accounts") { ForEach(owners, id: \.self) { value in Button(value) { owner = value } } }
                    }
                    Button("Load repositories") { loadRepositories() }.disabled(store.busy || owner.isEmpty)
                }
                HStack {
                    TextField("Search repositories", text: $search)
                    Toggle("Hide already cloned", isOn: $hideLocal)
                }
                Table(visible, selection: $selected) {
                    TableColumn("Repository", value: \.slug).width(min: 240)
                    TableColumn("Visibility") { Text($0.isPrivate ? "Private" : "Public") }.width(90)
                    TableColumn("Local state") { Text($0.alreadyLocal ? "Already cloned" : $0.archived ? "Archived remote" : "Not cloned here") }.width(130)
                    TableColumn("Description", value: \.description).width(min: 250)
                }
                HStack {
                    TextField("Clone into", text: $parent)
                    Button("Choose…") {
                        let panel = NSOpenPanel()
                        panel.canChooseDirectories = true; panel.canChooseFiles = false
                        panel.directoryURL = URL(fileURLWithPath: parent)
                        if panel.runModal() == .OK, let url = panel.url { parent = url.path }
                    }
                }
                Text(message).font(.caption).foregroundStyle(.secondary)
                if !results.isEmpty {
                    Text(results.map { ($0.ok ? "Cloned: " : "Held: ") + $0.path }.joined(separator: "\n")).font(.caption).lineLimit(4)
                }
                HStack {
                    Button("Done") { store.repositoriesShown = false }.keyboardShortcut(.cancelAction)
                    Spacer()
                    Text("\(visible.count) shown · \(selected.count) selected").font(.caption)
                    Button("Review clones…") {
                        let slugs = visible.filter { selected.contains($0.id) }.map(\.slug)
                        store.invoke(["clone-preview", parent] + slugs, as: CloneReview.self) { plan = $0 }
                    }.buttonStyle(.borderedProminent).disabled(store.busy || selected.isEmpty)
                }
            }
        }.padding(24).frame(width: 1120, height: 720)
            .onAppear {
                parent = store.preferences.cloneRoot
                owner = store.preferences.githubOwner
                store.invoke(["github-status"], as: GitHubStatus.self) { status in
                    owners = status.owners ?? [status.login]
                    if owner.isEmpty { owner = status.login }
                    message = status.message
                    if !owner.isEmpty { loadRepositories() }
                }
            }
    }
    private func loadRepositories() {
        store.invoke(["github-repos", owner], as: RepositoryListing.self) { listing in
            repositories = listing.repositories; selected = []; results = []
            message = listing.message + (listing.truncated ? " Showing the first 1,000 repositories." : "")
        }
    }
}
