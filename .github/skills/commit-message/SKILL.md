---
description: >
  Generate a conventional commit message from staged git changes.
  USE FOR: writing commit messages, summarizing staged changes, preparing commits.
  DO NOT USE FOR: unstaged changes, git operations beyond message generation.
applyTo: "**"
---

# Commit Message Generator

Generate a clear, conventional commit message based on the currently staged git changes.

## Workflow

### Step 1 — Gather staged changes

Use the `get_changed_files` tool with `sourceControlState: ['staged']` to retrieve all staged diffs.

If there are **no staged changes**, inform the user and stop:
> "No staged changes found. Stage your changes with `git add` first."

### Step 2 — Analyze changes

Classify the changes by examining the diffs:

1. **Type** — Determine the primary change type:
   | Type       | When to use                                      |
   |------------|--------------------------------------------------|
   | `feat`     | New functionality or capability                  |
   | `fix`      | Bug fix                                          |
   | `refactor` | Code restructuring without behavior change       |
   | `test`     | Adding or updating tests                         |
   | `docs`     | Documentation only                               |
   | `chore`    | Build, config, dependencies, maintenance         |
   | `style`    | Formatting, whitespace, linting (no logic change)|
   | `perf`     | Performance improvement                          |
   | `ci`       | CI/CD pipeline changes                           |

2. **Scope** — Identify the affected module or area (e.g., `auth`, `transactions`, `frontend`). Use the top-level directory or logical component name. Omit scope if changes span many unrelated areas.

3. **Breaking changes** — Flag if the change alters public API contracts, database schema in a non-additive way, or removes existing functionality.

### Step 3 — Compose the commit message

Follow the **Conventional Commits** format:

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Rules for each part:**

- **Subject line** (first line):
  - Use imperative mood ("add", "fix", "remove" — not "added", "fixes", "removed")
  - Lowercase start, no period at end
  - Max 72 characters
  - Must be specific — avoid vague phrases like "update code" or "fix stuff"

- **Body** (optional, separated by blank line):
  - Include when the *why* isn't obvious from the subject
  - Explain motivation and contrast with previous behavior
  - Wrap at 72 characters
  - Use bullet points for multiple distinct changes

- **Footer** (optional):
  - `BREAKING CHANGE: <description>` if applicable
  - Reference issues: `Closes #123`, `Relates to #456`

### Step 4 — Present the message

Output the commit message inside a single fenced code block so the user can copy it directly.

If the staged changes cover **multiple unrelated concerns**, suggest splitting into separate commits and provide a message for each logical group.

## Quality Criteria

- [ ] Type accurately reflects the nature of the change
- [ ] Subject is specific, concise, and uses imperative mood
- [ ] Scope matches the affected area (or is omitted for broad changes)
- [ ] Body is included when the change is non-trivial
- [ ] Breaking changes are called out in the footer
- [ ] Message would be understandable to a teammate reading `git log` months later

## Examples

**Simple feature:**
```
feat(transactions): add date range filter to transaction list
```

**Bug fix with body:**
```
fix(auth): prevent token refresh loop on 401 response

The refresh interceptor was retrying indefinitely when the refresh
token itself was expired. Now it redirects to login after one failed
refresh attempt.
```

**Multiple changes (suggest split):**
> These staged changes touch unrelated areas. Consider splitting into separate commits:

```
fix(rules): correct category matching for negative amounts
```
```
test(rules): add edge case tests for zero-amount transactions
```
