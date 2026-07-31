# MentoroAI – Befehls-Cheatsheet (aktualisiert)

Kurzreferenz für häufige Projekt-Kommandos (Django 5.2, Python 3.12, Tailwind/DaisyUI).

---

## i18n (Übersetzungen)

- `make makemessages` – Extrahiert Übersetzungsstrings für `de` und `en`.
- `make compilemessages` – Kompiliert `.po` zu `.mo`.

**Raw**

```bash
django-admin makemessages -l de -l en --ignore venv --ignore node_modules --no-wrap
django-admin compilemessages
```

---

## Django Checks

- `make check` – Allgemeine Projekt-Checks.
- `make check-deploy` – Produktionsrelevante Checks.
- `make showmigrations` – Übersicht der Migrationsstände.

**Raw**

```bash
python manage.py check
python manage.py check --deploy
python manage.py showmigrations
```

---

## Datenbank & Migrations

- `make makemigrations` – Neue Migrationen erstellen.
- `make migrate` – Migrationen anwenden.
- `make createsuperuser` – Admin-User erzeugen.
- `make shell` – Django-Python-Shell.

**Raw**

```bash
python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser
python manage.py shell
```

---

## Cache (Production-Voraussetzung)

Production nutzt `DatabaseCache` mit der Tabelle `mentoroai_cache_table`
(`mentoroai/settings/base.py`). Die Tabelle muss **vor** der ersten Nutzung
existieren — Django legt sie außerhalb des Test-Setups nicht automatisch an,
und `migrate` erzeugt sie ebenfalls nicht.

**Raw**

```bash
python manage.py createcachetable          # idempotent, bei jedem Deploy sicher
python manage.py createcachetable --dry-run  # zeigt nur das SQL an
```

Betroffen sind alle Flächen, die das gecachte Public Inventory nutzen
(Startseite, globaler Footer). Der Cache-Key ist sprachabhängig und versioniert
(`mentoroai:public-inventory:v5:<lang>`, TTL 300 s), siehe
`core/services.py`. Die Version wird nur erhöht, wenn sich die Count-Semantik
ändert — ein globales `cache.clear()` ist dafür nicht nötig und in
Produktionscode nicht vorgesehen.

---

## Globale Suche (Search-App, Beta 10)

Zentrale Module in `search/`:

- `query.py` – normalisiert die rohe Query; Grenzen: leer, < 2 Zeichen oder
  > 100 (normalisierte) Zeichen ergeben eine leere Response, ohne dass ein
  Adapter aufgerufen wird
- `fts.py` – PostgreSQL Full-Text Search (`SearchQuery(search_type="websearch")`,
  `SearchRank(normalization=32)`, `@@` als Match-Prädikat, `english`/`german`
  Sprachkonfiguration), Gewichte A (Titel) / B (Summary) / C (Body)
- `ranking.py` – ein einziger, globaler Sortierlauf über alle Adapterergebnisse
  (Match-Tier, Rank, Aktualität, Kind, Objekt-ID) statt Adapter-lokaler
  Normalisierung oder Typgewichtung
- `snippets.py` – Plaintext-Ausschnitt je Ergebnis für die Suchseite
- `registry.py` – feste Adapter-Liste (`SEARCH_ADAPTERS`); das Glossar ist
  bewusst nicht enthalten
- `services.py` – `search_site(*, raw_query, language_code)` → immutable
  `SearchResponse`; fail-closed (`SearchExecutionError`, keine Teilresultate)
- `views.py` – `SearchResultsView`; loggt einen Adapterfehler serverseitig,
  ohne Suchbegriff oder SQL in der Log-Nachricht

Adapter (`search/adapters/`): `tools.py`, `guides.py`, `prompts.py`,
`usecases.py`, `comparisons.py` – je ein Adapter pro Inhaltstyp, liefern
immutable `SearchResult`-Objekte für eine explizit übergebene Sprache.

**Sichtbarkeits-/Sprachregeln**

- Guide/Prompt/UseCase/Comparison: öffentliche Suchwerte kommen aus der
  veröffentlichten Revision (Snapshot-autoritativ, falls vorhanden); Draft-Werte
  und Draft-Slugs erreichen die Suche nie
- Tool: `Tool.objects.public()` ist die einzige Sichtbarkeitsgrenze; die Suche
  arbeitet direkt auf der angeforderten `ToolTranslation`, ohne
  Parler-Cross-Language-Fallback als Suchindex (ein EN-only Tool erscheint
  also nicht in der deutschen Suche, unabhängig vom Katalog-Fallback)

**Suchseite**

- URLs: `/en/search/`, `/de/search/`; ausschließlich GET, ausschließlich
  Parameter `q`
