# Contributing to MentoroAI

Thank you for your interest in contributing to **MentoroAI**.  
This document explains the contribution process, quality standards, and legal framework.

---

## Overview

MentoroAI is a public, open-source Django project.  
Contributions in the form of bug reports, improvements, documentation, or code are welcome.  
Please read this guide before submitting a pull request.

---

## Code of Conduct

We expect respectful and professional communication.  
Discrimination, personal attacks, or abusive language will not be tolerated.  
Constructive criticism is welcome and should include actionable suggestions.

---

## Development Environment

Minimum requirements:

- Python 3.12
- Django 5.x
- Node.js and npm (for frontend builds with TailwindCSS/DaisyUI)
- Git

Setup example:

```bash
git clone https://github.com/maikksmt/mentoro-ai.git
cd mentoroai
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
npm install
python manage.py migrate
python manage.py runserver
```

Frontend development (optional):

```bash
npm run dev
```

---

## Code Quality & Style

- Python: Follow **PEP 8**, include type hints when possible
- Formatting: e.g. **black** and **ruff**
- Tests: Required for all new features and bug fixes
- Security: Never commit secrets or `.env` files
- Accessibility and performance should always be considered for templates and CSS changes

Commit message examples:

```
fix(compare): correct breadcrumbs
feat(starter): enable section editing in admin
```

---

## Branch Strategy & Pull Requests

- Main branches: `main` (stable) and `dev` (active)
- Create feature branches from `dev`: `feature/<short-description>`
- Open pull requests against `dev`
- Describe motivation, changes, and migration steps in PR description
- Link related issues

Review checklist:

- No regressions; all tests must pass
- Clean structure, meaningful commits, proper documentation
- External dependencies must be license-compatible

---

## Editorial Content Contributions

- Use clear, neutral, and precise language
- Always cite external sources and data
- Do not include copyrighted material without permission

### Where content is created and edited

Guides, prompts, use cases, and comparisons — including their translated
fields, rich-text body content, guide sections/items, comparison tool
entries, and direct relations (categories, tools) — are created and edited
exclusively in the **Django Admin**. This is a deliberate product decision,
not a temporary limitation: the Admin is, and stays, the single content
management surface for all four editorial types.

The **Editorial Workspace** (`/en/editorial/`, `/de/editorial/`) is a
separate, workflow-only surface. It covers the existing review lifecycle —
submit for review, request rework (with a required reason) and resubmit,
approve, publish, archive, restore — plus the "My Content" and "Review
Queue" overviews, for the roles the underlying rules already allow. It does
**not** offer content creation, content editing, translation forms, child
editing (sections, items, comparison entries), preview, revision history, or
recovery; content titles in the workspace open the corresponding Admin
change form in a new tab (`target="_blank" rel="noopener noreferrer"`) so a
user with the necessary Admin permission can edit there, while a user
without Admin access simply cannot create or edit content — no additional
permissions or `is_staff` are granted through the workspace.

If you are contributing code, do not reintroduce content create/edit forms
into the Editorial Workspace; that direction was deliberately reversed
during Beta 11 in favor of this Admin-only contract.

---

## License & Contributor Agreement

MentoroAI is licensed under the **GNU General Public License v3 (or later)**.  
By submitting a pull request, you agree that your contribution will be released under GPLv3+.  
Ensure you hold the necessary rights to your work.

More details: see `LICENSE.txt`.

---

## Security Policy

- Report vulnerabilities privately to the maintainer
- Do not include secrets, API keys, or tokens in pull requests

---

## Contact

- Maintainer: Maik Kusmat
- Email: contact@mentoro-ai.com

---

## 🙏 Thank You

Your contribution helps make **MentoroAI** better  
and supports open access to reliable AI knowledge.

> _“Open knowledge for everyone who wants to learn and understand.”_
