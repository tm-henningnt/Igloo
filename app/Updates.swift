import Foundation
import CryptoKit

enum UpdateError: LocalizedError {
    case invalid(String)
    var errorDescription: String? {
        switch self { case .invalid(let message): return message }
    }
}

struct AppVersion: Comparable {
    let parts: [Int]
    init(_ text: String) throws {
        let fields = text.split(separator: ".", omittingEmptySubsequences: false)
        guard (2...3).contains(fields.count),
              fields.allSatisfy({ !$0.isEmpty && $0.allSatisfy(\.isNumber) && $0.count <= 6 }),
              fields.allSatisfy({ $0.count == 1 || !$0.hasPrefix("0") }) else {
            throw UpdateError.invalid("The release has an invalid version.")
        }
        parts = try fields.map {
            guard let value = Int($0) else { throw UpdateError.invalid("Invalid version.") }
            return value
        } + Array(repeating: 0, count: 3 - fields.count)
    }
    static func < (lhs: Self, rhs: Self) -> Bool { lhs.parts.lexicographicallyPrecedes(rhs.parts) }
}

struct ReleaseAsset: Decodable {
    var name: String
    var size: Int64
    var browser_download_url: String
}
struct GitHubRelease: Decodable {
    var tag_name: String
    var html_url: String
    var body: String?
    var draft: Bool
    var prerelease: Bool
    var assets: [ReleaseAsset]
}
struct UpdateAsset: Codable {
    var architecture: String
    var runtime: String = "bundled"
    var filename: String
    var url: String
    var size: Int64
    var sha256: String
}
struct UpdateManifest: Codable {
    var schemaVersion: Int
    var repository: String
    var tag: String
    var version: String
    var build: Int
    var minimumSystemVersion: String
    var assets: [UpdateAsset]
}
struct AvailableUpdate {
    var manifest: UpdateManifest
    var asset: UpdateAsset
    var notes: String
    var releaseURL: URL
}

enum UpdatePolicy {
    static let repository = "tm-henningnt/Igloo"
    static let website = URL(string: "https://tm-henningnt.github.io/Igloo/")!
    static let releasesPage = URL(string: "https://github.com/" + repository + "/releases")!
    static let manifestName = "igloo-update.json"
    static let api = URL(string: "https://api.github.com/repos/" + repository + "/releases/latest")!
    static var architecture: String {
        #if arch(arm64)
        return "arm64"
        #else
        return "x86_64"
        #endif
    }
    static func releaseURL(_ tag: String) -> String {
        "https://github.com/" + repository + "/releases/tag/" + tag
    }
    static func filename(version: String, architecture: String, runtime: String) -> String {
        "Igloo-" + version + "-" + architecture + (runtime == "system" ? "-system-python" : "") + ".zip"
    }
    static func assetURL(tag: String, filename: String) -> String {
        "https://github.com/" + repository + "/releases/download/" + tag + "/" + filename
    }
    static func safeTransport(_ url: URL) -> Bool {
        url.scheme == "https" && url.user == nil && url.password == nil &&
        (url.port == nil || url.port == 443) &&
        ["api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"].contains(url.host ?? "")
    }
    static func validate(_ manifest: UpdateManifest, release: GitHubRelease, architecture: String,
                         currentVersion: String, currentBuild: Int, systemVersion: String, runtime: String = "bundled") throws -> AvailableUpdate? {
        let version = try AppVersion(manifest.version)
        guard manifest.schemaVersion == 1, manifest.repository == repository,
              manifest.tag == "v" + manifest.version, release.tag_name == manifest.tag,
              !release.draft, !release.prerelease, manifest.build > 0,
              release.html_url == releaseURL(manifest.tag),
              (try AppVersion(manifest.minimumSystemVersion)) <= (try AppVersion(systemVersion)),
              !manifest.assets.isEmpty, manifest.assets.count <= 6 else {
            throw UpdateError.invalid("This release is incomplete or requires a newer version of macOS.")
        }
        let architectures = manifest.assets.map { $0.architecture + "|" + $0.runtime }
        guard Set(architectures).count == architectures.count else {
            throw UpdateError.invalid("The release contains conflicting downloads.")
        }
        for asset in manifest.assets {
            guard ["arm64", "x86_64", "universal"].contains(asset.architecture),
                  ["bundled", "system"].contains(asset.runtime),
                  asset.filename == filename(version: manifest.version, architecture: asset.architecture, runtime: asset.runtime),
                  asset.url == assetURL(tag: manifest.tag, filename: asset.filename),
                  asset.size > 0, asset.size <= 512 * 1024 * 1024,
                  asset.sha256.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil,
                  release.assets.filter({ $0.name == asset.filename && $0.size == asset.size &&
                      $0.browser_download_url == asset.url }).count == 1 else {
                throw UpdateError.invalid("The release download could not be verified.")
            }
        }
        guard let selected = manifest.assets.first(where: { $0.architecture == architecture && $0.runtime == runtime }) ??
                manifest.assets.first(where: { $0.architecture == "universal" && $0.runtime == runtime }) else {
            throw UpdateError.invalid("This release has no download for your Mac.")
        }
        let installed = try AppVersion(currentVersion)
        guard version > installed || version == installed && manifest.build > currentBuild else { return nil }
        return AvailableUpdate(manifest: manifest, asset: selected,
                               notes: String((release.body ?? "").prefix(30_000)),
                               releaseURL: URL(string: release.html_url)!)
    }
    static func verify(file: URL, asset: UpdateAsset) throws {
        let handle = try FileHandle(forReadingFrom: file)
        defer { try? handle.close() }
        var count: Int64 = 0
        var hash = SHA256()
        while let chunk = try handle.read(upToCount: 65_536), !chunk.isEmpty {
            count += Int64(chunk.count)
            guard count <= asset.size else { throw UpdateError.invalid("The download is larger than expected.") }
            hash.update(data: chunk)
        }
        guard count == asset.size, hash.finalize().map({ String(format: "%02x", $0) }).joined() == asset.sha256 else {
            throw UpdateError.invalid("The download failed its checksum check. Try again.")
        }
    }
}

