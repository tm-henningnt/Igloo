import SwiftUI
import AppKit

struct ReviewOutcome: Decodable {
    struct ObservedSpace: Decodable {
        var freedBytes: Int64?; var inTrashBytes: Int64?; var restoredBytes: Int64?
        init(freedBytes: Int64? = nil, inTrashBytes: Int64? = nil, restoredBytes: Int64? = nil) {
            self.freedBytes = freedBytes; self.inTrashBytes = inTrashBytes; self.restoredBytes = restoredBytes
        }
        private enum CodingKeys: String, CodingKey { case freedBytes, inTrashBytes, restoredBytes }
        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            freedBytes = try values.decodeIfPresent(Int64.self, forKey: .freedBytes)
            inTrashBytes = try values.decodeIfPresent(Int64.self, forKey: .inTrashBytes)
            restoredBytes = try values.decodeIfPresent(Int64.self, forKey: .restoredBytes)
        }
    }
    struct Checkouts: Decodable {
        var count: Int; var paths: [String]
        init(count: Int = 0, paths: [String] = []) { self.count = count; self.paths = paths }
        private enum CodingKeys: String, CodingKey { case count, paths }
        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            paths = try values.decodeIfPresent([String].self, forKey: .paths) ?? []
            count = try values.decodeIfPresent(Int.self, forKey: .count) ?? paths.count
        }
    }
    struct Archives: Decodable {
        var count: Int; var location: String; var paths: [String]?
        init(count: Int = 0, location: String = "", paths: [String]? = nil) {
            self.count = count; self.location = location; self.paths = paths
        }
        private enum CodingKeys: String, CodingKey { case count, location, paths }
        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            paths = try values.decodeIfPresent([String].self, forKey: .paths)
            count = try values.decodeIfPresent(Int.self, forKey: .count) ?? paths?.count ?? 0
            location = try values.decodeIfPresent(String.self, forKey: .location) ?? ""
        }
    }
    struct PushedRef: Decodable {
        var ref: String; var sha: String; var destination: String
        init(ref: String, sha: String, destination: String) { self.ref = ref; self.sha = sha; self.destination = destination }
        private enum CodingKeys: String, CodingKey { case ref, sha, destination }
        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            ref = try values.decodeIfPresent(String.self, forKey: .ref) ?? ""
            sha = try values.decodeIfPresent(String.self, forKey: .sha) ?? ""
            destination = try values.decodeIfPresent(String.self, forKey: .destination) ?? ""
        }
    }
    struct RestoredRef: Decodable, Identifiable {
        var path: String; var ref: String; var sha: String
        var id: String { path + ref }
    }
    struct HeldItem: Decodable {
        var path: String; var reason: String
        init(path: String, reason: String) { self.path = path; self.reason = reason }
        private enum CodingKeys: String, CodingKey { case path, reason }
        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            path = try values.decodeIfPresent(String.self, forKey: .path) ?? ""
            reason = try values.decodeIfPresent(String.self, forKey: .reason) ?? ""
        }
    }
    var checkoutsRemoved: Checkouts
    var bytesFreedEstimate: Int64
    var bytesUsedEstimate: Int64
    var bytesFreed: Int64?
    var archivesWritten: Archives
    var refsPushed: [PushedRef]
    var movedToTrash: Int
    var held: [HeldItem]
    var observed: Bool
    var observedSpace: ObservedSpace?
    var checkoutsRestored: Checkouts
    var archivesUsed: Archives
    var refsRestored: [RestoredRef]

    private enum CodingKeys: String, CodingKey {
        case checkoutsRemoved, bytesFreedEstimate, bytesUsedEstimate, bytesFreed, archivesWritten, refsPushed, movedToTrash, held, observed, observedSpace, checkoutsRestored, archivesUsed, refsRestored
    }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        checkoutsRemoved = try values.decodeIfPresent(Checkouts.self, forKey: .checkoutsRemoved) ?? Checkouts()
        bytesFreedEstimate = try values.decodeIfPresent(Int64.self, forKey: .bytesFreedEstimate) ?? 0
        bytesUsedEstimate = try values.decodeIfPresent(Int64.self, forKey: .bytesUsedEstimate) ?? 0
        bytesFreed = try values.decodeIfPresent(Int64.self, forKey: .bytesFreed)
        archivesWritten = try values.decodeIfPresent(Archives.self, forKey: .archivesWritten) ?? Archives()
        refsPushed = try values.decodeIfPresent([PushedRef].self, forKey: .refsPushed) ?? []
        movedToTrash = try values.decodeIfPresent(Int.self, forKey: .movedToTrash) ?? 0
        held = try values.decodeIfPresent([HeldItem].self, forKey: .held) ?? []
        observed = try values.decodeIfPresent(Bool.self, forKey: .observed) ?? false
        observedSpace = try values.decodeIfPresent(ObservedSpace.self, forKey: .observedSpace)
        checkoutsRestored = try values.decodeIfPresent(Checkouts.self, forKey: .checkoutsRestored) ?? Checkouts()
        archivesUsed = try values.decodeIfPresent(Archives.self, forKey: .archivesUsed) ?? Archives()
        refsRestored = try values.decodeIfPresent([RestoredRef].self, forKey: .refsRestored) ?? []
    }

    var actionLabel: String { actionLabel(preservedProjects: nil) }
    func actionLabel(preservedProjects: Int?) -> String {
        var actions: [String] = []
        if movedToTrash > 0 { actions.append("Move \(movedToTrash) " + (movedToTrash == 1 ? "folder" : "folders") + " to Trash") }
        if checkoutsRemoved.count > 0 {
            actions.append("Freeze \(checkoutsRemoved.count) " + (checkoutsRemoved.count == 1 ? "checkout" : "checkouts"))
        }
        let preserved = preservedProjects ?? (checkoutsRemoved.count == 0 ? archivesWritten.count : 0)
        if preserved > 0 { actions.append(preserved == 1 ? "Preserve project" : "Preserve \(preserved) projects") }
        return actions.isEmpty ? "Run reviewed actions" : actions.joined(separator: " and ")
    }
}

