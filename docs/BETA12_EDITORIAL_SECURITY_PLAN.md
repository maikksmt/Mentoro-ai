# Beta 12 – Editorial Roles, Ownership and Notifications

Internal technical planning document. Not a public release note.

This document is the binding plan for the final editorial role, ownership,
recovery and workflow-notification hardening. It records the complete outcome
of the Beta 11.15A permission audit and translates it into implementable
slices. Public release notes must never reproduce the finding details below.

---

## 1. Status and Release Gate

- **Beta 11 is a technical milestone.** It delivers the editorial security,
  rich-text, review, preview, revision, publishing and snapshot foundation.
- **Beta 11 may be merged into `dev`.** Beta 11.14 confirmed a conflict-free
  merge simulation against `dev`, 4,019 collected tests with 4,019 unique IDs
  and 0 duplicates, a full suite with 0 failures / 0 errors / 0 skips, green
  Ruff, green Django checks, technically consistent migrations, unchanged
  dependencies and a clean working tree.
- **Beta 11 alone is not a fully hardened production release** for the final
  role and ownership contract. Beta 11 must not be presented as delivering
  that contract.
- **Beta 12 is a mandatory blocker before the next production release.**
- The starting basis for Beta 12 is the Beta 11.14 validation state plus the
  Beta 11.15A ownership and permission audit.
- **Every Beta 12 slice requires its own tests.** A green pre-existing suite
  proves the previous contract, not the new one.
- After the code slices, a **new full suite run** is required; the Beta 11.14
  numbers do not carry over once behaviour changes.

---

## 2. Final Role Contract

### 2.1 Technical access

- `is_staff` is granted manually by the superadmin only.
- Ordinary registration grants no `is_staff`, no admin access, no editorial
  role, no model permissions and no workflow rights.
- `is_staff` is only the technical gateway into the Django Admin.
- Effective rights derive from: **role + ownership + content status + the
  concrete action.** `is_staff` alone confers no content rights.

### 2.2 Roles

- **Author** — full rights on own content only; no access to foreign content.
- **Editor** — same rights as Author on own content; on foreign content
  strictly review, approve and request-rework.
- **Admin** — the Django group `Admin` is the business admin role.
- **Superadmin** — `is_superuser` is the technical superadmin.

Business Admin and superuser may fully manage foreign content, hard delete
included. Admin and superuser nevertheless create content through the normal
Add View **under their own account only**; author transfer is not part of the
normal content form.

### 2.3 Role matrix

| Action | Author own | Author foreign | Editor own | Editor foreign | Admin/Superuser |
|---|---|---|---|---|---|
| List/View (normal changelist) | allow | deny | allow | deny (review queue only) | allow |
| Create | allow (self as author) | n/a | allow (self as author) | n/a | allow (self as author) |
| Edit | allow | deny | allow | deny | allow |
| Translation Edit | allow | deny | allow | deny | allow |
| Child Edit (sections, items, entries) | allow | deny | allow | deny | allow |
| Preview | allow | deny | allow | deny | allow |
| Diff | allow | deny | allow | deny | allow |
| History | allow | deny | allow | deny | allow |
| Reversion | allow | deny | allow | deny | allow |
| Recovery | allow | deny | allow | deny | allow |
| Submit Review | allow | deny | allow | deny | allow |
| Approve | deny (no self-approval) | deny | deny (no self-approval) | allow | allow |
| Request Rework | deny | deny | deny | allow | allow |
| Publish | allow | deny | allow | deny | allow |
| Archive | allow | deny | allow | deny | allow |
| Restore | allow | deny | allow | deny | allow |
| Hard Delete | deny | deny | deny | deny | allow |

### 2.4 Workflow ownership

- After approval the content moves to the approved status; the **author**
  publishes their own content, never the reviewing editor.
- After a rework request the **author** performs the rework and resubmits.
- Foreign content surfaces for editors **only** in the review queue, never in
  a normal changelist.
- Direct URLs of non-editorial admin models stay blocked server-side.

---

## 3. Beta 12.1 – Author Assignment and Immutability

**Covers:** K1, K2, part of M4.

### Goals

- The `author` field is read-only or not editable in all four root admins
  (Guide, Prompt, Use Case, Comparison).
- On create the author is always assigned server-side to `request.user`.
- A tampered `author` value in the POST body has no effect.
- The rule applies to Author, Editor, Admin **and** superuser alike.
- After creation the author is immutable through the normal admin edit path.
- No author transfer exists in the normal content form.
- Reversion and recovery must not swap the author.

### Required tests

