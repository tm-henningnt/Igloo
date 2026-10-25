# Tasks, CLI, and scheduling

Save a task when you want to repeat the same cleanup rules. Each run scans your projects again and records what it found and what happened.

Start with **Plan only** to see the suggestions without moving files. Choose **Cache cleanup** when you want to apply a reviewed plan or allow scheduled cleanup.

## Set up a task in the app

Open **Tools → Tasks** and create a task:

1. Give it a name and a short ID, such as weekly-review.
2. Choose Git activity or source-file age, then set cleanup and freeze ages.
3. Pick the cache types and projects to include. Add exclusions for anything you want left alone.
4. Set limits on projects and disk space per run.
5. Save, then choose **Plan now**.

Recent projects stay as they are. Projects between the two ages get cache cleanup; older projects are held for freeze review. Tasks never commit, push, freeze, remove project checkouts, or empty Trash.

The wizard's **Save rules as a task** button is another starting point. It creates a plan-only task from those rules; individual project overrides are not copied.

## Review and run

Open a saved plan from the task's history to see the exact folders and any held projects. For a cleanup task, choose **Apply reviewed cleanup** to accept it.

Reviews expire after ten minutes. If you change the rules or the project changes, prepare a new plan.

Only allow automatic cleanup for folders you're willing to regenerate. Igloo checks for active sessions, tracked files, and local configuration, but dependencies can still contain custom edits you care about.

## Install the CLI

Choose **Tasks → Install CLI**. The command is installed at `~/.local/bin/igloo`; add `~/.local/bin` to your shell's PATH if needed. Use **Reinstall CLI** after updating the app.

### Create a review task

```sh
igloo create weekly-review --cache-days 45 --freeze-days 180
igloo plan weekly-review
igloo history weekly-review
```

The default is plan-only, using Git activity, with limits of five projects and ten GiB per run. Nothing is scheduled automatically.

Use `igloo tasks` to list tasks and `igloo show weekly-review` to inspect one. Creating a task with an existing ID replaces its saved rules.

### Clean up after reviewing a plan

```sh
igloo create weekly-caches --mode cleanup --cache-days 45 --freeze-days 180
igloo plan weekly-caches
```

Review the plan in the app's task history. The CLI prints its ID. To accept that exact plan:

```sh
igloo apply PLAN_ID --accept
```

To scan and execute directly under the saved task rules:

```sh
igloo run weekly-caches --apply
```

Without `--apply`, running a task only saves a plan.

### Limit what a task can touch

Use `--include` and `--exclude` for folders, `--type` for a project type, and `--cache-kind` for a cache type. For example:

```sh
igloo create python-caches --mode cleanup --age files --cache-kind .venv --cache-kind __pycache__ --include "$HOME/Projects/Examples" --exclude "$HOME/Projects/Examples/Current Work" --type "Command-line tool" --max-projects 3 --max-gib 4
```

Folders must be inside your configured projects folder. Empty include and project-type lists mean all projects; exclusions always win. A project's known linked worktree caches are reviewed with it.

Extra projects beyond the project limit are held. If the proposed batch exceeds the space limit, the whole batch is held. Changing your workspace or archive location requires updating the task.

## Schedule a task

In the app, choose the task's **Schedule** section and set a daily or weekly time. Plan-only tasks produce reports. Cleanup tasks also need unattended cleanup enabled in their saved rules.

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

Use the task's **History** section or `igloo history TASK_ID` to see completed, held, skipped, and failed runs. Open the saved plan for details. Caches moved to Trash can be restored through **Trash & restore history**.

If a run stops unexpectedly, inspect its plan and recovery history before running a fresh plan. Don't assume an interrupted run completed everything.

For scripts, add `--json` to task or history commands. Exit codes are 0 for a saved plan, completed run, or nothing to do; 1 for a refusal, failure, held, or partial result; and 75 when another operation is running.
