# Igloo

![Igloo — Make space. Keep your work.](assets/README-header.png)

Projects pile up. Dependencies grow, experiments go quiet, and it gets harder to remember which folders contain work you haven't pushed.

Igloo is a native macOS app that helps you sort it out. See what's taking up space, clean up files you can rebuild, and freeze projects you want to come back to later.

![Igloo's project overview and disk usage sidebar](docs/screenshots/overview.png)

## What you can do

- Find large, inactive projects and explore disk use by age, project type, technology, or folder type.
- Review uncommitted changes, unpushed branches, worktrees, stashes, and local files before cleaning up.
- Remove old dependencies and caches, with a preview first and recovery through Trash.
- Preserve local files and Git recovery in the Freezer and push reviewed source while keeping the checkout. Freeze also removes verified checkouts; thaw the project when you need it again.
- Resolve requirements shown in the Freeze checklist, put projects on Hold, and review an outcome summary before accepting changes.
- Connect Git remotes, create private GitHub repositories, commit selected files, and browse or clone your repositories.
- Use the Project wizard to keep recent projects, clean older ones, propose freezing projects past a second age threshold, or Thaw Frozen projects.
- Save tasks, run them with the `igloo` CLI, and schedule daily or weekly reviews or cache cleanup.

You choose the rules and review the proposed changes. Scheduled cleanup needs your explicit permission; projects that need freezing stay held for review.

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/screenshots/space-explorer-light.png">
  <img src="docs/screenshots/space-explorer.png" alt="Space Explorer showing project sizes with warm-to-cold age colors">
</picture>

[Website and downloads](https://tm-henningnt.github.io/Igloo/) · [Published releases](https://github.com/tm-henningnt/Igloo/releases)

## Get started

You'll need macOS 14 or later, Git, and the GitHub CLI (`gh`) for GitHub features. The standard app includes its own private Python runtime.

Open the DMG and drag Igloo to Applications, open **Settings**, and choose your projects folder. Then click **Analyze Projects**. A scan gives you a starting point without changing your projects.

This release is not notarized or signed with Apple Developer ID. If macOS blocks the first launch, try opening Igloo, then go to **System Settings → Privacy & Security → Open Anyway** to approve this app. See [Apple’s instructions](https://support.apple.com/en-us/102445).

Open **How to use** in the app, or read the [user guide](docs/guide.md), for cleanup, freezing, and recovery.

## CLI and scheduling

Choose **Tasks → Install CLI** in the app. You can then save a task and generate a review from Terminal:

```sh
igloo create weekly-review --cache-days 45 --freeze-days 180
igloo review weekly-review
```

Set a daily or weekly schedule in the app or CLI. Start with a review task, then allow automatic cache cleanup when you're happy with its scope and limits.

[Tasks, CLI, and scheduling guide](docs/tasks.md)

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/screenshots/tasks-light.png">
  <img src="docs/screenshots/tasks.png" alt="Task settings for age rules, exclusions, and cleanup limits">
</picture>

## Updates

Choose **Igloo → Check for Updates** to check published releases and download a verified update. Then choose **Install and relaunch**. Igloo checks the download, replaces the app in place and opens the new version. If a step fails, the old app stays in place.

Igloo 0.2.x and older cannot install an update. To update one of these versions, open the new DMG and drag Igloo to Applications. Replace the old app when the Finder asks. If the `igloo` command stops working after an update, open **Tasks** and choose **Reinstall CLI**.

If a check fails, the window explains the problem and links to the releases page; a rate-limit message includes the reset time when available. A smaller system-Python build is also supported for people who maintain Python 3.11 or later themselves. Updates keep your chosen variant.

## Keep your recovery files safe

Local files such as ignored configuration stay in the Freezer when you preserve or freeze a project; they are not uploaded to GitHub or encrypted. Choose a Freezer backup folder in Settings to keep verified copies on a separate disk or in cloud storage. Cloud uploads are unencrypted and require your confirmation. Reinstall dependencies after thawing.

## Contributing

Found a bug or have an idea? [Open an issue](https://github.com/tm-henningnt/Igloo/issues). Pull requests are welcome; see [Contributing](CONTRIBUTING.md) and the [development guide](docs/development.md) for builds and tests.

## License

Igloo is source-available under the [Sustainable Use License 1.0](LICENSE). You can use and modify it for personal, noncommercial, or internal business purposes for free. Sharing copies or modified versions is allowed only free of charge for noncommercial purposes; selling repackaged versions or charging for access requires separate permission. The full license governs these permissions.
