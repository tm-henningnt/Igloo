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

    python3 -m unittest -q tests.test_organizer.GuardTests tests.test_lifecycle.LifecycleTests tests.test_preferences.PreferencesTests tests.test_space.SpaceTests tests.test_planning.PlanningTests tests.test_classification.ClassificationTests tests.test_tasks.TaskTests tests.test_releases tests.test_site

Tests use disposable repositories and archives. Launch Igloo.app with --demo for a fictional read-only catalog. Add --demo-light or --demo-dark for matching documentation screenshots without changing the Mac’s appearance. Normal app launches follow the system appearance.

## Contributing

Report bugs and suggest changes through [issues](https://github.com/tm-henningnt/Igloo/issues), or send a pull request. See [Contributing](../CONTRIBUTING.md) for what to include.

## Artwork

The [installer background](../assets/InstallerBackground.png) and [README header](../assets/README-header.png) were created with the built-in image-generation tool. Their prompt files live beside the artwork. The app icon is in assets/IglooIcon.png.