- GET add view per role and per root type: the author field is absent or
  read-only, never a selectable dropdown.
- POST add view with a foreign `author` id per role and per root type: the
  created object belongs to the acting user.
- POST change view with a foreign `author` id per role and per root type: the
  stored author is unchanged.
- Change-form, inline/formset, bulk-action and custom-action paths.
- Revision revert and recovery confirmations leave the author untouched.

---

## 4. Beta 12.2 – Root Ownership and Editor Review Boundaries

**Covers:** K4, H1, H2, H6, H7, M3, M4, M6.

### Goals

- Separate, object-aware predicates for Author, Editor, Admin and superuser.
  The current editor predicate ignores the object entirely and must not.
- `Editor` and `Admin` become distinguishable business roles in the central
  predicates instead of being merged by a shared group-name constant.
- Authors and editors see only their own root content in normal changelists.
- Foreign content is reachable for editors only through the review queue.
- Foreign change views fail closed.
- Editor rights on foreign content are exactly: **Approve** and **Request
  Rework** (with a mandatory reason).
- Editors must not, on foreign content: edit, preview, submit for review,
  publish, archive, restore, view history, view or revert revisions, recover,
  or delete.
- The author publishes after a foreign editor's approval.
- The author performs the rework after a rework request.
- Both workspace routes (`review_update` and `my_content_update`) must be
  fail-closed against object-id tampering, not only role-gated.

### Deliberate test replacement

An existing test asserts the previous contract — that an editor may edit
content that is not their own. Beta 12.2 must **consciously replace** it with
the new contract rather than delete it silently. The replacement is part of
the slice's acceptance, and the docstrings that describe the old global editor
access must be corrected with it.

---

## 5. Beta 12.3 – Child, Inline and Relation Ownership

**Covers:** H8, M1, N1.

### Goals for own content

**Guide**

- create sections; edit sections; remove sections
- create items; edit items; remove items
- translations
- categories
- tools
- ordering

**Comparison**

- comparison tool entries
- ordering
- tool selection
- translations

**Prompt and Use Case**

- translations
- categories
- tools
- further M2M relations

Authors currently cannot fully edit existing sections and items of their own
guide, because the group's Django model permissions do not cover the child
models while the inline permission check consults exactly those. This slice
must close that gap — either by granting the missing child permissions or by
deriving inline permissions from root ownership — without widening foreign
access.

### Security requirements

- A foreign root blocks every child change.
- Formset tampering is rejected.
- Manipulated child ids are rejected.
- Direct child admin URLs enforce root ownership.
- Foreign-key querysets are restricted to objects the user owns.
- M2M POSTs are validated against root ownership.
- Fix the readonly-field typo in the Comparison admin: `["intro, body"]`
  (a single string) must become `["intro", "body"]`.

---

## 6. Beta 12.4 – Preview, Diff, History, Reversion and Recovery Security

**Covers:** K3, H3, H4, H5, N2.

### Target contract

| Function | Author/Editor own | Author/Editor foreign | Admin/Superuser |
|---|---|---|---|
| Preview | allow | deny | allow |
| Diff | allow | deny | allow |
| History | allow | deny | allow |
| Revision View | allow | deny | allow |
| Exact Reversion | allow | deny | allow |
| Recovery | allow | deny | allow |

### Additional requirements

- Prefer HTTP 404 over 403 for disallowed foreign objects, so the endpoint
  never confirms that a given object id exists.
- No existence or metadata leaks — revision users, comments and version lists
  of foreign content must not be visible.
- Block IDOR through `version_id`, `revision_id` and object id.
- The recover list shows only the user's own deleted content.
- Ownership of a deleted root must be determined from the stored version data,
  since the object is no longer in the model admin queryset.
- **The foreign recovery POST must be verified empirically and blocked.** The
  audit proved read access to both the recover list and the recover form; the
  write effect was not driven to completion and remains an open question that
  Beta 12.4 must settle.
- The author must remain unchanged through reversion and recovery.
- When one revision contains several root objects, each root is authorised
  separately.
- Child objects and translations inside a revision are covered by the same
  ownership rule as their root.
- `diff_view` returns 404 instead of raising a 500 when the object is missing.

---

## 7. Beta 12.5 – Permission Defense in Depth

**Covers:** M2, M5, N3, N4.

### Goals

- Restrict hard delete explicitly to business Admin and superuser, for single
  and bulk delete. Hard delete is currently blocked only because the Author
  and Editor groups lack the Django `delete_*` model permissions; the
  object-level hook itself would allow editors. A second, explicit role check
  is required so that a future permission grant cannot silently open the path.