struct OutcomeSummaryRow: View {
    let label: String
    var body: some View {
        Text(label).textSelection(.enabled)
            .accessibilityElement(children: .combine)
            .accessibilityLabel(Text(label))
    }
}

enum IglooIllustrationKind: String {
    case freezeResult = "freeze-result"
    case thawResult = "thaw-result"
    case freezerEmpty = "freezer-empty"
}

enum IglooIllustrationMotion: Equatable {
    case none
    case frostIn
    case warmUp

    var duration: Double {
        switch self {
        case .none: return 0
        case .frostIn: return 0.32
        case .warmUp: return 0.36
        }
    }
}

struct IglooDomeMask: Shape {
    func path(in rect: CGRect) -> Path {
        func point(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
            CGPoint(x: rect.minX + rect.width * x / 160,
                    y: rect.minY + rect.height * y / 120)
        }
        var path = Path()
        path.move(to: point(17, 96))
        path.addCurve(to: point(79, 18), control1: point(17, 47), control2: point(43, 19))
        path.addCurve(to: point(142, 96), control1: point(117, 18), control2: point(140, 52))
        path.addQuadCurve(to: point(17, 96), control: point(85, 114))
        path.closeSubpath()
        return path
    }
}

struct IglooDoorGlowMask: Shape {
    func path(in rect: CGRect) -> Path {
        func point(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
            CGPoint(x: rect.minX + rect.width * x / 160,
                    y: rect.minY + rect.height * y / 120)
        }
        var path = Path()
        path.move(to: point(91, 103))
        path.addLine(to: point(91, 84))
        path.addQuadCurve(to: point(98, 74), control: point(91, 75))
        path.addLine(to: point(103, 74))
        path.addLine(to: point(107, 78))
        path.addLine(to: point(115, 78))
        path.addQuadCurve(to: point(120, 86), control: point(120, 78))
        path.addLine(to: point(120, 103))
        path.closeSubpath()
        return path
    }
}

