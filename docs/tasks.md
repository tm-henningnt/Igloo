# Tasks, CLI, and scheduling

Save a task when you want to repeat the same cleanup rules. Each run scans your projects again and records what it found and what happened.

Start with **Review only** to see the suggestions without moving files. Choose **Cache cleanup** when you want to accept a Review or allow scheduled cleanup.

## Set up a task in the app

Open **Tools → Tasks** and create a task:

1. Give it a name and a short ID, such as weekly-review.
2. Choose Git activity or source-file age, then set cleanup and freeze ages.
3. Pick the cache types and projects to include. Add exclusions for anything you want left alone.
4. Set limits on projects and disk space per run.
5. Save, then choose **Review now**.

Recent projects stay as they are. Projects between the two ages get cache cleanup; older projects are held for freeze review. Tasks never commit, push, freeze, remove project checkouts, or empty Trash.

Put a project on **Hold** to exclude it from wizard proposals and task actions, including scheduled cleanup. The review lists it with “On hold.” Notes stay with the project. Existing Keep decisions become Hold; the other old decisions become unheld and keep their notes. Manual cleanup, Preserve and Freeze remain available and show the Hold in their Review.

The Project wizard starts with **Make space** and **Thaw projects**. Only **Make space** has rules to save as a task. Its **Save rules as a task** button creates a review-only task from those rules; individual project overrides are not copied. Tasks do not thaw projects.

## Review and run

Open a saved review from the task's history to see the exact folders and any held projects. For a cleanup task, choose **Apply reviewed cleanup** to accept it.

Reviews expire after ten minutes. If you change the rules or the project changes, prepare a new review.

Preserve keeps the checkout after verifying local recovery and the reviewed remote refs. Freeze uses one Review and one acceptance: it runs Preserve, verifies recovery again, then removes the checkouts. If a process opens files, content changes, or verification fails before removal, the checkout stays and the Freezer records why Freeze stopped. A remote is required for both actions.

Every Review summarizes checkout paths, space estimates, Freezer archives, refs and their destinations, folders moved to Trash, and projects left out. Wizard and task summaries include their ready rows. Results show an **Estimate** beside measured **Freed from disk**, **In Trash**, and **Restored from Trash** values. Freed from disk is the gross allocated size removed from a checkout. Bytes moved to Trash remain on disk, so a Cleanup can correctly show 0 bytes freed from disk and a positive In Trash value. Restored from Trash counts bytes returned through Trash & restore history and lowers In Trash for that run. **Net space change after recovery** is the actual disk-space change after accounting for archives; it can differ from Freed from disk because archives use disk space.

A **Thaw plan** lists checkouts and remote refs to restore, the verified Freezer archive copy used for recovery, and any blocked projects. The remote supplies refs when available, with the verified Freezer archive copy as a fallback for missing Frozen refs. Thaw does not measure space. After a Thaw, results report the checkouts and remote refs restored. Freeze estimates use allocated file bytes and an uncompressed recovery estimate; compression and filesystem allocation can change the result.

The Review states the number of recovery sets and their Freezer location. Exact recovery paths are assigned during execution and appear in the result.

Only allow automatic cleanup for folders you're willing to regenerate. Igloo checks for active sessions, tracked files, and local configuration, but dependencies can still contain custom edits you care about.

## Install the CLI

Choose **Tasks → Install CLI**. The command is installed at `~/.local/bin/igloo`; add `~/.local/bin` to your shell's PATH if needed. Use **Reinstall CLI** after updating the app.

### Create a review task

```sh
igloo create weekly-review --cache-days 45 --freeze-days 180
igloo review weekly-review
igloo history weekly-review
```

The default is review-only, using Git activity, with limits of five projects and ten GiB per run. Nothing is scheduled automatically.

Use `igloo tasks` to list tasks and `igloo show weekly-review` to inspect one. Creating a task with an existing ID replaces its saved rules.

### Clean up after a Review

```sh
igloo create weekly-caches --mode cleanup --cache-days 45 --freeze-days 180
igloo review weekly-caches
```

Inspect the Review in the app's task history. The CLI prints its ID. To accept that exact Review:

```sh
igloo apply REVIEW_ID --accept
```

To scan and execute directly under the saved task rules:

```sh
igloo run weekly-caches --apply
```

Without `--apply`, running a task only saves a review.

### Limit what a task can touch

Use `--include` and `--exclude` for folders, `--type` for a project type, and `--cache-kind` for a cache type. For example:

```sh
igloo create python-caches --mode cleanup --age files --cache-kind .venv --cache-kind __pycache__ --include "$HOME/Projects/Examples" --exclude "$HOME/Projects/Examples/Current Work" --type "Command-line tool" --max-projects 3 --max-gib 4
```

Folders must be inside your configured projects folder. Empty include and project-type lists mean all projects; exclusions always win. A project's known linked worktree caches are reviewed with it.

Extra projects beyond the project limit are held. If the proposed Review exceeds the space limit, the whole selection is held. Changing your workspace or Freezer location requires updating the task.

## Schedule a task

In the app, choose the task's **Schedule** section and set a daily or weekly time. Review-only tasks produce reports. Cleanup tasks also need unattended cleanup enabled in their saved rules.

From the CLI, schedule the review task above:

```sh
igloo schedule weekly-review --frequency weekly --day sun --at 09:00
```

For automatic cache cleanup, create a task with explicit permission first:

```sh
igloo create automatic-caches --mode cleanup --cache-days 45 --freeze-days 180 --max-projects 5 --max-gib 10 --allow-unattended
igloo schedule automatic-caches --frequency weekly --day sun --at 09:00
```

Enabling a schedule does not run it immediately. Times are local to your Mac, and you must be logged in. A run missed during sleep may happen after wake; Igloo doesn't wake the Mac or guarantee catch-up after shutdown or logout.

If you edit the task's rules, re-enable its schedule to approve the changes. A busy app or CLI causes a scheduled run to skip rather than overlap another operation.

To stop a schedule and optionally delete the task:

```sh
igloo unschedule automatic-caches
igloo delete automatic-caches
```

Deleting a task keeps its run history.

## Check results and recover files

Use the task's **History** section or `igloo history TASK_ID` to see completed, held, skipped, and failed runs with Estimate, Freed from disk, In Trash, and Restored from Trash values. Older run records show **not measured**. Open the saved review for details. Caches moved to Trash can be restored through **Trash & restore history**; the space report updates the original run when an item is restored.

The **Space report** in Trash & restore history lists runs and totals for the last 30 days and all time. From Terminal, use `igloo space-report` for the same report. This does not empty Trash.

If a run stops unexpectedly, inspect its review and recovery history before running a fresh review. Don't assume an interrupted run completed everything.

For scripts, add `--json` to task, history, or space-report commands. Exit codes are 0 for a saved review, completed run, or nothing to do; 1 for a refusal, failure, held, or partial result; and 75 when another operation is running.
