# 🧩 Git Workflow for MentoroAI (2025)

This workflow describes the complete Git / GitHub process for **MentoroAI**,  
based on the current branching and merge rules.

---

## Branch Structure

| Branch                  | Purpose                                     |
|-------------------------|---------------------------------------------|
| `main`                  | Stable production releases                  |
| `dev`                   | Active development base for features        |
| `feature/<description>` | Short-lived feature branches                |
| `hotfix/<description>`  | Urgent production fixes derived from `main` |

**Principles**

- `main` is never edited directly — only through PRs
- `dev` is the integration branch
- New features branch from `dev`
- Releases merge from `dev` → `main`

---

## Branch Rules (GitHub Settings)

### 🔹 main

- Require PR before merging
- Require review (≥ 1 approval)
- Require conversation resolution
- Allow **Merge commits** (for releases)
- Disallow force pushes or deletions

### 🔹 dev

- Require PR before merging
- Enforce **linear history** ✅
- Allow **Squash and Merge** (for features)
- Disallow force pushes or deletions

---

## Merge Strategy

| From → To                 | Method                  | Purpose                          |
|---------------------------|-------------------------|----------------------------------|
| `feature/* → dev`         | **Squash and Merge**    | Clean, single-commit history     |
| `dev → main`              | **Create Merge Commit** | Release bundle with full history |
| `hotfix/* → main`         | **Create Merge Commit** | Urgent production fix            |

> 💡 **No squash for releases!**  
> Prevents “1 commit ahead / behind” drift.

---

## Commit Conventions

```
feat(starter): add section editing in admin
fix(compare): correct breadcrumb links
docs(readme): add project overview
refactor(content): simplify mixin logic
chore(ci): add GitHub Actions cache
```

**Types:** `feat`, `fix`, `docs`, `refactor`, `test`, `build`, `chore`, `ci`

---

## Feature Sync & Rebase

Before merging a feature branch:

- In PyCharm: Branch → **Update from dev**<br>
  (rebases with latest `dev`)
- Resolve conflicts in editor → commit merge result

---

## Release Process

1. Ensure `dev` is stable (tests + manual check passed)

   Minimum pre-release checks:

   ```bash
   make test                                        # full suite
   ruff check .
   python manage.py check
   python manage.py check --deploy                  # see note below
   python manage.py makemigrations --check --dry-run
   ```

   > `check --deploy` reports SSL/cookie/HSTS/DEBUG warnings when run against
   > the local development settings. Those settings are set correctly in
   > `mentoroai/settings/production.py`; only warnings that also appear with
   > the production settings are release-relevant.

   Deployment prerequisite: the `DatabaseCache` table must exist on the target
   host (`python manage.py createcachetable`) — see `README.md`.

2. Create PR → `dev → main`
3. Title: `release: vX.Y.Z`
4. Merge method: **Create Merge Commit**
5. On GitHub: **Releases → Draft new release → Tag** (`vX.Y.Z`)
6. After release: update `dev` from `main` to sync

---

## Hotfix Flow

1. Branch → **New Branch → hotfix/<description>** (from `main`)
2. Commit & push fix
3. PR → `main`, merge commit
4. Sync `main` → `dev` afterwards

---

## Common Issues

| Problem                   | Cause                         | Solution                                  |
|---------------------------|-------------------------------|-------------------------------------------|
| “1 commit ahead / behind” | Squash merge used for release | Use **merge commit** for releases         |
| Conflicts                 | Out-of-sync branches          | Use **Merge Conflicts** dialog in PyCharm |
| “Force push forbidden”    | Branch protection rule        | Expected — keeps repo safe                |
| PR blocked by checks      | Missing CI workflow           | Disable checks until CI configured        |

---

## Naming Conventions

| Type    | Example                | Description           |
|---------|------------------------|-----------------------|
| Feature | `feature/editor-rbac`  | new module / feature  |
| Fix     | `fix/compare-links`    | small correction      |
| Hotfix  | `hotfix/runtime-error` | urgent production fix |

---

## Roles & Permissions

| Role            | Responsibilities                          |
|-----------------|-------------------------------------------|
| **Maintainer**  | Merge/review, branch protection, releases |
| **Contributor** | Feature PRs, code changes                 |
| **Reviewer**    | Code review & feedback                    |

---

## TL;DR

1. Work in `dev`
2. Create feature branch → commit & push
3. PR → `dev` → Squash & Merge
4. Release PR `dev → main` → Merge Commit
5. Tag release `vX.Y.Z`

---

✅ **Result**

- Clean, traceable Git history
- Full GitHub integration
- No “ahead/behind” confusion
- Clear separation between development and release phases  