struct IglooIllustration: View {
    let kind: IglooIllustrationKind
    let size: CGSize
    var motion: IglooIllustrationMotion = .none
    var operationID = "static"
    var shouldAnimate = false
    var showFinalEffect = false

    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.displayScale) private var displayScale
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @StateObject private var animation = IllustrationAnimationState()
    private var playedOperationID: String? {
        get { animation.playedOperationID }
        nonmutating set { animation.playedOperationID = newValue }
    }
    private var effectProgress: Double {
        get { animation.effectProgress }
        nonmutating set { animation.effectProgress = newValue }
    }

    private var resourceName: String {
        kind.rawValue + (colorScheme == .dark ? "-night" : "") + (displayScale >= 1.5 ? "-2x" : "")
    }

    private var illustration: NSImage? {
        guard let url = Bundle.main.url(forResource: resourceName, withExtension: "png", subdirectory: "brand"),
              let image = NSImage(contentsOf: url) else { return nil }
        image.size = NSSize(width: size.width, height: size.height)
        return image
    }

    var body: some View {
        ZStack {
            if let illustration {
                Image(nsImage: illustration)
                    .resizable()
                    .interpolation(.high)
                    .frame(width: size.width, height: size.height)
            }
            if showFinalEffect && motion == .frostIn {
                LinearGradient(colors: [.white.opacity(0.22), TemperatureAccent.cold.foreground(for: colorScheme).opacity(0.30),
                                        TemperatureAccent.cold.foreground(for: colorScheme).opacity(0.18)],
                               startPoint: .topLeading, endPoint: .bottomTrailing)
                    .opacity(effectProgress)
                    .mask(IglooDomeMask())
                    .blendMode(.softLight)
            } else if showFinalEffect && motion == .warmUp {
                RadialGradient(colors: [TemperatureAccent.warm.foreground(for: colorScheme).opacity(0.5),
                                        TemperatureAccent.warm.foreground(for: colorScheme).opacity(0.18), .clear],
                               center: UnitPoint(x: 105.0 / 160.0, y: 88.0 / 120.0), startRadius: 1,
                               endRadius: size.width * 0.18)
                    .opacity(effectProgress)
                    .mask(IglooDoorGlowMask())
                    .blendMode(.screen)
            }
        }
        .frame(width: size.width, height: size.height)
        .accessibilityHidden(true)
        .onAppear(perform: startEffect)
        .onChange(of: operationID) { startEffect() }
        .onChange(of: shouldAnimate) { if shouldAnimate { startEffect() } else if showFinalEffect { showFinalFrame() } }
        .onChange(of: reduceMotion) { if reduceMotion { showFinalFrame() } }
    }

    private func startEffect() {
        guard showFinalEffect, motion != .none else { return }
        guard shouldAnimate else { showFinalFrame(); return }
        guard playedOperationID != operationID else { return }
        playedOperationID = operationID
        guard !reduceMotion else { showFinalFrame(); return }
        var transaction = Transaction()
        transaction.disablesAnimations = true
        withTransaction(transaction) { effectProgress = 0 }
        DispatchQueue.main.async {
            withAnimation(.easeOut(duration: motion.duration)) { effectProgress = 1 }
        }
    }

    private func showFinalFrame() {
        var transaction = Transaction()
        transaction.disablesAnimations = true
        withTransaction(transaction) { effectProgress = 1 }
    }
}