- serverseitig gerendert, kein JavaScript nötig
- `SearchExecutionError` → HTTP 503, keine technischen Details in der Ausgabe
- `robots="noindex,follow"`, nicht in der Sitemap

**Card-Kürzung vs. Search-Snippets**

- `core/text.py` (`summarize_html()` / `truncate_at_word_boundary()`,
  `EDITORIAL_INTRO_MAX_CHARS = 200`) ist die einzige Kürzungsstelle für
  Editorial-Card-Intros (Guide/Prompt/UseCase/Comparison-Listen,
  „Aktuelle Inhalte“, Related-Content-Abschnitte). Kürzt ausschließlich an
  Wortgrenzen und hängt nur bei tatsächlicher Kürzung exakt `...` an.
- `search/snippets.py` ist davon getrennt und behält seine eigene, bereits
  bestehende typografische Ellipse (`…`) für den Ausschnitt auf der
  Suchergebnisseite.

**Raw**

```bash
make test-app APP=search   # nur die Search-App
```

---

## Editorial Workspace (Workflow, Beta 11)

Der Editorial Workspace unter `/en/editorial/` bzw. `/de/editorial/` ist
**ausschließlich eine Workflow-Oberfläche**, keine Content-Management-Fläche.
Erstellung und Bearbeitung von Guide/Prompt/UseCase/Comparison — inklusive
übersetzter Felder, Rich Text, Guide Sections/Items, Comparison Tool Entries
und direkter Relationen — bleiben vollständig dem Django Admin vorbehalten
(`content/urls_editorial.py` registriert keine Create-/Edit-Route).

**Tatsächlich registrierte Routen** (`content/urls_editorial.py`):

- `me/content/` (`my_content`) – eigene Inhalte, Statusanzeige, Reworkgrund
- `me/submit/` (`submit_to_review`) – Submit/Resubmit (POST)
- `me/update/` (`my_content_update`) – Statuswechsel durch Author/Editor (POST)
- `review/` (`review_queue`) – Review Queue für Editor/Admin
- `review/update/` (`review_update`) – Approve/Rework/Publish/Archive/Restore (POST)
- `layout-examples/` – statische Richtext-Referenzseite

Inhaltstitel in „My Content“ und der „Review Queue“ verlinken auf die
bestehende Admin-Change-Seite des Objekts (`{% admin_change_url %}`) und
öffnen dort mit `target="_blank" rel="noopener noreferrer"` in einem neuen
Tab. Ein Nutzer ohne Admin-Zugriff kann dadurch bewusst keine Inhalte
erstellen oder bearbeiten — es werden keine zusätzlichen Rechte und kein
`is_staff` über den Workspace vergeben.

**Rework-/Resubmit-Kreislauf** (Beta 11.13D1G-a)

- Editor muss bei „Request rework“ einen Grund angeben; ein leerer oder
  reiner Whitespace-Grund wird serverseitig abgelehnt
- der Grund wird als Plain Text gespeichert (normales Autoescaping, kein
  `mark_safe`) und ist Teil der Reversion-Revision
- der Author sieht den Grund in „My Content“ und kann den Inhalt über
  denselben Submit-Endpunkt erneut zur Prüfung einreichen
- gilt einheitlich für Guide, Prompt, Use Case und Comparison

**Gemeinsame Workflowprimitiven** (Beta 11.13D1B/D1B1, `core/editorial_actions.py`)

- Admin und Workspace delegieren für fachlich gleiche Aktionen (Submit,
  Rework, Approve, Publish, Archive, Restore) an dieselbe
  `apply_editorial_action()`-Primitive statt an zwei getrennte Implementierungen
- eine Admin-Bulkauswahl bleibt in einer gemeinsamen Revision; eine
  Workspace-Aktion öffnet ihren eigenen Revisionskontext
- der Publishmarker (`last_published_revision_id`) wird über einen
  explizit begrenzten, leakfreien `ContextVar`-Scope
  (`publish_marker_scope()`) gesetzt, erst nachdem Reversion die
  zugehörigen `Version`-Zeilen tatsächlich geschrieben hat
- Prompt behält seinen spezialisierten Submit-/Approve-/Publish-Vertrag
  (Review-Payload, Fingerprint, Bindings, Live-Author-Snapshot) unverändert;
  die gemeinsame Primitive ersetzt diesen Pfad nicht

**Reversion- und Publish-Semantik**

- django-reversion ist die zentrale Revisionsbasis für alle vier
  Editorialtypen
- `last_published_revision_id` trägt trotz des historischen Namens eine
  `reversion.Version`-ID, nicht die `Revision`-ID der zugehörigen Revision
