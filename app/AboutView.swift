import SwiftUI
import AppKit

struct OrganizerAboutCommands: Commands {
    let updater: AppUpdater
    @Environment(\.openWindow) private var openWindow
    var body: some Commands {
        CommandGroup(replacing: .appInfo) {
            Button("About Igloo") { openWindow(id: "about") }
            Button("Check for Updates…") {
                openWindow(id: "updates")
                Task { await updater.check() }
            }
        }
    }
}

struct AboutView: View {
    @EnvironmentObject var store: OrganizerStore
    @EnvironmentObject var updater: AppUpdater
    @Environment(\.openWindow) private var openWindow
    @Environment(\.dismiss) private var dismiss
    private var version: String {
        let info = Bundle.main.infoDictionary ?? [:]
        return "Version " + (info["CFBundleShortVersionString"] as? String ?? "") +
            " (" + (info["CFBundleVersion"] as? String ?? "") + ")"
    }
    private var icon: NSImage {
        Bundle.main.url(forResource: "AppIcon", withExtension: "icns")
            .flatMap { NSImage(contentsOf: $0) } ?? NSImage(named: NSImage.applicationIconName)!
    }
    private var runtimeDescription: String {
        Bundle.main.infoDictionary?["IglooRuntime"] as? String == "system" ? "Uses the Python installed on this Mac" : "Python included"
    }
    var body: some View {
        VStack(spacing: 0) {
            Image(nsImage: icon).resizable().interpolation(.high)
                .frame(width: 128, height: 128).accessibilityHidden(true)
                .padding(.bottom, 16)
            Text("Igloo").font(.system(size: 28, weight: .semibold))
            Text("Make space. Keep your work.").font(.headline).padding(.top, 8)
            Text("Review disk use, clean up safely, and freeze your projects until you need them again.")
                .font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true).padding(.top, 12)
            Text("By Henning Naarlien-Tolpinrud")
                .font(.callout).foregroundStyle(.secondary).padding(.top, 16)
                .textSelection(.enabled)
            HStack(spacing: 10) {
                Button("How to use") {
                    openWindow(id: "catalog")
                    store.helpShown = true
                    dismiss()
                }
                Button("Check for Updates…") {
                    openWindow(id: "updates")
                    Task { await updater.check() }
                }
            }.buttonStyle(.bordered).padding(.top, 22)
            HStack(spacing: 20) {
                Link("Igloo website", destination: UpdatePolicy.website)
                Link("Source on GitHub", destination: URL(string: "https://github.com/" + UpdatePolicy.repository)!)
            }.font(.callout).padding(.top, 14)
            Divider().padding(.top, 24).padding(.bottom, 16)
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text(version).textSelection(.enabled)
                    Text(runtimeDescription)
                }.font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Close") { dismiss() }.keyboardShortcut(.cancelAction)
            }
        }.padding(32).frame(width: 460)
    }
}