struct LifecycleResultHeader: View {
    let title: String?
    let message: String?
    let accent: TemperatureAccent
    let kind: IglooIllustrationKind
    let imageSize: CGSize
    let motion: IglooIllustrationMotion
    let operationID: String
    let success: Bool
    var animateSuccess = true

    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @StateObject private var animation = ResultSymbolAnimationState()
    private var playedOperationID: String? {
        get { animation.playedOperationID }
        nonmutating set { animation.playedOperationID = newValue }
    }
    private var successTrigger: Int {
        get { animation.successTrigger }
        nonmutating set { animation.successTrigger = newValue }
    }

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    if success {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.system(size: 16))
                            .frame(width: 16, height: 16)
                            .foregroundStyle(accent.foreground(for: colorScheme))
                            .symbolEffect(.bounce, options: .nonRepeating, value: successTrigger)
                            .symbolEffectsRemoved(reduceMotion || !animateSuccess)
                            .accessibilityHidden(true)
                    }
                    if let title {
                        Text(title).font(.headline).foregroundStyle(accent.foreground(for: colorScheme))
                    }
                }
                if let message { Text(message).font(.callout) }
            }
            Spacer(minLength: 4)
            IglooIllustration(kind: kind, size: imageSize, motion: motion,
                              operationID: operationID, shouldAnimate: animateSuccess,
                              showFinalEffect: success)
        }
        .frame(maxWidth: .infinity, minHeight: imageSize.height, alignment: .leading)
        .onAppear(perform: startSuccessEffect)
        .onChange(of: operationID) { startSuccessEffect() }
        .onChange(of: success) { if $0 { startSuccessEffect() } }
    }

    private func startSuccessEffect() {
        guard success, playedOperationID != operationID else { return }
        playedOperationID = operationID
        guard animateSuccess, !reduceMotion else { return }
        DispatchQueue.main.async { successTrigger += 1 }
    }
}

struct OutcomeSummaryView: View {
    let outcome: ReviewOutcome
    let includeThaw: Bool
    let showSpace: Bool
    let showEstimate: Bool
    let showFreedSpace: Bool
    let showInTrash: Bool
    let inTrashQualifier: String?
    let showRestoredSpace: Bool
    let showNetSpaceChange: Bool
    let netSpaceLabel: String
    let heading: String?
    let spaceExplanation: String?
    let estimateLabel: String?

    init(outcome: ReviewOutcome, includeThaw: Bool = false, showSpace: Bool = true,
         showEstimate: Bool = true, showFreedSpace: Bool = true, showInTrash: Bool = true,
         inTrashQualifier: String? = nil, showRestoredSpace: Bool = true,
         showNetSpaceChange: Bool = true, netSpaceLabel: String = "Net space change after recovery",
         heading: String? = nil, spaceExplanation: String? = nil, estimateLabel: String? = nil) {
        self.outcome = outcome
        self.includeThaw = includeThaw
        self.showSpace = showSpace
        self.showEstimate = showEstimate
        self.showFreedSpace = showFreedSpace
        self.showInTrash = showInTrash
        self.inTrashQualifier = inTrashQualifier
        self.showRestoredSpace = showRestoredSpace
        self.showNetSpaceChange = showNetSpaceChange
        self.netSpaceLabel = netSpaceLabel
        self.heading = heading
        self.spaceExplanation = spaceExplanation
        self.estimateLabel = estimateLabel
    }

