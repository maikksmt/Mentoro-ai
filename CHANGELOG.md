# 🧾 Changelog

All notable changes to **MentoroAI** will be documented in this file.   
This project follows the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format  
and adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0-beta-11] – 2026-07-31

### Added

- shared, surface-independent editorial action primitive (`apply_editorial_action`,
  `core/editorial_actions.py`): the Django Admin and the Editorial Workspace now
  delegate to the same code for submit-for-review, request-rework, approve,
  publish, archive and restore, instead of two implementations that only agreed
  on a status string
- explicit, leak-free publish-marker scope: `last_published_revision_id` is now
  set from a bounded `ContextVar` scope after `django-reversion` has actually
  written the revision's versions, on both surfaces — an Admin bulk selection
  keeps landing in one shared revision, a Workspace action opens its own
- complete rework/resubmit loop in the Editorial Workspace: an editor must give
  a reason for "Request rework" (empty or whitespace-only is rejected), the
  reason is stored as part of the revision, the author sees it in "My Content"
  and can resubmit for review through the same workflow contract — for Guide,
  Prompt, Use Case and Comparison alike
- review and approval bindings for Guide, Prompt, Use Case and Comparison: a
  submitted review and a granted approval are now bound to the concrete
  revision they were given for (`review_revision`, `approved_revision`),
  instead of being a status value that any later edit could silently outdate
- Prompt review payload v2 with a stored payload fingerprint
  (`review_payload_fingerprint`): the reviewed state of a prompt is captured
  canonically, so a later content change invalidates the approval instead of
  publishing something that was never reviewed
- live-author snapshot for Prompt (`live_author`): the author name shown on a
  published prompt is frozen at publish time and only changes on a conscious
  republish, so renaming an account no longer rewrites already published pages
- draft-preview and object-reference hardening for the admin preview
  endpoints: an unsupported language, a missing translation and an object the
  requester may not preview all fail closed with the same 404
- public tool filter: public guide, prompt and use-case output resolves tools
  exclusively through `Tool.objects.public()`, so non-public or archived tools
  can no longer surface in published editorial content

### Changed

- content titles in "My Content" and the Review Queue open the Django Admin
  change form in a new tab (`target="_blank" rel="noopener noreferrer"`)
- the Editorial Workspace remains workflow-only: content creation and editing
  continue to be handled exclusively in Django Admin; a planned Workspace
  editing surface was intentionally not adopted

### Notes

- Prompt keeps its specialised submit/approve/publish contract (review
  payload, fingerprint, review/approval bindings, live-author snapshot)
  unchanged; the shared primitive above does not replace it
- `last_published_revision_id` still stores a `reversion.Version` id despite
  its historical name, not a `Revision` id; a published live snapshot stays
  visible while new draft/review changes are prepared internally
- Exact Reversion and Recovery remain Django Admin functions; Archive/Restore
  are available to Author/Editor per the existing role contract, Hard Delete
  stays Admin/Superuser-only
- tool deletion in the Admin is blocked while a `ComparisonToolEntry` still
  references the tool (single and bulk delete); a plain direct
  `Guide.tools`/`Prompt.tools`/`UseCase.tools` membership is not an additional
  delete blocker
- rich-text content continues to go through the existing `bleach`-based
  central sanitizer and TinyMCE editor/upload endpoint; workspace-rendered
  editorial text (e.g. rework reasons) uses plain Django autoescaping, never
  `mark_safe`
- Admin draft preview remains an Admin-only feature for Guide, Prompt, Use
  Case and Comparison — the Editorial Workspace has no preview route
- test loader inventory at the end of this cycle: 4,019 collected tests,
  4,019 unique IDs, 0 duplicates (up from an earlier full-suite validation of
  3,914 tests / 0 failures / 0 errors / 0 skips taken mid-cycle); the final
  editorial-workspace state was verified with a focused ~166-test scope run
  normally, `--reverse`, and with two `--shuffle` seeds — not a repeated full
  suite
- Beta 11 includes six schema and data-backfill migrations across the
  Comparison, Guide, Prompt and Use Case editorial models: `review_revision`,
  `approved_revision` and `review_payload_fingerprint` are added to all four
  root types, Prompt additionally receives the review-payload v2 data
  migration and the `live_author` snapshot field with its backfill
- no new dependencies: `requirements.txt` is unchanged against `dev`
- Beta 11 establishes the editorial review, preview, revision, publishing,
  and snapshot foundation. Final role-ownership hardening and editorial
  workflow notification badges are scheduled for Beta 12 and are required
  before the next production release.

---

## [1.0.0-beta-10] – 2026-07-21

### Added

- global full-text search across tools, guides, prompts, use cases and comparisons, in English and German
  (`/en/search/`, `/de/search/`), using PostgreSQL full-text search (`websearch` queries, title/summary/body
  weighting) rather than a naive substring match
- global search entry point in both the desktop and mobile navigation, replacing the earlier search placeholder
  dialog

### Changed

- search results are strictly language-bound: a result only surfaces the public, published revision of a guide,
  prompt, use case or comparison, and a tool only matches on its actual translation in the requested language
  (no cross-language fallback used as a search index)