final class UpdateRedirects: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(request.url.map(UpdatePolicy.safeTransport) == true ? request : nil)
    }
}

enum UpdateService {
    static func session() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 600
        config.httpShouldSetCookies = false
        config.urlCache = nil
        return URLSession(configuration: config, delegate: UpdateRedirects(), delegateQueue: nil)
    }
    static func request(_ url: URL) -> URLRequest {
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData)
        request.setValue("Igloo", forHTTPHeaderField: "User-Agent")
        return request
    }
    static func responseError(_ response: HTTPURLResponse) -> UpdateError {
        if response.statusCode == 403 {
            let remaining = response.value(forHTTPHeaderField: "x-ratelimit-remaining")?.trimmingCharacters(in: .whitespacesAndNewlines)
            if remaining == "0" {
                if let raw = response.value(forHTTPHeaderField: "x-ratelimit-reset")?.trimmingCharacters(in: .whitespacesAndNewlines),
                   let seconds = TimeInterval(raw), seconds.isFinite, seconds > 0, seconds <= 253_402_300_799 {
                    let formatter = DateFormatter()
                    formatter.locale = Locale(identifier: "en_US_POSIX")
                    formatter.calendar = Calendar(identifier: .gregorian)
                    formatter.timeZone = TimeZone(secondsFromGMT: 0)!
                    formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss'Z'"
                    let reset = formatter.string(from: Date(timeIntervalSince1970: seconds))
                    return .invalid("GitHub's update request limit has been reached. Try again after " + reset + ".")
                }
                return .invalid("GitHub's update request limit has been reached. Try again later.")
            }
            return .invalid("GitHub denied this update request. Try again later.")
        }
        if (500...599).contains(response.statusCode) {
            return .invalid("GitHub is having trouble serving updates. Try again later.")
        }
        return .invalid("GitHub could not serve the update. Try again later.")
    }
    static func transportError(_ error: URLError) -> Error {
        switch error.code {
        case .notConnectedToInternet, .cannotFindHost, .networkConnectionLost:
            return UpdateError.invalid("You appear to be offline. Check your internet connection and try again.")
        case .timedOut:
            return UpdateError.invalid("The update request timed out. Check your connection and try again.")
        default:
            return error
        }
    }
    static func decode<T: Decodable>(_ type: T.Type, data: Data) throws -> T {
        do { return try JSONDecoder().decode(type, from: data) }
        catch is DecodingError { throw UpdateError.invalid("The update information is invalid. Try again later.") }
    }
    static func data(_ url: URL, session: URLSession, maximum: Int) async throws -> Data? {
        do {
            guard UpdatePolicy.safeTransport(url) else { throw UpdateError.invalid("Invalid update address.") }
            let (stream, response) = try await session.bytes(for: request(url))
            guard let response = response as? HTTPURLResponse else { throw UpdateError.invalid("No update response.") }
            if response.statusCode == 404 && url == UpdatePolicy.api { return nil }
            guard response.statusCode == 200 else {
                throw responseError(response)
            }
            var data = Data()
            for try await byte in stream {
                try Task.checkCancellation()
                guard data.count < maximum else { throw UpdateError.invalid("The update information is too large.") }
                data.append(byte)
            }
            return data
        } catch let error as URLError { throw transportError(error) }
    }
    static func check(version: String, build: Int, system: String, runtime: String) async throws -> AvailableUpdate? {
        let session = session()
        defer { session.invalidateAndCancel() }
        guard let releaseData = try await data(UpdatePolicy.api, session: session, maximum: 1_048_576) else { return nil }
        let release = try decode(GitHubRelease.self, data: releaseData)
        guard !release.draft, !release.prerelease,
              release.tag_name.range(of: "^v[0-9]+\\.[0-9]+(?:\\.[0-9]+)?$", options: .regularExpression) != nil,
              release.html_url == UpdatePolicy.releaseURL(release.tag_name) else {
            throw UpdateError.invalid("The published release could not be verified.")
        }
        let matches = release.assets.filter { $0.name == UpdatePolicy.manifestName }
        guard matches.count == 1, let asset = matches.first, asset.size > 0, asset.size <= 65_536,
              asset.browser_download_url == UpdatePolicy.assetURL(tag: release.tag_name, filename: UpdatePolicy.manifestName),
              let url = URL(string: asset.browser_download_url),
              let manifestData = try await data(url, session: session, maximum: 65_536) else {
            throw UpdateError.invalid("This release does not include a complete update manifest.")
        }
        let manifest = try decode(UpdateManifest.self, data: manifestData)
        return try UpdatePolicy.validate(manifest, release: release, architecture: UpdatePolicy.architecture,
                                         currentVersion: version, currentBuild: build, systemVersion: system, runtime: runtime)
    }
    static func download(_ asset: UpdateAsset) async throws -> URL {
        do {
            guard let url = URL(string: asset.url), UpdatePolicy.safeTransport(url) else {
                throw UpdateError.invalid("Invalid download address.")
            }
            let session = session()
            defer { session.invalidateAndCancel() }
            let folder = FileManager.default.temporaryDirectory.appendingPathComponent("Igloo-update-" + UUID().uuidString)
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false,
                                                    attributes: [.posixPermissions: 0o700])
            defer { try? FileManager.default.removeItem(at: folder) }
            let staged = folder.appendingPathComponent(asset.filename)
            FileManager.default.createFile(atPath: staged.path, contents: nil, attributes: [.posixPermissions: 0o600])
            let handle = try FileHandle(forWritingTo: staged)
            defer { try? handle.close() }
            let (stream, response) = try await session.bytes(for: request(url))
            guard let response = response as? HTTPURLResponse else {
                throw UpdateError.invalid("The release download is unavailable.")
            }
            if response.statusCode != 200 {
                if response.statusCode == 403 || (500...599).contains(response.statusCode) { throw responseError(response) }
                throw UpdateError.invalid("The release download is unavailable.")
            }
            var buffer = Data()
            var count: Int64 = 0
            for try await byte in stream {
                try Task.checkCancellation()
                count += 1
                guard count <= asset.size else { throw UpdateError.invalid("The download is larger than expected.") }
                buffer.append(byte)
                if buffer.count == 65_536 { try handle.write(contentsOf: buffer); buffer.removeAll(keepingCapacity: true) }
            }
            if !buffer.isEmpty { try handle.write(contentsOf: buffer) }
            try handle.synchronize()
            try UpdatePolicy.verify(file: staged, asset: asset)
            try Task.checkCancellation()
            let downloads = try FileManager.default.url(for: .downloadsDirectory, in: .userDomainMask,
                                                        appropriateFor: nil, create: true)
            var destination = downloads.appendingPathComponent(asset.filename)
            if FileManager.default.fileExists(atPath: destination.path) {
                destination = downloads.appendingPathComponent(asset.filename.replacingOccurrences(of: ".zip", with: "-" + UUID().uuidString + ".zip"))
            }
            try FileManager.default.moveItem(at: staged, to: destination)
            return destination
        } catch let error as URLError { throw transportError(error) }
    }
}