    private var countQualifier: String { outcome.observed ? "observed" : "estimate" }
    private var estimateIsAlreadyObserved: Bool {
        guard outcome.observed, let space = outcome.observedSpace, let freed = space.freedBytes else { return false }
        return bytes(freed) == bytes(outcome.bytesFreedEstimate)
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(heading ?? (outcome.observed ? "Outcome" : "Expected outcome"))
                .font(.headline).accessibilityAddTraits(.isHeader)
            if includeThaw {
                OutcomeSummaryRow(label: "Checkouts \(outcome.observed ? "restored" : "to restore"): \(outcome.checkoutsRestored.count)")
                ForEach(outcome.checkoutsRestored.paths, id: \.self) {
                    OutcomeSummaryRow(label: "Checkout restored: " + $0)
                }
                if outcome.observed {
                    OutcomeSummaryRow(label: "Remote refs restored: \(outcome.refsRestored.count)")
                    ForEach(outcome.refsRestored) { ref in
                        let hash = shortGitHash(ref.sha)
                        OutcomeSummaryRow(label: "Ref restored: " + ref.ref + (hash.isEmpty ? "" : " · " + hash))
                    }
                }
                if outcome.archivesUsed.count > 0 {
                    let paths = outcome.archivesUsed.paths ?? []
                    let firstPath = paths.first ?? outcome.archivesUsed.location
                    if !firstPath.isEmpty {
                        let more = paths.count > 1 ? " and \(paths.count - 1) more" : ""
                        OutcomeSummaryRow(label: "Freezer: \(firstPath)\(more)")
                    }
                }
            } else {
                if outcome.checkoutsRemoved.count > 0 {
                    OutcomeSummaryRow(label: "Checkouts removed: \(outcome.checkoutsRemoved.count) (\(countQualifier))")
                }
                ForEach(outcome.checkoutsRemoved.paths, id: \.self) {
                    OutcomeSummaryRow(label: "Checkout removed: " + $0)
                }
            }
            if showSpace {
                if showEstimate && !estimateIsAlreadyObserved {
                    OutcomeSummaryRow(label: estimateLabel ?? "Estimate: about \(bytes(outcome.bytesFreedEstimate))")
                }
                if let space = outcome.observedSpace {
                    if showFreedSpace, let value = space.freedBytes {
                        OutcomeSummaryRow(label: "Freed from disk: \(bytes(value))")
                    }
                    if showInTrash, let value = space.inTrashBytes {
                        let qualifier = inTrashQualifier.map { " (\($0))" } ?? ""
                        OutcomeSummaryRow(label: "In Trash\(qualifier): \(bytes(value))")
                    }
                    if let spaceExplanation {
                        Text(spaceExplanation).font(.caption).foregroundStyle(.secondary)
                    }
                    if showRestoredSpace, let value = space.restoredBytes {
                        OutcomeSummaryRow(label: "Restored from Trash: \(bytes(value))")
                    }
                    if showNetSpaceChange, outcome.observed, let freed = outcome.bytesFreed {
                        let net = freed < 0
                            ? "−\(bytes(freed == Int64.min ? Int64.max : -freed))"
                            : bytes(freed)
                        OutcomeSummaryRow(label: "\(netSpaceLabel): \(net) (observed)")
                    }
                }
            }
            if !includeThaw, outcome.bytesUsedEstimate > 0 {
                OutcomeSummaryRow(label: "Additional space used: about \(bytes(outcome.bytesUsedEstimate)) (estimate)")
            }
            if !includeThaw, outcome.archivesWritten.count > 0 {
                OutcomeSummaryRow(label: "Archives written: \(outcome.archivesWritten.count) (\(countQualifier))")
                if !outcome.archivesWritten.location.isEmpty {
                    OutcomeSummaryRow(label: "Freezer: \(outcome.archivesWritten.location)")
                }
                ForEach(outcome.archivesWritten.paths ?? [], id: \.self) { OutcomeSummaryRow(label: $0) }
            }
            if !includeThaw, !outcome.refsPushed.isEmpty {
                OutcomeSummaryRow(label: "Refs pushed: \(outcome.refsPushed.count) (\(countQualifier))")
                ForEach(Array(outcome.refsPushed.enumerated()), id: \.offset) { _, ref in
                    let hash = shortGitHash(ref.sha)
                    OutcomeSummaryRow(label: "Ref pushed: " + ref.ref + (hash.isEmpty ? "" : " · " + hash) + " → " + ref.destination)
                }
            }
            if !includeThaw, outcome.movedToTrash > 0 {
                OutcomeSummaryRow(label: "Folders moved to Trash: \(outcome.movedToTrash) (\(countQualifier))")
            }
            if !outcome.held.isEmpty {
                OutcomeSummaryRow(label: "Not run: \(outcome.held.count) (\(countQualifier))")
            }
            ForEach(Array(outcome.held.enumerated()), id: \.offset) { _, item in
                OutcomeSummaryRow(label: item.path + " · " + item.reason)
            }
        }.font(.caption).frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct SpaceRun: Decodable, Identifiable {
    var id: String; var kind: String; var label: String; var started: String; var status: String
    var estimateBytes: Int64; var observed: ReviewOutcome.ObservedSpace?
    private enum CodingKeys: String, CodingKey { case id, kind, label, started, status, estimateBytes, observed }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decodeIfPresent(String.self, forKey: .id) ?? ""
        kind = try values.decodeIfPresent(String.self, forKey: .kind) ?? "run"
        label = try values.decodeIfPresent(String.self, forKey: .label) ?? kind.capitalized
        started = try values.decodeIfPresent(String.self, forKey: .started) ?? ""
        status = try values.decodeIfPresent(String.self, forKey: .status) ?? "unknown"
        estimateBytes = try values.decodeIfPresent(Int64.self, forKey: .estimateBytes) ?? 0
        observed = try values.decodeIfPresent(ReviewOutcome.ObservedSpace.self, forKey: .observed)
    }
}
struct SpaceTotals: Decodable {
    var estimateBytes: Int64; var observed: ReviewOutcome.ObservedSpace
    var measuredRuns: Int; var unmeasuredRuns: Int
}
struct SpaceReportTotals: Decodable { var last30Days: SpaceTotals; var allTime: SpaceTotals }
struct SpaceReport: Decodable { var runs: [SpaceRun]; var totals: SpaceReportTotals }