- a failing search fails closed: the visitor sees a plain "temporarily unavailable" state (HTTP 503) with no
  technical detail, and no partial result list is ever shown
- editorial card intros/teasers (guide, prompt, use case and comparison cards, including the homepage "latest
  content" section) are shortened by one shared, word-boundary-safe function instead of each call site cutting
  text on its own; a card only ever appends `...`, and only when the text was actually shortened

### Fixed

- editorial card intros could be cut off mid-word with no `...` marker, and adjacent rich-text blocks could be
  glued together into a single run-on sentence in the homepage "latest content" section and in related-content
  sections on guide/prompt/use-case/comparison detail pages
- a pre-existing, order-dependent catalog test failure caused by ambient search-page language state leaking
  between tests

### Notes

- the search page is server-rendered, needs no JavaScript, and is marked `noindex,follow` and excluded from the
  sitemap
- search snippets (the excerpt shown per result on the search results page) keep their own, pre-existing
  ellipsis convention (`…`) and are a separate mechanism from the editorial card intro shortening (`...`)
- the glossary is not part of the global search
- no migrations, no new dependencies

---

## [1.0.0-beta-9] – 2026-07-20

### Added

- roadmap "what's coming" dialog on the homepage, with an accessible name (`aria-labelledby`), autofocus on open and focus returned to its
  trigger on close
- shared `_editorial_card.html` component unifying guide/prompt/use-case/comparison list cards, homepage "latest content" teasers and every
  related-content section (replaces the former `_teaser_card.html`/`_guideitem_card.html` duplicates)
- shared pagination partial (`partials/pagination.html`) used by the catalog and every paginated editorial list, with `<nav>`, `aria-label`
  and `aria-current`
- grouped, labeled catalog filter panel (search, free-tier checkbox, category) with a visible active-filter summary and a "reset"/"clear
  all" escape hatch
- `.reading-column.prose` (70ch) for long-form guide/prompt/use-case/comparison/tool body content
- scoped `.touch-target` utility raising mobile tap targets to ~40px on header login/logout, catalog/comparison filter Search and Reset, the
  tool-card website link, the prompt copy button, and glossary A–Z/pagination controls — without any global `.btn`/`a` override
- screen-reader-only title context on repeated "Read more"/"Weiterlesen" card links, so they read as distinct links rather than identical
  ones in a links list

### Changed

- theme tokens and surface colors recalibrated for consistent contrast in light and dark
- mobile root font-size restored to a 16px baseline; typography scale (`page-title`, `page-lead`, `section-title`) and detail-page reading
  headers unified across all content types
- main navigation, mobile menu (now with a dedicated scrim) and detail-page shells unified across guides, prompts, use cases and
  comparisons; desktop nav is sticky, mobile nav is not
- homepage section order reprioritized for returning users (current content and personalized recommendations promoted ahead of the area
  overview)
- decorative icons (theme toggle, search) hidden from the accessibility tree; `role="button"`/`aria-pressed` removed from plain navigating
  links (header login/logout, glossary navigation, starter guide and author-info CTAs) that carried no real toggle state

### Fixed

- comparison list `q`/`query` query-parameter mismatch that broke pagination on filtered results
- German homepage translation inaccuracies

### Notes

- 1.0.0-beta-9 closes with a dedicated read-only UI regression audit across all public page types, both languages, light/dark, and
  320–1920px — no additional Beta 9 regressions or dead Beta 9 code were found; a pre-existing (pre-Beta-9) horizontal-overflow gap for wide
  tables inside rich-text body content on narrow viewports was identified and is tracked separately, not fixed in this cycle
- no migrations, no new dependencies

---

## [1.0.0-beta-8] – 2026-07-19

### Added

- search placeholder dialog ("Search is coming soon") in the main navigation
- theme switching with persisted preference (`localStorage`, key `mentoroai-theme`) and system fallback
- dynamic public inventory (tool/category/guide/prompt/use case/comparison counts) for homepage and footer
- semantic, grouped global footer (Explore / Browse categories / Start here / Legal)
- explicit starter guide resolution via `is_starter` instead of a hardcoded slug
- sticky desktop navigation (`lg` and up; mobile navbar stays static)
- active-section highlighting in the main navigation

### Changed

- mobile navigation now uses the DaisyUI dropdown; dead drawer JavaScript removed
- accessibility basics: skip link, `#main-content` landmark, dialog focus handling
- public guides, prompts, use cases and comparisons are filtered strictly by language — no cross-language fallback on lists, detail pages,
  related content or counts
- public URLs, titles, intros, bodies and teasers are resolved from the live snapshot instead of the current (possibly draft) translation
- related guides/prompts/use cases/comparisons are language-safe
- related use case persona ranking is case-insensitive, explicitly language-scoped and no longer awards a point for two empty personas
- public inventory cache key versioned to `v5` as the count semantics became language-strict

### Fixed

- prompt copy button layout
- foreign-language content rendered under the wrong URL prefix
- draft slugs publicly reachable
- HTTP 500 on guides without a translation in the active language
- broken comparison language-switcher targets
- empty related comparison cards
- order-dependent navigation/context-processor tests
- sitemaps were language-independent while their URLs were generated under the active language prefix, so single-language content was listed
  in both `/en/sitemap.xml` and `/de/sitemap.xml` — producing 404 targets (foreign-language slug under the wrong prefix) and
  malformed `<loc>` entries (`https://<domain>#`) for content without a translation in that language
- aligned scheduled tool visibility across catalog, detail pages, category counts, inventory and sitemaps: a tool with a
  future `published_at` was correctly hidden from the catalog list and the counts, but was still reachable under its detail URL, shown in
  the homepage featured-tools row, and listed in both sitemaps

### Deployment / Operations

- production uses `DatabaseCache` with the table `mentoroai_cache_table`; the table must exist before first use — Django only creates it
  automatically in the test setup, so `python manage.py createcachetable` is required on deploy (idempotent)
- no migrations and no new dependencies in this release cycle

---

## [1.0.0-beta-7] – 2026-07-18

### Added

-

### Changed

- replace Cookiebot CMP with Klaro Open Source solution
- minor UI improvements
- removing recommended_items from home view

### Fixed

- minor fixes and improvements
- update Klaro Cookiebanner texts

---

## [1.0.0-beta-6] – 2025-12-14

### Added

- error pages (400,403,404,500)
- comparison added to services functions
- Google Analytics 4 completely converted to Google Tag Manager

### Changed

- update for rich text layout examples page
- rework Comparison Views and Templates
- minor UI updates
- Comparison Data model
- Metadata
- Direct GA4 tracking removed from templates
- Privacy and cookie information adapted to new tracking architecture

### Fixed

- editorial workflow

---

## [1.0.0-beta-5] – 2025-12-07

### Added

- User Signup enabled
- add author and date information to editorial list and detail sites
- Richtext / TinyMCE Safelist
- Richtext Layout Notes and layout snippets for Authors
- tool cards for used tools in use cases.
- fonts for project.

### Changed

- update editorial backend
- update editorial templates
- update editorial groups permissions
- centralize admin object permissions and setup permissions for objects and Roles
- update to Django 5.2.9
- minor UI improvements
- richtext ALLOWED_TAGS and ALLOWED_ATTRS
- templates

### Fixed

- fix and optimize SEO
- behavior for published date for Editorial Objects to not be updated after changes.

---

## [1.0.0-beta-4] – 2025-11-30

### Added

- Tools Content

### Changed

- Optimizations for Tool-Dataset-System
- minor UI updates
- Glossary & Tools robustness improved
- Static / Media / White Noise Setup cleaned up
- Server Rate limiting optimized

### Fixed

- fix and optimize SEO
- TinyMCE: Image-Upload issue (CSP blob:) fixed

---

## [1.0.0-beta-3] – 2025-11-23

### Added

- rebuild Tools pages (list and detail)
- missing reversion usage for editorial workflow in usecases app
- Google Analytics
- Cookie Banner

### Changed

- Final step for user account management Dashboard and user registration.
- minor UI updates
- Update Tool Data model

### Fixed

- missing translation for badges

---

## [1.0.0-beta-2] – 2025-11-18

### Added

- minor UI updates
- add impress information

### Changed

- remove jsdeliver CDN for htmx, instead self hosted.

### Fixed

- bugfixes for Comparison Category select dropdown
    - every category were shown twice due to translation query issue
    - the category value causing a server error due to wrong query handling.
    - limit selectable categories to only available categories in available comparisons.
- bugfixes for wrong url links
- editorial fix where last published live version was not shown when article is set back to review.
- fix for missing compression and versioning of static files

---

## [1.0.0-beta-1] – 2025-11-13

### Added

- First public **Beta release** of the MentoroAI platform.
- Complete **multilingual content framework** (German/English) using *django-parler*.
- Core applications:
    - **Catalog** – Overview of AI tools with detailed tool pages.
    - **Glossary** – AI terminology database with categories.
    - **Guides** – Structured, multi-section learning guides.
    - **Prompts** – Curated prompt library with categories.
    - **Usecases** – AI use cases with filtering, categories, and tool assignments.
    - **Compare** – Tool comparison system.
- **i18n language switcher** with smart slug translation and fallbacks.
- **Django Admin enhancements**: translation tabs, inline editing, improved list and detail views.
- **Editorial system**: State machine with Roles and Rules for Editorial Workflow.
- **Tailwind CSS + DaisyUI theme** with dark/light mode.
- **Responsive frontend layout** with optimized cards, lists, and detail pages.
- **PostgreSQL integration** for development and production.
- **Newsletter system**: Allow to subscribe und unsubscribe with email.

### Changed

n.a

### Fixed

n.a.

---

## Versioning Notes

This project follows **Semantic Versioning 2.0.0**:

- **MAJOR**: incompatible API changes.
- **MINOR**: new features without breaking changes.
- **PATCH**: bug fixes.

Current version: **1.0.0-beta-11**.<br>
Breaking changes may still occur before the stable 1.0 release.
---

**Author:** Maik Kusmat  
**Repository:** [github.com/maikksmt/mentoro-ai](https://github.com/maikksmt/mentoro-ai)
