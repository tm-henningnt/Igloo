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

## Igloo Dev and the installed release

A local build is Igloo Dev, with the bundle ID `local.igloo.dev`. The release build is Igloo, with the bundle ID `local.igloo`. Igloo Dev does not change the settings, the CLI or the task LaunchAgents of the installed Igloo.

| Item | Igloo | Igloo Dev |
| --- | --- | --- |
| Settings folder (`config.json`, `wizard.json`, `tasks.json`) | `~/.config/igloo` | `~/.config/igloo.dev` |
| Task CLI | `~/.local/bin/igloo` | `~/.local/bin/igloo.dev` |
| Task LaunchAgent labels | `local.igloo.task.<id>` | `local.igloo.dev.task.<id>` |
| Schedule list and task logs in the state folder | `task-schedules.json`, `task-logs/` | `task-schedules.dev.json`, `task-logs.dev/` |

The app gives its identity to the backend in the `IGLOO_APP_ID` environment variable. The Dev CLI sets the same variable. Without the variable, the backend and the CLI use the release folder. A binary without a bundle ID uses the Dev folder.

On its first launch, Igloo Dev copies `config.json` and `wizard.json` from the release folder to the Dev folder. The copy does not include `pythonExecutable`. Igloo Dev does not copy saved tasks. When the Dev folder exists, Igloo Dev does not copy again. To start again from the release settings, remove the Dev settings folder. Settings in Igloo Dev show the Dev folder.

A Settings save does not write the bundled Python path. The bundled Python is a runtime choice of each app copy. A Python path that you type in Settings is saved as you typed it. When an app reads a `pythonExecutable` inside another app bundle, it uses its default Python. The file stays as it is until you save Settings.

The state folder (`~/.igloo`) and the Freezer (`~/.igloo/vault`) are shared by default. Igloo Dev therefore sees the catalog, the history and the Frozen projects of the installed Igloo. A Dev test that freezes or thaws uses disposable projects only. To keep Dev state apart, set other storage paths in Igloo Dev Settings.

## Contributing