func measuredSpace(_ value: Int64?) -> String {
    guard let value else { return "not measured" }
    return bytes(value)
}

func shortGitHash(_ value: String) -> String {
    let hash = value.trimmingCharacters(in: .whitespacesAndNewlines)
    guard hash.count >= 7, hash.allSatisfy(\.isHexDigit) else { return "" }
    return String(hash.prefix(7))
}

struct CleanupResultView: View {
    let result: CleanupResult
    let onDone: () -> Void
    private var detailHeight: CGFloat { min(220, max(100, 72 + CGFloat(result.results.count) * 26)) }
    private var resultHeight: CGFloat { min(440, max(320, detailHeight + 190)) }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Cleanup result").font(.title2).fontWeight(.semibold)
            Text(result.message).font(.callout)
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    OutcomeSummaryView(outcome: result.outcome, showEstimate: false,
                                       inTrashQualifier: "can be restored", showRestoredSpace: false,
                                       showNetSpaceChange: false)
                    ForEach(result.results, id: \.path) { item in
                        let size = item.size.map { " · " + bytes($0) } ?? ""
                        OutcomeSummaryRow(label: item.ok ? "Moved to Trash: " + item.path + size : "Not moved: " + item.path + " · " + item.message)
                            .font(.caption)
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }.frame(height: detailHeight)
            HStack { Spacer(); Button("Done", action: onDone).keyboardShortcut(.cancelAction) }
        }.padding(24).frame(width: sheetWidth(760), height: resultHeight)
    }
}