enum ReleaseNotesMarkup {
    enum Kind: Equatable {
        case heading(Int)
        case bullet
        case paragraph
    }

    struct Block {
        let kind: Kind
        let text: AttributedString
    }

    static func parse(_ markdown: String) -> [Block] {
        var blocks: [Block] = []
        var paragraph: [String] = []

        func flushParagraph() {
            guard !paragraph.isEmpty else { return }
            blocks.append(Block(kind: .paragraph, text: inline(paragraph.joined(separator: "\n"))))
            paragraph.removeAll(keepingCapacity: true)
        }

        for rawLine in markdown.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(rawLine).trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty else {
                flushParagraph()
                continue
            }

            let headingLevel = line.prefix(while: { $0 == "#" }).count
            if (1...6).contains(headingLevel), line.dropFirst(headingLevel).first == " " {
                flushParagraph()
                let content = String(line.dropFirst(headingLevel + 1))
                blocks.append(Block(kind: .heading(headingLevel), text: inline(content)))
            } else if line.hasPrefix("- ") || line.hasPrefix("* ") || line.hasPrefix("• ") {
                flushParagraph()
                blocks.append(Block(kind: .bullet, text: inline(String(line.dropFirst(2)))))
            } else {
                paragraph.append(line)
            }
        }
        flushParagraph()
        return blocks
    }

    private static func inline(_ source: String) -> AttributedString {
        var result = (try? AttributedString(markdown: source,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(source)
        let unsafeLinkRanges = result.runs.compactMap { run -> Range<AttributedString.Index>? in
            guard let link = run.link else { return nil }
            guard link.scheme?.lowercased() == "https", link.host?.isEmpty == false,
                  link.user == nil, link.password == nil else { return run.range }
            return nil
        }
        for range in unsafeLinkRanges { result[range].link = nil }
        return result
    }
}

// MARK: - Interface
import SwiftUI
import AppKit

@MainActor final class AppUpdater: ObservableObject {
    @Published var status = "Check for a published Igloo release."
    @Published var checking = false
    @Published var downloading = false
    @Published var failed = false
    var failureReleaseURL: URL? { failed ? UpdatePolicy.releasesPage : nil }
    @Published var available: AvailableUpdate?
    @Published var downloaded: URL?
    var downloadTask: Task<Void, Never>?
    var demo = false
    static let demoFailureNames = ["quota", "quota-no-reset", "forbidden", "offline", "timeout", "server", "malformed", "cancelled-download", "digest-mismatch"]
    static func demoMarkdownUpdate() -> AppUpdater {
        let tag = "v0.2.1"
        let filename = UpdatePolicy.filename(version: "0.2.1", architecture: "universal", runtime: "bundled")
        let manifest = UpdateManifest(schemaVersion: 1, repository: UpdatePolicy.repository, tag: tag,
                                      version: "0.2.1", build: 15, minimumSystemVersion: "14.0", assets: [])
        let asset = UpdateAsset(architecture: "universal", filename: filename,
                                url: UpdatePolicy.assetURL(tag: tag, filename: filename), size: 128_000, sha256: "")
        let notes = """
        # Igloo 0.2.1

        - **Updated:** Release notes now use readable headings, bullets, and emphasis.
        - Review [the release guide](https://example.com/updates) for the change list.
        - [This non-HTTPS link](http://example.com/updates) is displayed as text.
        """
        let updater = AppUpdater()
        updater.demo = true
        updater.status = "A new version of Igloo is available."
        updater.available = AvailableUpdate(manifest: manifest, asset: asset, notes: notes,
                                            releaseURL: URL(string: UpdatePolicy.releaseURL(tag))!)
        return updater
    }
    static func demoFailure(_ name: String) -> AppUpdater? {
        guard demoFailureNames.contains(name) else { return nil }
        let updater = AppUpdater()
        updater.demo = true
        updater.failed = true
        let response: (Int, [String: String]) -> HTTPURLResponse = { status, headers in
            HTTPURLResponse(url: UpdatePolicy.api, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)!
        }
        switch name {
        case "quota": updater.status = UpdateService.responseError(response(403, ["x-ratelimit-remaining": "0", "x-ratelimit-reset": "1793145600"])).localizedDescription
        case "quota-no-reset": updater.status = UpdateService.responseError(response(403, ["x-ratelimit-remaining": "0"])).localizedDescription
        case "forbidden": updater.status = UpdateService.responseError(response(403, [:])).localizedDescription
        case "offline": updater.status = UpdateService.transportError(URLError(.notConnectedToInternet)).localizedDescription
        case "timeout": updater.status = UpdateService.transportError(URLError(.timedOut)).localizedDescription
        case "server": updater.status = UpdateService.responseError(response(503, [:])).localizedDescription
        case "malformed": updater.status = "The update information is invalid. Try again later."
        case "cancelled-download": updater.status = "Download canceled."
        default: updater.status = "The download failed its checksum check. Try again."
        }
        return updater
    }
    func check() async {
        guard !checking, !downloading else { return }
        checking = true
        failed = false
        available = nil
        downloaded = nil
        status = "Checking GitHub releases…"
        defer { checking = false }
        if demo { status = "Update checks are disabled in the example catalog."; return }
        do {
            let info = Bundle.main.infoDictionary ?? [:]
            available = try await UpdateService.check(version: info["CFBundleShortVersionString"] as? String ?? "0.0",
                                                      build: Int(info["CFBundleVersion"] as? String ?? "") ?? 0,
                                                      system: ProcessInfo.processInfo.operatingSystemVersionStringValue,
                                                      runtime: info["IglooRuntime"] as? String ?? "bundled")
            status = available == nil ? "No newer published release is available." : "A new version of Igloo is available."
        } catch {
            failed = true
            status = error.localizedDescription
        }
    }
    func download() {
        guard let update = available, !downloading else { return }
        downloading = true
        failed = false
        status = "Downloading and verifying Igloo…"
        downloadTask = Task {
            defer { downloading = false; downloadTask = nil }
            do {
                downloaded = try await UpdateService.download(update.asset)
                status = "Download verified. Open the ZIP, quit Igloo, and move the new app to Applications."
            } catch is CancellationError {
                failed = true
                status = "Download canceled."
            } catch let error as URLError where error.code == .cancelled {
                failed = true
                status = "Download canceled."
            }
            catch {
                failed = true
                status = error.localizedDescription
            }
        }
    }
}
extension ProcessInfo {
    var operatingSystemVersionStringValue: String {
        let v = operatingSystemVersion
        return "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
    }
}
struct UpdatesView: View {
    @EnvironmentObject var updater: AppUpdater
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Igloo updates").font(.title2).fontWeight(.semibold)
            if updater.failed {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange).accessibilityHidden(true)
                    Text(updater.status).font(.callout).textSelection(.enabled)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel(Text(updater.status))
            } else {
                Text(updater.status).font(.callout).textSelection(.enabled)
            }
            if let url = updater.failureReleaseURL { Link("View releases on GitHub", destination: url) }
            if updater.checking || updater.downloading { ProgressView().controlSize(.small) }
            if let update = updater.available {
                Text("Version \(update.manifest.version) · \(update.asset.architecture) · \(bytes(update.asset.size))")
                Text(update.asset.runtime == "bundled" ? "Python included" : "Uses your installed Python")
                    .font(.caption).foregroundStyle(.secondary)
                ScrollView {
                    ReleaseNotesView(markdown: update.notes.isEmpty ? "No release notes were provided." : update.notes)
                }
                .frame(height: 180)
                .accessibilityIdentifier("updates.releaseNotes")
                Link("Release on GitHub", destination: update.releaseURL)
            }
            Text("Updates come from published GitHub releases. Project files, settings, and local archives stay in place.")
                .font(.caption).foregroundStyle(.secondary)
            Divider()
            HStack {
                Button("Close") { dismiss() }.keyboardShortcut(.cancelAction)
                    .accessibilityIdentifier("updates.close")
                Spacer()
                if updater.downloading { Button("Cancel download") { updater.downloadTask?.cancel() } }
                else if let downloaded = updater.downloaded {
                    Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([downloaded]) }
                        .buttonStyle(.borderedProminent)
                } else if updater.available != nil {
                    Button("Download update") { updater.download() }.buttonStyle(.borderedProminent)
                        .disabled(updater.demo)
                        .accessibilityIdentifier("updates.download")
                }
                Button("Check again") { Task { await updater.check() } }
                    .disabled(updater.checking || updater.downloading)
                    .accessibilityIdentifier("updates.checkAgain")
            }
        }.padding(24).frame(width: 540)
    }
}


private struct ReleaseNotesView: View {
    let markdown: String
    private var blocks: [ReleaseNotesMarkup.Block] { ReleaseNotesMarkup.parse(markdown) }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                switch block.kind {
                case .heading(let level):
                    Text(block.text)
                        .font(level == 1 ? .title3 : .headline)
                        .textSelection(.enabled)
                case .bullet:
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("•").accessibilityHidden(true)
                        Text(block.text).textSelection(.enabled)
                    }.font(.callout)
                case .paragraph:
                    Text(block.text).font(.callout).textSelection(.enabled)
                }
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}