Report bugs and suggest changes through [issues](https://github.com/tm-henningnt/Igloo/issues), or send a pull request. See [Contributing](../CONTRIBUTING.md) for what to include.

## Artwork

The [installer background](../assets/InstallerBackground.png) and [README header](../assets/README-header.png) were created with the built-in image-generation tool. Their prompt files live beside the artwork. The app icon is in assets/IglooIcon.png.

Browser icons use the same master artwork. Regenerate them with python3 scripts/build_favicons.py after changing assets/IglooIcon.png.

## Native window capture

Build the app first, then use the capture launcher on macOS. It starts a separate demo instance for each scenario and never builds the app. The default `orca` backend targets the launched process by PID, confirms that PID runs the executable inside the supplied app bundle, and uses Orca computer use for real-pixel screenshots and clicks. It polls for interactive states without screenshots every 0.5 seconds and requests a screenshot after the state is ready. The Orca demo has a 900-second deadline; window-server captures keep their 60-second deadline. The launcher sends `SIGTERM` only after checking the PID's executable path again. That check and the signal are separate OS calls, leaving a narrow PID reuse window between verification and termination. Choose a new output directory in an existing parent; output symlinks and existing directories are refused.

    python3 scripts/capture_states.py --list
    python3 scripts/capture_states.py --list --since HEAD~1
    mkdir -p -m 700 captures
    python3 scripts/capture_states.py --app ./Igloo.app --output captures/review --since HEAD~1

`--since <commit>` verifies that the argument resolves to a commit, compares it with the current worktree under `app/`, and selects the scenarios mapped to changed Swift view files. Shared view and model declarations map to every scenario that can use them. The mapping is in `scripts/capture_states.py`; an unrecognized Swift source selects the full matrix. `--scenario <name>` can be repeated and adds those scenarios to the `--since` selection. Without either option, the full matrix runs. Use `--list --since <commit>` to inspect the selected scenario names before capture.

Each scenario folder keeps renderer diagnostics in `render/window-render.png` and, for sheets, `render/sheet-render.png`. At the scenario root, `accessibility.json`, `windows.json`, and `started.json` record the launch view, window metadata, and app PID. The real-pixel image files also stay at the root: Orca writes `window-orca.png` for a static screen and named `*-orca.png` files for each interactive step; `window-server` writes `window-ws.png` and, when present, `sheet-ws.png`. Renderer PNGs are diagnostics, not visual evidence. A renderer can omit native chrome or show placeholders for AppKit-hosted views even when the real-pixel screenshot is valid. All successful captures report `PARTIAL` until someone reviews the pixels.

The matrix includes both appearances, the minimum and default sizes, compact and full rails, shown and hidden scope, all detail tabs, freeze and shelf sheets, and long-name and empty states. The minimum-size scenarios use hidden scope with the compact rail at 850 points, hidden scope with the full rail at 1100 points, and shown scope with the full rail at 1300 points. The default scenario is 1440 points wide. It includes the interactive Thaw flow in both appearances, from the wizard start screen through Frozen-project selection, plan review, confirmation, and results.

The measured macOS window-content captures are:

- An `850x700` request captures at 850 × 793 points with hidden scope and the compact rail.
- An `1100x700` request captures at 1100 × 793 points with hidden scope and the full rail.
- A `1300x700` request captures at 1300 × 793 points with shown scope and the full rail.
- A `1440x900` request captures at 1440 × 900 points.

The 700-point height clamps to 793 points (741 layout points plus 52 for the title and toolbar). These are window-content point dimensions, distinct from PNG pixels and backing scale. The launcher checks these measured minimums and reports a different layout as open for review.

To use macOS `screencapture` instead of Orca, wake and unlock the display, grant Screen Recording access to the calling terminal, and choose `--backend window-server`. `--window-server` remains a compatibility alias. That backend waits for on-screen main-window and attached-sheet metadata, captures each visible window, then signals the demo instance to exit. If Orca cannot read the app, the screen is locked, a screenshot is missing, or a step cannot be verified, that scenario is `OPEN`; the launcher does not call it a pass. See [the native capture review checklist](capture-review.md) for visual review. The checklist path is printed at the end of each run.

For a single custom app capture, use a listed scenario. The size, view, scope, rail, data, and capture arguments have no effect outside `--demo`. The capture path must be absolute and name a new folder in an existing parent directory. The app creates the scenario and renderer folders with private permissions and writes private PNG and JSON files. A valid PNG, accessibility tree, or successful click does not replace visual review; record every unobserved state as open.

To inspect the live Orca sheet tree without clicking through the flow, pass one explicit scenario and `--probe`. It launches the built app, prints the current sheet subtree, and stops the verified demo process:

```sh
python3 scripts/capture_states.py --app ./Igloo.app --output "$TMPDIR/igloo-orca-probe" \
  --scenario light-wizard-thaw-flow-1440x900-shown-full-normal --probe
```

Interactive Orca clicks use a fresh tree snapshot and a locator relative to the `sheet` node. A locator names a role, a 1-based ordinal among matching descendants, and anchor text that must appear in that sheet. An optional `label` further matches text on the target node, such as the `Harbor CLI` outline row. A nested `parent` locator narrows matching descendants, for example the first `checkbox` inside the first `outline row`. The launcher lists app windows, finds the window containing the anchored sheet, and uses that same window ID for both the snapshot and click. If Orca returns a `nextSteps` instruction to restore the window, it retries once with `--restore-window`; an unresolved failure is reported with Orca's error code and message. Accessibility identifiers remain in the app for XCUITest and VoiceOver, but this Orca route does not depend on them.

Do not click rows inside a sheet. Orca clicks a row at its coordinates, and it refuses coordinate clicks inside a sheet with `window_not_focused`. Sheet buttons and checkboxes accept clicks. A step can press Up or Down to change the selected row. The launcher then waits until the labeled outline row is marked selected in a fresh sheet snapshot. The key step works only when the list has keyboard focus. After "Create Thaw plan" the plan list does not have focus, so the Thaw flow does not change rows.