struct CleanupReview: View {
    @EnvironmentObject var store: OrganizerStore
    let plan: CleanupPlan
    @LocalState var reviewed = false
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if plan.held == true { Text("This project is on Hold").foregroundStyle(.orange) }
            Text("Review cleanup · " + URL(fileURLWithPath: plan.items.first?.owner ?? plan.items.first?.path ?? "Project").lastPathComponent)
                .font(.title2).fontWeight(.semibold)
            // Moving to Trash frees no disk space yet, so the estimate is the review total that moves to Trash.
            if let outcome = plan.outcome {
                ScrollView {
                    OutcomeSummaryView(outcome: outcome,
                                       estimateLabel: "Estimate: about \(bytes(plan.totalSize)) to move to Trash")
                }.frame(maxHeight: 96)
            }
            Text(noun(plan.items.count, "directory", "directories") + " · \(bytes(plan.totalSize)) to move to Trash")
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(plan.items) { item in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(item.path).font(.callout).textSelection(.enabled)
                            Text(bytes(item.size)).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    if !plan.blocked.isEmpty {
                        Divider()
                        Text("Not run").font(.headline)
                        ForEach(plan.blocked) { item in
                            VStack(alignment: .leading, spacing: 3) {
                                Text(item.path).font(.callout)
                                Text(item.reason).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }.frame(minHeight: 110, maxHeight: 300)
            Text("Files remain in Trash until you empty it. Restore through History. Dependencies may contain custom edits; only continue after reviewing the listed folders. This preview expires after ten minutes.")
                .font(.callout).foregroundStyle(.secondary)
            Toggle("I have reviewed these folders for local edits.", isOn: $reviewed)
            HStack {
                Button("Cancel", role: .cancel) { store.plan = nil }.keyboardShortcut(.cancelAction)
                Spacer()
                Button(plan.outcome?.actionLabel ?? "Move \(plan.items.count) \(plan.items.count == 1 ? "folder" : "folders") to Trash") {
                    let id = plan.id
                    store.plan = nil
                    store.action(["trash", id])
                }.buttonStyle(WizardPrimaryButtonStyle()).disabled(plan.items.isEmpty || !reviewed || store.busy || store.isDemo)
                    .help(store.isDemo ? "Cleanup is unavailable in demo mode." : store.busy ? "Wait for the current operation to finish." : plan.items.isEmpty ? "There are no eligible folders in this review." : !reviewed ? "Review and confirm the listed folders first." : "Move the reviewed folders to Trash.")
            }
        }.padding(24).frame(width: 700)
    }
}

struct PushReview: View {
    @EnvironmentObject var store: OrganizerStore
    let request: PushRequest
    @LocalState var reviewed = false
    private var commit: String { shortGitHash(request.branch.sha) }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Push one branch").font(.title2).fontWeight(.semibold)
            Text(request.branch.name).font(.headline).textSelection(.enabled)
            VStack(alignment: .leading, spacing: 4) {
                Text("Destination: " + request.remote.pushURL)
                Text("Ref: refs/heads/" + request.branch.name)
                Text("Commit: " + (commit.isEmpty ? "Not available. Analyze Projects again." : commit))
                    .help(request.branch.sha)
            }.font(.callout).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            Text("A push makes this branch visible on the remote. Igloo cannot undo it, and the local checkout stays unchanged. Review the committed history for private data first. Other branches, tags, stashes and uncommitted files are excluded. A rejected push stops; Igloo never force-pushes.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Toggle("I reviewed this branch and remote destination.", isOn: $reviewed)
            Text("Select this checkbox to enable Push this branch.").font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Cancel", role: .cancel) { store.pushRequest = nil }
                Spacer()
                Button("Push this branch") {
                    let r = request
                    store.pushRequest = nil
                    store.action(["push", r.project.path, r.branch.name, r.branch.sha, r.remote.name, r.remote.pushURL])
                }.buttonStyle(WizardPrimaryButtonStyle()).disabled(!reviewed || store.busy || commit.isEmpty)
                    .help(store.busy ? "Wait for the current operation to finish." : commit.isEmpty ? "Analyze Projects again to verify this branch commit." : !reviewed ? "Review and confirm the branch and destination first." : "Push this reviewed branch.")
            }
        }.padding(24).frame(width: 660)
    }
}