- Review-/Approval-Bindings referenzieren konkrete, geprüfte Versionen
- ein veröffentlichter Live-Snapshot bleibt öffentlich unverändert
  sichtbar, während intern neue Draft-/Reviewänderungen vorbereitet werden;
  diese Draftänderungen leaken nicht vor dem nächsten Publish
- Exact Reversion (Revert auf eine Revision) ist ein destruktiver
  Admin-Vorgang (`revision.revert(delete=True)`); Recovery gelöschter
  Objekte bleibt ebenso eine Admin-Funktion
- Archive/Restore stehen Author und Editor gemäß Rollenvertrag zur
  Verfügung; Hard Delete bleibt Admin/Superuser vorbehalten

**Tool-Lifecycle** (`catalog/`)

- öffentliche Guide-/Prompt-/UseCase-Ausgaben verwenden ausschließlich
  `Tool.objects.public()`; nicht öffentliche bzw. archivierte Tools
  erscheinen dadurch nicht versehentlich in öffentlichen Editorialinhalten
- eine Tool-Löschung wird im Admin blockiert (`get_deleted_objects()`
  meldet das Tool an Djangos eigene `protected`-Liste), sobald mindestens
  ein `ComparisonToolEntry` das Tool referenziert — sowohl Einzel- als auch
  Bulk-Löschung; eine Auswahl mit einem geschützten Tool wird als Ganzes
  blockiert, keine Teillöschung
- reine direkte M2M-Beziehungen (`Guide.tools`, `Prompt.tools`,
  `UseCase.tools`) ohne `ComparisonToolEntry` sind nach diesem Vertrag
  **kein** zusätzlicher Delete-Blocker
- zur Cache-Konsistenz des Public-Inventory-Caches (bis zu 300 s Eventual
  Consistency) siehe Abschnitt „Cache (Production-Voraussetzung)“ oben

**Raw**

```bash
make test-app APP=content   # Workspace-Views, Rework-Loop, Action-Parity
```

---

## Tests & Coverage

- `make test` – Alle Django-Tests mit Coverage (mit `.coveragerc` & Settings).
- `make test-app APP=<app>` – Tests für eine App.
- `make pytest` – Tests mit pytest (falls eingerichtet).
- `make cov-html` – Coverage-HTML-Report erstellen.
- `make cov-xml` – XML-Report nach `coverage.xml`.

**Raw**

```bash
coverage erase
coverage run --rcfile=.coveragerc -m django test --settings=mentoroai.settings -v 2
coverage report -m
coverage html --rcfile=.coveragerc
coverage xml  --rcfile=.coveragerc
```

---

## Lint & Format

- `make lint` – Lint mit Ruff.
- `make format` – Formatierung mit Ruff.

**Raw**

```bash
ruff check .
ruff format .
```

---

## Staticfiles & Frontend

- `make collectstatic` – Statische Dateien einsammeln.
- `make build-frontend` – Frontend-Abhängigkeiten installieren & Build erzeugen.

**Raw**

```bash
python manage.py collectstatic --noinput
npm ci && npm run build
```

---

## Dependency Management (pip-tools)

- `make pip-compile` – Kompiliert `requirements.in` → `requirements.txt` (mit Upgrade).
- `make pip-sync` – Synchronisiert venv exakt zu `requirements.txt`.

**Raw**

```bash
pip-compile requirements.in --output-file=requirements.txt --upgrade
pip-sync requirements.txt
```

---

## Security

- `make pip-audit` – Paketabhängigkeiten auf CVEs prüfen.
- `make bandit` – Statische Sicherheitsanalyse.
- `make security` – Beide Security-Checks ausführen.

**Raw**

```bash
pip-audit
bandit -r .
```

---

## Server/Dev

- `make runserver` – Entwicklungsserver starten (0.0.0.0:8000).

**Raw**

```bash
python manage.py runserver 0.0.0.0:8000
```

---

## Pre-Deployment Pipeline (Ein Kommando)

- `make predeploy` – Führt in Reihenfolge aus:
    1. `make pip-compile`
    2. `make pip-sync`
    3. `make lint`
    4. `make makemessages`
    5. `make compilemessages`
    6. `make makemigrations`
    7. `make migrate`
    8. `make test`
    9. `make build-frontend`
    10. `make collectstatic`
    11. `make check-deploy`
    12. `make security`

**Hinweis:** Tools ggf. vorher installieren (`pip-tools`, `ruff`, `coverage`, `pip-audit`, `bandit`, `pytest`).

---

## Variablen & Overrides

Du kannst Tools/Interpreter überschreiben, z. B.:

- `make build-frontend NPM=pnpm`
- `make runserver MANAGE="python manage.py"`
- `make test COVER=coverage`
