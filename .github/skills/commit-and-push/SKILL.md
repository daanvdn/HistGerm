---
name: commit-and-push
description: "Generate a conventional commit message from staged changes, commit with that message, and push the current branch. Use when: commit and push, generate commit message and push, publish staged changes."
---

# Commit and Push

Generate the commit message before performing any Git write operation, then use that
message to commit and push the staged changes.

## Workflow

### 1. Inspect staged changes

Use the `get_changed_files` tool with `sourceControlState: ['staged']` to retrieve all
staged diffs.

If there are no staged changes, inform the user and stop. Do not stage files
automatically.

### 2. Generate the commit message

Analyze the staged diffs and generate a Conventional Commits message before running
`git commit`.

Use this format:

```text
<type>(<scope>): <subject>

<optional body>

<optional footer>
```

- Choose an accurate type such as `feat`, `fix`, `refactor`, `test`, `docs`,
	`chore`, `style`, `perf`, or `ci`.
- Use an optional scope for the affected module or area.
- Write the subject in imperative mood, start it with lowercase, omit the final
	period, and keep it at 72 characters or fewer.
- Add a body when the motivation is not clear from the subject.
- Add a `BREAKING CHANGE:` footer when applicable.
- Do not mention internal phase names.

Store the complete generated message for the next step. Do not ask the user to copy
or re-enter it.

### 3. Commit

Run `git commit` using the exact message generated in step 2. Pass the subject and
each additional paragraph with separate `-m` arguments so no temporary message file
is needed.

If the commit fails, report the error and stop. Do not push.

### 4. Push

Determine the current branch and its upstream. If an upstream exists, run:

```powershell
git push
```

If no upstream exists, run:

```powershell
git push --set-upstream origin <current-branch>
```

If the repository has no `origin` remote, report that clearly and stop rather than
guessing a remote.

### 5. Report the result

Report the generated commit message, the new commit hash, and the branch and remote
that received the push.

## Safety

- Never use `git add .` or `git add -A`.
- Never amend an existing commit unless the user explicitly requests it.
- Never force-push unless the user explicitly requests it.
- Never push when commit creation fails.