struct HistoryView: View {
    @EnvironmentObject var store: OrganizerStore
    @LocalState var selected: String?
    @LocalState var restoreConfirm = false
    @LocalState var report: SpaceReport?
    var item: TrashItem? { store.snapshot?.history.first { $0.id == selected } }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Trash & restore history").font(.title2).fontWeight(.semibold)
            if let history = store.snapshot?.history, !history.isEmpty {
                Table(history.reversed().map { $0 }, selection: $selected) {
                    TableColumn("Original directory") { Text($0.original).lineLimit(1).truncationMode(.middle).help($0.original) }.width(min: 320)
                    TableColumn("Size") { Text(bytes($0.size)) }.width(90)
                    TableColumn("State", value: \.status).width(85)
                    TableColumn("Date") { Text(shortDate($0.at)) }.width(90)
                }.alternatingRowBackgrounds(.disabled)
                    .frame(height: compactTableHeight(history.count, rowHeight: 32, maxRows: 4, maxHeight: 170))
            } else {
                ContentUnavailableView("No Trash history", systemImage: "trash", description: Text("Moved folders appear here until they are restored."))
                    .frame(height: 58)
            }
            Text("Restore requires the original path to be empty and the Trash item to remain available. The app never overwrites a replacement directory or permanently empties Trash.")
                .font(.caption).foregroundStyle(.secondary)
            Divider()
            HStack {
                Text("Space report").font(.headline).accessibilityAddTraits(.isHeader)
                Spacer()
                Button("Refresh report") { loadSpaceReport() }.disabled(store.busy)
                    .help(store.busy ? "Wait for the current operation to finish." : "Refresh the space report.")
            }
            if let report {
                HStack(alignment: .top, spacing: 18) {
                    totals("Last 30 days", report.totals.last30Days)
                    totals("All time", report.totals.allTime)
                }
                Table(report.runs) {
                    TableColumn("Run") { Text($0.label).help($0.kind + " · " + $0.status) }.width(min: 110)
                    TableColumn("Date") { Text(String($0.started.prefix(10))) }.width(90)
                    TableColumn("Estimate") { Text(bytes($0.estimateBytes)) }.width(90)
                    TableColumn("Freed from disk") { Text(measuredSpace($0.observed?.freedBytes)) }.width(120)
                    TableColumn("In Trash") { Text(measuredSpace($0.observed?.inTrashBytes)) }.width(90)
                    TableColumn("Restored from Trash") { Text(measuredSpace($0.observed?.restoredBytes)) }.width(140)
                }.alternatingRowBackgrounds(.disabled)
                    .frame(minHeight: 100, idealHeight: 150, maxHeight: 180)
            } else {
                Text(store.busy ? "Loading space report…" : "Space report is unavailable.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Button("Done") { store.historyShown = false }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Show in Trash") { if let i = item { store.reveal(i.destination) } }.disabled(item == nil || store.isDemo)
                    .help(store.isDemo ? "Finder is unavailable in demo mode." : item == nil ? "Select a Trash item first." : "Show the selected Trash item in Finder.")
                Button("Restore selected…") { restoreConfirm = true }
                    .disabled(store.busy || store.isDemo || item == nil || item?.status == "restored")
                    .help(store.isDemo ? "Restore is unavailable in demo mode." : store.busy ? "Wait for the current operation to finish." : item == nil ? "Select a Trash item first." : item?.status == "restored" ? "This item has already been restored." : "Restore the selected directory.")
            }
        }.padding(24).frame(width: sheetWidth(980), height: 650)
        .onAppear { loadSpaceReport() }
        .confirmationDialog("Restore this directory to its original location?", isPresented: $restoreConfirm, titleVisibility: .visible) {
            Button("Restore") {
                if let i = item {
                    store.invoke(["restore", i.id], as: Reply.self) { _ in
                        store.load()
                        loadSpaceReport()
                    }
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: { Text(item?.original ?? "") }
    }
    func loadSpaceReport() {
        store.invoke(["space-report"], as: SpaceReport.self) { report = $0 }
    }
    func totals(_ title: String, _ value: SpaceTotals) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.subheadline).fontWeight(.semibold)
            Text("Estimate \(bytes(value.estimateBytes)) · Freed from disk \(bytes(value.observed.freedBytes ?? 0)) · In Trash \(bytes(value.observed.inTrashBytes ?? 0)) · Restored from Trash \(bytes(value.observed.restoredBytes ?? 0))")
            Text("\(value.measuredRuns) measured · \(value.unmeasuredRuns) not measured").foregroundStyle(.secondary)
        }.font(.caption).frame(maxWidth: .infinity, alignment: .leading)
    }
}


// The Command Line Tools toolchain has no SwiftUIMacros plugin, so `@State` does not compile. These boxes hold the
// animation state with `@StateObject`, as the rest of the app does.
final class IllustrationAnimationState: ObservableObject {
    @Published var playedOperationID: String?
    @Published var effectProgress = 0.0
}

final class ResultSymbolAnimationState: ObservableObject {
    @Published var playedOperationID: String?
    @Published var successTrigger = 0
}
