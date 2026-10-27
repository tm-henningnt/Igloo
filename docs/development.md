# Developing Igloo

The app uses SwiftUI/AppKit with a Python standard-library backend. Build on macOS with Xcode Command Line Tools and Python 3.12 or later. Git and gh are needed for repository features.

## Source layout

- app/: SwiftUI and AppKit interface.
- backend/: Python analysis, cleanup, recovery, and CLI modules.
- tests/: backend, native model, and release checks.
- scripts/: build and packaging helpers.
- assets/, docs/, and site/: artwork, user documentation, and the website.

The build entry point, license, and release metadata stay at the root. The installed app bundles the backend independently of this layout.

## Build and test

    ./build.command
    ./build.command universal
    ./build.command universal system

The default includes a private Python runtime. The system variant leaves it out. Runtime downloads are pinned by version and SHA-256 in scripts/python-runtime.json; upstream license notices remain in the bundle. Building a universal app includes Apple Silicon and Intel runtimes.

Run verification from the repository root:

    python3 scripts/verify.py fast
    python3 scripts/verify.py release

The fast gate checks whitespace, the explicit test suite, test-module coverage, version metadata, public-file paths, private-path exclusions when the policy is present, and the verification commands in the docs. The release gate adds the default native build and strict deep signature verification, and reports the tested commit, working-tree state, and checks that still need separate acceptance: native window states, Intel execution, live updater behavior, and an installed-app update.

Enable the local pre-push gate in a development clone with:

    python3 scripts/verify.py install-hook

Commits must include an issue reference such as `#12` or start with `Record` or `Merge`; the fast gate checks commits since the upstream (or `origin/main`), or an explicitly supplied `--base`.

This sets the clone's `core.hooksPath` to `.githooks`. The hook directory is excluded from public exports; public contributors can run the verification commands directly.

Tests run with a disposable HOME and use disposable repositories and archives. Launch Igloo.app with --demo for a fictional read-only catalog. Add --demo-light or --demo-dark for matching documentation screenshots without changing the Mac’s appearance. Normal app launches follow the system appearance.

## Contributing

Report bugs and suggest changes through [issues](https://github.com/tm-henningnt/Igloo/issues), or send a pull request. See [Contributing](../CONTRIBUTING.md) for what to include.

## Artwork

The [installer background](../assets/InstallerBackground.png) and [README header](../assets/README-header.png) were created with the built-in image-generation tool. Their prompt files live beside the artwork. The app icon is in assets/IglooIcon.png.

Browser icons use the same master artwork. Regenerate them with python3 scripts/build_favicons.py after changing assets/IglooIcon.png.

## Native window capture

Build the app first, then use the capture launcher on macOS. It starts a separate demo instance for each scenario and waits for that instance to write its output and exit. It does not build the app or stop other instances. Choose a new output directory in an existing parent; output symlinks and existing directories are refused so old captures cannot masquerade as new evidence.

    python3 scripts/capture_states.py --list
    mkdir -p -m 700 captures
    python3 scripts/capture_states.py --app ./Igloo.app --output captures/review

To run a subset, add `--scenario <name>` for each name listed by `--list`. Each scenario folder contains `window.png`, `accessibility.json`, `windows.json`, and `started.json` with the capture process ID. `windows.json` records each main or attached sheet window's number, frame and on-screen state; an unavailable on-screen state is null. Freeze and shelf scenarios also contain `sheet.png`. The images render SwiftUI view roots offscreen, not a window-server screenshot: native window chrome and some AppKit-hosted controls may be absent. The JSON includes the demo view, requested and actual window-content dimensions, layout dimensions, backing scale, and the accessibility tree. The launcher reports requested, actual, and expected clamped dimensions; a mismatch is open rather than a pass. The launcher checks PNG pixel samples and reports black or near-uniform output as open when at most one percent of samples differ from the dominant color. It also reports an offscreen image as open when more than two percent of samples have the yellow or red AppKit placeholder colors. This check cannot detect every incomplete rendering. The accessibility JSON may omit controls; its completeness is reported as partial. A failed launch, missing or invalid output, timeout, or empty accessibility tree is reported as open; the launcher does not terminate an app that fails to exit. Inspect each PNG and tree for readable labels, control count, outer bounds, scroll ownership, and visible bottom edges. Record any unobserved scenario as open; a successful launcher is not a visual review.

The matrix includes both appearances, requested minimum and default sizes, compact and full rails, shown and hidden scope, all detail tabs, freeze and shelf sheets, and long-name and empty states. On the measured macOS layout, shown scope clamps width to 1300 points, hidden scope with the full rail to 1100 points, hidden scope with the compact rail to 850 points, and a requested 700-point height clamps to 793 points (741 layout points plus 52 for the title and toolbar). Thus `850x700` requires hidden scope to reach 850 points wide, but the actual window-content capture is 850 × 793; `1440x900` captured at 1440 × 900. These are point dimensions, distinct from PNG pixels and backing scale. The launcher checks those measured minimums and reports a different layout as open for review. For a single custom capture, launch an already built app with:

    capture_root="$(mktemp -d)"
    open -n -W ./Igloo.app --args --demo --demo-light --demo-size 850x700 --demo-view plan --demo-scope hidden --demo-rail compact --demo-data normal --demo-capture "$capture_root/single"

To also capture the real windows, wake and unlock the display, then add `--window-server` to the launcher command. The launcher waits for window metadata while each demo instance is alive and runs `screencapture` for the on-screen main window and any attached sheet. It writes `window-ws.png` and, for sheets, `sheet-ws.png`, plus `window-server.json` with results; it then signals the app to exit. Window numbers are supplied by the running app and the launcher does not independently verify their owning process, so use this mode only with a trusted local build. A window that is not on screen, or whose on-screen state cannot be determined, is OPEN as display asleep or locked. Capture errors, permission failures, blank PNGs and timeouts are OPEN. The launcher never stops a leftover app; it reports its recorded PID for the person who launched it. A valid sampled PNG is still PARTIAL until a person checks it visually. The offscreen window may remain OPEN because AppKit-backed content uses placeholders, even if a window-server capture was produced.

Use `--demo-dark` for dark appearance. Supported views include the project tabs, `hold`, `freeze`, `shelf`, `freeze-review`, `preserve-review`, `freeze-result`, `freeze-failure`, blocker-specific freeze views including `freeze-no-files`, `cleanup-review`, `cleanup-hold`, `wizard-review`, `task-review`, `settings`, `settings-cloud`, and the update failure views listed by `--list`; data choices are `normal`, `long`, and `empty`. Review, settings and shelf scenarios cover minimum and default sizes in both appearances. Overview also covers the rail, scope and long-name combinations. When a window image is unavailable, the launcher validates its sheet separately; sampled images still require visual review and AppKit placeholders remain open. The size, view, scope, rail, data, and capture arguments have no effect outside `--demo`. The capture path must be absolute and name a new folder in an existing parent directory. The app creates the folder with private permissions, writes private PNG and JSON files, and exits. Interactive inspection requires a running instance. On macOS, `iconutil` and `open` may fail in restricted environments; run the build and launch with the required host access. The `screencapture` window and rectangle modes have also failed in the documented environment. The release gate lists native capture as skipped until this separate capture and review are performed.
