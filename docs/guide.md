# Using Igloo

Start by choosing your projects folder in **Settings**, then click **Analyse Projects**. Igloo looks for projects and Git repositories, checks their local state, and estimates how much disk space they use. Scanning does not change project files.

## First launch

The current release is not notarized or signed with Apple Developer ID. Open the DMG and drag Igloo to Applications. If macOS blocks it, try opening the app, then approve Igloo in **System Settings → Privacy & Security → Open Anyway**. See [Apple’s instructions](https://support.apple.com/en-us/102445). macOS may ask for approval again after an update.

## Find a place to start

The project list can show quiet projects, active sessions, cleanup opportunities, or projects with work to preserve. Search by name or technology, filter by project type, and sort by size or age.

Select a project to see its overview. The Git, Worktrees, Local files, and Cleanup tabs let you look more closely at anything that needs attention. Igloo suggests project types from their files; you can correct a suggestion in Overview.

The scope panel on the right follows your current filters. Open **Space Explorer** for a larger view with sunburst, icicle, and Marimekko charts. Warm age colors mean recent Git activity, cold colors mean older work, and gray means unknown. Disk figures are estimates, so the amount reclaimed may differ.

## Clean up dependencies and caches

Open a project's **Cleanup** tab, select the folders you want to remove, and review the preview. Igloo rechecks the selection before moving it to Trash. Use **Trash & restore history** if you need to restore it.

Check for custom changes before removing dependencies you normally regenerate. Active projects and folders containing tracked files or local configuration can be held for review.

For a larger cleanup, open **Cleanup wizard**:

1. Choose how to measure age: Git activity or source-file modification time.
2. Set a cleanup age and a later freeze age.
3. Scan, then adjust the proposed action for each project.
4. Review the folders, Git destinations, local archives, and checkout removals.
5. Accept the plan to run its ready actions.

Projects younger than the first threshold stay as they are. Between thresholds, Igloo proposes cache cleanup. Past the second threshold, it proposes freezing instead. Active sessions and unknown age or activity are protected by default.

The results show what completed and what was held or failed. If something changes during the run, inspect the result and make a fresh plan for the remaining work.

## Preserve work before putting a project away

Use the **Git** tab to review changes, branches, stashes, and remotes. The **Local files** tab helps you spot ignored configuration and data that an ordinary push would leave behind.

In **Plan**, you can connect an existing remote or create a private GitHub repository. Choose the GitHub account or organization explicitly. You can also review and commit selected source files. Commit source changes before freezing, and check committed history before publishing it.

## Freeze a project

Freezing is for projects you want to keep but no longer need on disk as a working checkout.

1. Commit the source changes you want to keep and close applications or terminals using the project.
2. Choose **Plan → Freeze**.
3. Review the branches and tags to publish, local files to archive, folders to omit, and affected worktrees.
4. Choose **Archive & push reviewed source**. Igloo creates local recovery archives, tests restoration, pushes the reviewed source, and verifies it remotely.
5. Open **Frozen projects** and choose **Remove verified checkouts** when you're ready to reclaim the space.

The wizard can complete the full freeze, including checkout removal, when you accept that action in its plan.

Ignored local files, stashes, and other local Git state stay in your recovery archives. Rebuildable folders such as node_modules can be left out. Review the omissions, especially if you've edited dependencies locally.

## Thaw a project

Open **Frozen projects**, select the project, and choose **Thaw from remote**. Igloo restores the frozen source state, local files, and worktrees. **Offline recovery** uses the local recovery copy when you don't have remote access.

Existing destination files are never overwritten. Reinstall dependencies afterward, then pull newer remote changes when you're ready. Keep the archive until you're satisfied everything you need is back.

## Bring another repository onto your Mac

Open **GitHub repositories** to browse your account or an organization. You can hide repositories already cloned locally, select several, and review their destination folders before cloning. Existing folders are left untouched.

Run **Analyse Projects** afterward to add the clones to your project list.

## Repeat the routine

Save a task for rules you want to reuse. Tasks can prepare fresh plans or clean matching caches within your chosen limits. Daily and weekly schedules are optional; automatic cleanup needs explicit permission.

See [Tasks, CLI, and scheduling](tasks.md) for examples.

## Updating Igloo

Choose **Igloo → Check for Updates** from the app menu or About screen. Igloo checks published releases, shows the release notes, and downloads the version for your Mac and runtime choice.

After the checksum has been verified, choose **Show in Finder**. Open the ZIP, quit Igloo, and move the new app into Applications. Your projects, settings, and local archives stay where they are. If you use the CLI, choose **Tasks → Reinstall CLI** after updating.

The standard app keeps its own Python inside the app bundle and leaves your system Python and PATH alone. The optional system-Python build uses Python 3.11 or later from your Mac; its path can be changed in Settings.

## Settings and backups

Settings are stored in `~/.config/igloo`. Catalogs, history, and other app data default to `~/.igloo`, with recovery archives in `~/.igloo/vault`. You can choose different storage locations in Settings.

Archives stay on your Mac and are not encrypted. Back them up separately, especially before removing a checkout. A remote repository alone does not preserve your ignored files or all local Git state.

Changing a storage location selects another folder; it does not move existing archives for you. Keep the previous location until you've moved and checked anything you still need.