- The approval primitive performs its own permission check as a second line of
  defence, matching the publish primitive, instead of relying on the calling
  view alone.
- The central workflow primitives verify authorisation themselves.
- Remove unused or misleading role helpers in the workspace views; they
  currently shadow the names of the real predicates without being used.
- Correct docstrings that still describe the old global editor access as the
  intended contract.
- Keep non-editorial admin models blocked (this contract already holds and
  must not regress).
- Verify autocomplete, raw-id and Select2 foreign-key endpoints. No root admin
  currently declares such fields, so the endpoints should be unreachable; this
  was not probed directly and must be confirmed.
- Update the permissions of already-existing groups reliably, not only on
  first creation.

---

## 8. Beta 12.6 – Editorial Review Badges

### 8.1 Editor Review Badge

A badge showing the number of open reviews appears on the **Reviews** button
or menu entry.

Counting contract:

- status `review` only;
- across Guide, Prompt, Use Case and Comparison;
- only content the editor is actually allowed to review;
- **foreign content only** — the editor's own content never counts;
- archived or otherwise non-reviewable content is excluded;
- the badge uses the **same business queryset as the review queue**, so the
  number and the list can never disagree;
- the badge is hidden at 0;
- optionally displayed as `99+`, while the internal value stays exact;
- rendered on desktop and mobile;
- available in EN and DE;
- carries an appropriate accessibility label;
- computed server-side on each page load;
- no WebSocket and no polling in the minimum scope.

Administrators may receive the same counter when they use the review queue.

### 8.2 Author Review Result Badge

A badge appears on the **My Content** / **Meine Inhalte** menu entry when a
review has been processed:

- review approved;
- rework requested.

The badge belongs to the **owner** of the content. An editor also receives it
for their own content when another reviewer processed it.

---

## 9. Notification Data Model

Plan a persistent model, along the lines of `EditorialWorkflowNotification`.

Fields and contracts:

- `recipient`
- `actor`
- `ContentType`
- `object_id`
- an optional durable content identifier
- `event_type`
- the relevant version/revision reference
- `created_at`
- `read_at`
- an idempotency key

Initial event types:

- `review_approved`
- `rework_requested`

Requirements:

- The event is written **in the same transaction** as the workflow action.
- No event on rollback or on a failed action.
- Idempotent: repeated requests never produce duplicate events.
- No notification for the recipient's own action.
- Several review cycles may legitimately produce several business events.
- The badge counts **unread events**, not merely the current content status.
- Read state is per user and shared across devices.
- A hard delete of the content must not corrupt the notification history —
  hence the durable identifier alongside the generic relation.
- Existing reviews are **not** retroactively backfilled.
- Notifications start with the Beta 12 deployment.

---

## 10. Notification Acknowledgement

Preferred implementation:

- Opening **My Content** shows the relevant new events.
- Read state is **not** changed uncontrolled by an ordinary GET.
- Acknowledgement runs through a **CSRF-protected POST**.
- Only events actually visible to the user are acknowledged.
- The badge is reduced or hidden afterwards.
- Parallel tabs and repeated requests are idempotent.
- Foreign notification ids can never be acknowledged.
- EN/DE and accessibility are covered by tests.

Optional:

- Additionally mark the affected content row as new.

Explicitly **not** part of the binding minimum scope:

- email; push notifications; WebSocket; polling; a global notification-centre
  UI.

---

## 11. Beta 12.7 – Migration and Rollout

- A migration for the notification model.
- Update the permissions of existing groups (`Author`, `Editor`, `Admin`), not
  only newly created ones.
- Existing content keeps its current authors.
- **No automatic author transfer.**
- **No historical notification backfill.**
- Deployment order: migrate, update group permissions, deploy code, verify.
- Production-like data verification covering at least:
  - a guide with sections and items;
  - a comparison with tool entries;
  - a published live snapshot;
  - own and foreign deleted content;
  - rollback and recovery boundaries.

---

## 12. Beta 12.8 – Documentation and Changelog

Document:

- the final role matrix;
- `is_staff` versus the business role;
- Admin-only content create/edit;
- review-only access to foreign content;
- preview, reversion and recovery bound to ownership;
- badge semantics;
- notification read state;
- migration and rollout information.

**No detailed attack paths in public release notes.** The public changelog
records capabilities and contracts; the finding details stay in this document.

---

## 13. Beta 12.9 – Final Security and Merge Validation

Binding final validation:

