import Foundation

enum UpdateInstaller {
    static func start(archive: URL, update: AvailableUpdate,
                      onReady: @escaping () -> Void,
                      onFailure: @escaping (String) -> Void) throws -> Process {
        guard Bundle.main.bundleIdentifier == "local.igloo" else {
            throw UpdateError.invalid("Igloo Dev cannot install release updates.")
        }
        guard let helper = Bundle.main.resourceURL?.appendingPathComponent("update_installer.py"),
              FileManager.default.isReadableFile(atPath: helper.path) else {
            throw UpdateError.invalid("The update installer is missing from this app.")
        }
        let fileManager = FileManager.default
        let appRoot = Bundle.main.bundleURL.standardizedFileURL.resolvingSymlinksInPath()
        let helperWorkspace = fileManager.temporaryDirectory
            .appendingPathComponent("Igloo-update-helper-" + UUID().uuidString, isDirectory: true)
        do {
            try fileManager.createDirectory(at: helperWorkspace, withIntermediateDirectories: false,
                                            attributes: [.posixPermissions: 0o700])
            let copiedHelper = helperWorkspace.appendingPathComponent("update_installer.py")
            try fileManager.copyItem(at: helper, to: copiedHelper)

            let preferredPython = update.asset.runtime == "bundled" ? UserPreferences.bundledPython : nil
            let configuredPython = update.asset.runtime == "system"
                ? (UserPreferences.read().pythonExecutable as NSString).expandingTildeInPath
                : (preferredPython ?? "/usr/bin/python3")
            var python = configuredPython
            let configuredURL = URL(fileURLWithPath: configuredPython).standardizedFileURL.resolvingSymlinksInPath()
            if configuredURL.path == appRoot.path || configuredURL.path.hasPrefix(appRoot.path + "/") {
                if fileManager.isExecutableFile(atPath: "/usr/bin/python3") {
                    python = "/usr/bin/python3"
                } else if let bundled = preferredPython {
                    let architecture = URL(fileURLWithPath: bundled).deletingLastPathComponent().deletingLastPathComponent()
                    let copiedRuntime = helperWorkspace.appendingPathComponent("python", isDirectory: true)
                    try fileManager.copyItem(at: architecture, to: copiedRuntime)
                    python = copiedRuntime.appendingPathComponent("bin/python3").path
                }
            }
            guard fileManager.isExecutableFile(atPath: python) else {
                throw UpdateError.invalid("The Python runtime for this Igloo app is unavailable.")
            }

            let process = Process()
            let output = Pipe()
            let errors = Pipe()
            process.executableURL = URL(fileURLWithPath: python)
            process.arguments = [
                "-E", "-s", "-B", copiedHelper.path,
                "--archive", archive.path,
                "--sha256", update.asset.sha256,
                "--version", update.manifest.version,
                "--build", String(update.manifest.build),
                "--runtime", update.asset.runtime,
                "--target", Bundle.main.bundleURL.path,
                "--parent-pid", String(ProcessInfo.processInfo.processIdentifier),
                "--helper-workspace", helperWorkspace.path
            ]
            process.standardOutput = output
            process.standardError = errors
            do {
                try process.run()
                try? output.fileHandleForWriting.close()
                try? errors.fileHandleForWriting.close()
            } catch {
                try? fileManager.removeItem(at: helperWorkspace)
                throw UpdateError.invalid("The update installer could not be started.")
            }

            DispatchQueue.global(qos: .userInitiated).async {
                let reader = output.fileHandleForReading
                var line = Data()
                var ready = false
                while true {
                    let byte = reader.readData(ofLength: 1)
                    guard let value = byte.first else { break }
                    if value == 10 {
                        ready = String(data: line, encoding: .utf8) == "READY"
                        break
                    }
                    line.append(value)
                }
                if ready {
                    DispatchQueue.main.async(execute: onReady)
                    process.waitUntilExit()
                    guard process.terminationStatus != 0 else { return }
                    let message = String(data: errors.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
                        .trimmingCharacters(in: .whitespacesAndNewlines)
                    let failure = message.flatMap { $0.isEmpty ? nil : $0 } ??
                        "The update could not be completed. Igloo is still running."
                    DispatchQueue.main.async {
                        onFailure(failure)
                    }
                    return
                }
                let message = String(data: errors.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                process.waitUntilExit()
                let failure = message.flatMap { $0.isEmpty ? nil : $0 } ??
                    "The update could not be prepared. Igloo is still running."
                DispatchQueue.main.async {
                    onFailure(failure)
                }
            }
            return process
        } catch let error as UpdateError {
            try? fileManager.removeItem(at: helperWorkspace)
            throw error
        } catch {
            try? fileManager.removeItem(at: helperWorkspace)
            throw UpdateError.invalid("The update installer could not be prepared.")
        }
    }
}
