"""Transport fixtures compile the production Swift updater and never use the network."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

def compile_update_check(root, checks, *, include_updater=False, compiler_flags=()):
    """Compile checks against the production updater, without the window view."""
    source = (Path(__file__).resolve().parents[1] / "app/Updates.swift").read_text()
    boundary = "struct UpdatesView: View" if include_updater else "// MARK: - Interface"
    (root / "main.swift").write_text(source.split(boundary)[0] + checks)
    executable = root / "check"
    build = subprocess.run(["/usr/bin/swiftc", *compiler_flags, root / "main.swift", "-o", executable],
                           capture_output=True, text=True, timeout=120)
    if build.returncode:
        raise AssertionError(build.stderr)
    return executable



SWIFT_FIXTURES = r'''
import ObjectiveC

enum FixtureReply {
    case response(Int, [String: String], Data)
    case failure(URLError.Code)
}

final class FixtureProtocol: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    private static var replies: [String: FixtureReply] = [:]
    private static var requests: [URLRequest] = []
    private static var unexpected: [String] = []

    static func configure(_ values: [String: FixtureReply]) {
        lock.lock(); defer { lock.unlock() }
        replies = values; requests = []; unexpected = []
    }
    static func assertRequests(_ urls: [URL]) {
        lock.lock(); defer { lock.unlock() }
        precondition(unexpected.isEmpty, "An unconfigured request was refused locally")
        precondition(requests.map { $0.url! } == urls, "Unexpected request sequence")
        for request in requests {
            precondition(request.value(forHTTPHeaderField: "User-Agent") == "Igloo")
            precondition(request.value(forHTTPHeaderField: "Authorization") == nil)
        }
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock()
        Self.requests.append(request)
        let reply = Self.replies[request.url!.absoluteString]
        if reply == nil { Self.unexpected.append(request.url!.absoluteString) }
        Self.lock.unlock()
        guard let reply else {
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
            return
        }
        switch reply {
        case .response(let status, let headers, let body):
            let response = HTTPURLResponse(url: request.url!, statusCode: status,
                                           httpVersion: "HTTP/1.1", headerFields: headers)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: body)
            client?.urlProtocolDidFinishLoading(self)
        case .failure(let code):
            client?.urlProtocol(self, didFailWithError: URLError(code))
        }
    }
    override func stopLoading() {}
}

extension URLSessionConfiguration {
    // Install a protocol on the real factory's ephemeral configuration. No
    // production source is rewritten, and no unhandled URL can reach a socket.
    @objc dynamic class func fixtureEphemeralConfiguration() -> URLSessionConfiguration {
        let configuration = fixtureEphemeralConfiguration()
        configuration.protocolClasses = [FixtureProtocol.self]
        return configuration
    }
}

func swapFixtureConfiguration() {
    let original = class_getClassMethod(URLSessionConfiguration.self,
        #selector(getter: URLSessionConfiguration.ephemeral))!
    let fixture = class_getClassMethod(URLSessionConfiguration.self,
        #selector(URLSessionConfiguration.fixtureEphemeralConfiguration))!
    method_exchangeImplementations(original, fixture)
}

struct ReleaseFixture {
    let payload = Data("fixture update archive".utf8)
    let asset: UpdateAsset
    let manifest: UpdateManifest
    let manifestURL: URL
    let replies: [String: FixtureReply]

    init(version: String = "0.2.0", build: Int = 14, malformed: Bool = false) throws {
        let tag = "v" + version
        let filename = UpdatePolicy.filename(version: version, architecture: "universal", runtime: "bundled")
        let url = UpdatePolicy.assetURL(tag: tag, filename: filename)
        let digest = SHA256.hash(data: payload).map { String(format: "%02x", $0) }.joined()
        asset = UpdateAsset(architecture: "universal", filename: filename, url: url,
                            size: Int64(payload.count), sha256: digest)
        manifest = UpdateManifest(schemaVersion: 1, repository: UpdatePolicy.repository,
            tag: tag, version: version, build: build, minimumSystemVersion: "14.0", assets: [asset])
        manifestURL = URL(string: UpdatePolicy.assetURL(tag: tag, filename: UpdatePolicy.manifestName))!
        let manifestData: Data
        if malformed { manifestData = Data("{broken manifest".utf8) }
        else { manifestData = try JSONEncoder().encode(manifest) }
        let release: [String: Any] = [
            "tag_name": tag, "html_url": UpdatePolicy.releaseURL(tag), "body": "Fixture release notes.",
            "draft": false, "prerelease": false,
            "assets": [
                ["name": UpdatePolicy.manifestName, "size": manifestData.count,
                 "browser_download_url": manifestURL.absoluteString],
                ["name": filename, "size": payload.count, "browser_download_url": url]
            ]
        ]
        replies = [
            UpdatePolicy.api.absoluteString: .response(200, [:], try JSONSerialization.data(withJSONObject: release)),
            manifestURL.absoluteString: .response(200, [:], manifestData)
        ]
    }
}

@main struct TransportChecks {
    static let quota = "GitHub's update request limit has been reached. Try again after 2026-10-28T00:00:00Z."
    static let quotaUnknown = "GitHub's update request limit has been reached. Try again later."
    static let forbiddenMessage = "GitHub denied this update request. Try again later."
    static let offlineMessage = "You appear to be offline. Check your internet connection and try again."
    static let timeoutMessage = "The update request timed out. Check your connection and try again."
    static let serverMessage = "GitHub is having trouble serving updates. Try again later."
    static let malformedMessage = "The update information is invalid. Try again later."

    static func failure(_ operation: () async throws -> Void) async -> Error {
        do { try await operation() }
        catch { return error }
        fatalError("Expected the updater to refuse the fixture")
    }
    static func data(_ url: URL = UpdatePolicy.api) async throws -> Data? {
        let session = UpdateService.session()
        defer { session.invalidateAndCancel() }
        return try await UpdateService.data(url, session: session, maximum: 1_048_576)
    }
    static func check() async throws -> AvailableUpdate? {
        try await UpdateService.check(version: "0.1.0", build: 13, system: "14.0", runtime: "bundled")
    }
    @MainActor static func transportFailure(_ code: URLError.Code, expected: String) async {
        for operation in [0, 1] {
            FixtureProtocol.configure([UpdatePolicy.api.absoluteString: .failure(code)])
            let error = await failure {
                if operation == 0 { _ = try await data() }
                else { _ = try await check() }
            }
            precondition(error is UpdateError)
            precondition(error.localizedDescription == expected)
            FixtureProtocol.assertRequests([UpdatePolicy.api])
        }
        FixtureProtocol.configure([UpdatePolicy.api.absoluteString: .failure(code)])
        let updater = AppUpdater()
        await updater.check()
        precondition(updater.status == expected && updater.failed)
        precondition(updater.failureReleaseURL?.absoluteString == "https://github.com/tm-henningnt/Igloo/releases")
        FixtureProtocol.assertRequests([UpdatePolicy.api])
    }
    @MainActor static func httpFailure(_ status: Int, _ headers: [String: String], expected: String) async {
        for operation in [0, 1] {
            FixtureProtocol.configure([UpdatePolicy.api.absoluteString: .response(status, headers, Data())])
            let error = await failure {
                if operation == 0 { _ = try await data() }
                else { _ = try await check() }
            }
            precondition(error is UpdateError)
            precondition(error.localizedDescription == expected)
            FixtureProtocol.assertRequests([UpdatePolicy.api])
        }
        FixtureProtocol.configure([UpdatePolicy.api.absoluteString: .response(status, headers, Data())])
        let updater = AppUpdater()
        await updater.check()
        precondition(updater.status == expected && updater.failed)
        precondition(updater.failureReleaseURL?.absoluteString == "https://github.com/tm-henningnt/Igloo/releases")
        FixtureProtocol.assertRequests([UpdatePolicy.api])
    }
    @MainActor static func main() async throws {
        swapFixtureConfiguration()
        defer { swapFixtureConfiguration() }
        let scenario = CommandLine.arguments[1]
        switch scenario {
        case "demo-states":
            let expected = ["quota": quota, "quota-no-reset": quotaUnknown, "forbidden": forbiddenMessage,
                            "offline": offlineMessage, "timeout": timeoutMessage, "server": serverMessage,
                            "malformed": malformedMessage, "cancelled-download": "Download canceled.",
                            "digest-mismatch": "The download failed its checksum check. Try again."]
            for (name, message) in expected {
                guard let updater = AppUpdater.demoFailure(name) else { fatalError("Missing demo state") }
                precondition(updater.status == message)
                precondition(updater.failed)
                precondition(updater.failureReleaseURL?.absoluteString == "https://github.com/tm-henningnt/Igloo/releases")
            }
            precondition(AppUpdater.demoFailure("unknown") == nil)
            FixtureProtocol.assertRequests([])
        case "markdown-notes":
            let notes = """
            # Release highlights

            - **Bold change** and `inline code`
            - [Secure link](https://example.com/releases)
            - [Plain unsafe link](http://example.com/releases)
            """
            let blocks = ReleaseNotesMarkup.parse(notes)
            precondition(blocks.map(\.kind) == [.heading(1), .bullet, .bullet, .bullet])
            precondition(String(blocks[0].text.characters) == "Release highlights")
            precondition(String(blocks[1].text.characters).contains("Bold change"))
            precondition(blocks[1].text.runs.contains {
                $0.inlinePresentationIntent?.contains(.stronglyEmphasized) == true
            })
            precondition(blocks[2].text.runs.compactMap(\.link) == [URL(string: "https://example.com/releases")!])
            precondition(String(blocks[3].text.characters) == "Plain unsafe link")
            precondition(blocks[3].text.runs.allSatisfy { $0.link == nil })
            FixtureProtocol.assertRequests([])
        case "no-release":
            FixtureProtocol.configure([UpdatePolicy.api.absoluteString: .response(404, [:], Data())])
            let releaseData = try await data()
            precondition(releaseData == nil)
            FixtureProtocol.assertRequests([UpdatePolicy.api])
            FixtureProtocol.configure([UpdatePolicy.api.absoluteString: .response(404, [:], Data())])
            let update = try await check()
            precondition(update == nil)
            FixtureProtocol.assertRequests([UpdatePolicy.api])
            FixtureProtocol.configure([UpdatePolicy.api.absoluteString: .response(404, [:], Data())])
            let updater = AppUpdater()
            await updater.check()
            precondition(updater.status == "No newer published release is available.")
            precondition(!updater.checking && updater.available == nil)
            precondition(updater.failureReleaseURL == nil)
            FixtureProtocol.assertRequests([UpdatePolicy.api])
        case "current", "newer":
            let fixture = try ReleaseFixture(version: scenario == "current" ? "0.1.0" : "0.2.0",
                                             build: scenario == "current" ? 13 : 14)
            FixtureProtocol.configure(fixture.replies)
            let update = try await check()
            if scenario == "current" { precondition(update == nil) }
            else {
                precondition(update?.manifest.version == "0.2.0")
                precondition(update?.manifest.build == 14)
                precondition(update?.asset.filename == fixture.asset.filename)
                precondition(update?.asset.sha256 == fixture.asset.sha256)
                precondition(update?.notes == "Fixture release notes.")
                precondition(update?.releaseURL.absoluteString == UpdatePolicy.releaseURL("v0.2.0"))
            }
            FixtureProtocol.assertRequests([UpdatePolicy.api, fixture.manifestURL])
            if scenario == "newer" {
                FixtureProtocol.configure(fixture.replies)
                let updater = AppUpdater()
                await updater.check()
                precondition(updater.status == "A new version of Igloo is available.")
                precondition(!updater.checking && updater.available?.manifest.version == "0.2.0")
                precondition(updater.failureReleaseURL == nil)
                FixtureProtocol.assertRequests([UpdatePolicy.api, fixture.manifestURL])
            }
        case "quota":
            await httpFailure(403, ["x-ratelimit-remaining": "0", "x-ratelimit-reset": "1793145600"], expected: quota)
        case "quota-no-reset":
            await httpFailure(403, ["x-ratelimit-remaining": "0", "x-ratelimit-reset": "invalid"], expected: quotaUnknown)
        case "forbidden":
            await httpFailure(403, [:], expected: forbiddenMessage)
        case "server":
            await httpFailure(503, [:], expected: serverMessage)
        case "offline":
            await transportFailure(.notConnectedToInternet, expected: offlineMessage)
        case "timeout":
            await transportFailure(.timedOut, expected: timeoutMessage)
        case "dns":
            await transportFailure(.cannotFindHost, expected: offlineMessage)
        case "connection-lost":
            await transportFailure(.networkConnectionLost, expected: offlineMessage)
        case "malformed":
            let fixture = try ReleaseFixture(malformed: true)
            FixtureProtocol.configure(fixture.replies)
            let error = await failure { _ = try await check() }
            precondition(error is UpdateError)
            precondition(error.localizedDescription == malformedMessage)
            FixtureProtocol.assertRequests([UpdatePolicy.api, fixture.manifestURL])
            FixtureProtocol.configure(fixture.replies)
            let updater = AppUpdater()
            await updater.check()
            precondition(updater.status == malformedMessage && updater.failed)
            precondition(updater.failureReleaseURL?.absoluteString == "https://github.com/tm-henningnt/Igloo/releases")
            FixtureProtocol.assertRequests([UpdatePolicy.api, fixture.manifestURL])
        case "cancelled-download", "digest-mismatch":
            let fixture = try ReleaseFixture()
            let downloadURL = URL(string: fixture.asset.url)!
            let body = Data(repeating: 120, count: fixture.payload.count)
            FixtureProtocol.configure([fixture.asset.url: scenario == "cancelled-download"
                ? .failure(.cancelled) : .response(200, [:], body)])
            let updater = AppUpdater()
            updater.available = AvailableUpdate(manifest: fixture.manifest, asset: fixture.asset,
                notes: "Fixture release notes.", releaseURL: URL(string: UpdatePolicy.releaseURL("v0.2.0"))!)
            updater.download()
            precondition(updater.downloading)
            precondition(updater.status == "Downloading and verifying Igloo…")
            let task = updater.downloadTask!
            await task.value
            if scenario == "cancelled-download" {
                precondition(updater.status == "Download canceled.")
            } else {
                precondition(updater.status == "The download failed its checksum check. Try again.")
            }
            precondition(!updater.downloading && updater.downloadTask == nil && updater.downloaded == nil)
            precondition(updater.failed)
            precondition(updater.failureReleaseURL?.absoluteString == "https://github.com/tm-henningnt/Igloo/releases")
            FixtureProtocol.assertRequests([downloadURL])
            let temporary = try FileManager.default.contentsOfDirectory(
                at: FileManager.default.temporaryDirectory, includingPropertiesForKeys: nil)
            precondition(!temporary.contains { $0.lastPathComponent.hasPrefix("Igloo-update-") })
        default:
            fatalError("Unknown fixture")
        }
        print("PASS " + scenario)
    }
}
'''


class UpdateTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="igloo-update-tests-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.executable = compile_update_check(cls.root, SWIFT_FIXTURES, include_updater=True,
                                              compiler_flags=("-parse-as-library",))

    def run_fixture(self, scenario):
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            root = Path(temporary)
            home, scratch = root / "home", root / "tmp"
            home.mkdir()
            scratch.mkdir()
            env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
            env.update(HOME=str(home), CFFIXED_USER_HOME=str(home), TMPDIR=str(scratch))
            result = subprocess.run([self.executable, scenario], env=env, capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "PASS " + scenario)
            self.assertEqual(list(scratch.glob("Igloo-update-*")), [], "Download staging must be removed on failure")
            self.assertFalse(any(path.is_file() for path in home.rglob("*.zip")))

    def test_demo_failure_states_match_user_messages(self):
        self.run_fixture("demo-states")

    def test_markdown_notes_format_blocks_and_only_link_https(self):
        self.run_fixture("markdown-notes")

    def test_no_release_404_returns_none(self):
        self.run_fixture("no-release")

    def test_current_version_returns_none(self):
        self.run_fixture("current")

    def test_newer_version_returns_verified_update(self):
        self.run_fixture("newer")

    def test_403_with_quota_headers_shows_reset_time(self):
        self.run_fixture("quota")

    def test_403_with_invalid_reset_time_gives_fallback(self):
        self.run_fixture("quota-no-reset")

    def test_server_failure_has_distinct_message(self):
        self.run_fixture("server")

    def test_403_without_quota_headers_has_distinct_message(self):
        self.run_fixture("forbidden")

    def test_offline_has_actionable_message(self):
        self.run_fixture("offline")

    def test_dns_failure_has_offline_message(self):
        self.run_fixture("dns")

    def test_lost_connection_has_offline_message(self):
        self.run_fixture("connection-lost")

    def test_timeout_has_distinct_message(self):
        self.run_fixture("timeout")

    def test_malformed_manifest_has_distinct_message(self):
        self.run_fixture("malformed")

    def test_cancelled_download_has_distinct_message_and_removes_staging(self):
        self.run_fixture("cancelled-download")

    def test_digest_mismatch_keeps_checksum_message_and_removes_staging(self):
        self.run_fixture("digest-mismatch")


if __name__ == "__main__":
    unittest.main()