- test loader count; unique ids; no duplicates;
- full test suite;
- permission tests run normally;
- permission tests run with `--reverse`;
- permission tests run with several `--shuffle` seeds;
- direct IDOR probes;
- all four root types;
- Author, Editor, Admin and superuser;
- create-time author tampering;
- immutable author;
- root ownership;
- child ownership;
- preview; diff; history; reversion; recovery;
- hard delete;
- editor on foreign content limited to approve and rework only;
- editor review badge;
- my-content badge;
- notification read state;
- browser smoke tests;
- merge simulation;
- clean working tree.

---

## 14. Audit Finding Coverage

| Finding | Beta 12 slice | Acceptance criterion |
|---|---|---|
| K1 – author tampering on create | 12.1 | POST `author=<foreign id>` has no effect for Author, Editor, Admin and superuser across all four root types |
| K2 – author mutable after create | 12.1 | The stored author never changes through any normal admin edit path |
| K3 – recovery of foreign content reachable | 12.4 | Recover list and recover form deny foreign content; the foreign recovery POST is proven blocked |
| K4 – foreign content and revisions editable by editors | 12.2 | Object-aware predicates; foreign change POST and foreign revision revert fail closed |
| H1 – editor publishes foreign content | 12.2 | Publish on foreign content is denied on both workspace routes and in the admin |
| H2 – editor archives/restores foreign content | 12.2 | Archive and restore on foreign content are denied on every surface |
| H3 – `diff_view` without object check | 12.4 | Diff denies foreign objects with 404 |
| H4 – foreign history visible | 12.4 | History denies foreign objects; no revision metadata leaks |
| H5 – foreign revision view accessible | 12.4 | Revision view and revert deny foreign objects |
| H6 – foreign draft preview for editors | 12.4 (contract), 12.2 (predicate) | Preview denies foreign objects for both Author and Editor |
| H7 – unfiltered root changelists | 12.2 | Normal changelists list own content only; foreign content appears solely in the review queue |
| H8 – child/inline/translation/relation bypass | 12.3 | Every child change requires root-edit permission; formset and id tampering rejected |
| M1 – authors cannot edit own sections/items | 12.3 | Authors fully edit sections and items of their own guides |
| M2 – hard delete only implicitly blocked | 12.5 | Explicit role check for single and bulk delete, independent of group permissions |
| M3 – `Editor` and `Admin` not separable | 12.2 | Predicates express Author, Editor, Admin and superuser distinctly |
| M4 – existing test locks the old contract | 12.1, 12.2 | The old-contract test is consciously replaced by new-contract tests |
| M5 – approval primitive without own check | 12.5 | The approval primitive verifies permission itself |
| M6 – editor may submit foreign content for review | 12.2 | Submit-for-review on foreign content is denied |
| N1 – `["intro, body"]` typo | 12.3 | Corrected to `["intro", "body"]` |
| N2 – `diff_view` 500 on missing object | 12.4 | Missing object yields 404 |
| N3 – unused/misleading role helpers | 12.5 | Removed or renamed; no shadowing of the real predicates |
| N4 – docstrings describe the old contract | 12.5 | Docstrings match the final role contract |
| Open: foreign recovery POST effect | 12.4 | Empirically proven and blocked |
| Open: tampering probes for Prompt, Use Case, Comparison | 12.1 | Empirical probes exist for all four root types, not only Guide |
| Open: autocomplete / raw-id / Select2 paths | 12.5 | Verified unreachable or ownership-restricted |
| Open: several root objects in one revision | 12.4 | Each root authorised separately |
| Admin model isolation (already met) | 12.5 | Regression tests keep non-editorial admin models blocked |
| Editor review badge | 12.6 | Counts foreign reviewable content only, matches the review queue queryset |
| Author review result badge | 12.6 | Appears for the content owner on approval and on rework request |

---

## 15. Slice Dependencies

Recommended order:

1. Beta 12.1 – Author Assignment and Immutability
2. Beta 12.2 – Root Ownership and Editor Review Boundaries
3. Beta 12.3 – Child, Inline and Relation Ownership
4. Beta 12.4 – Preview, Diff, History, Reversion and Recovery Security
5. Beta 12.5 – Permission Defense in Depth
6. Beta 12.6 – Editorial Review Badges
7. Beta 12.7 – Migration and Rollout
8. Beta 12.8 – Documentation and Changelog
9. Beta 12.9 – Final Security and Merge Validation

Dependencies:

- 12.3 builds on the root gate established in 12.2.
- 12.4 must preserve the author contract from 12.1.
- 12.5 follows the business permission corrections.
- 12.6 uses the final review querysets from 12.2.
- 12.7 follows the notification model from 12.6.
- 12.9 follows all code and documentation slices.
