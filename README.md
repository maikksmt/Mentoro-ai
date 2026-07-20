# MentoroAI

[🇬🇧 English](#-english-version) | [🇩🇪 Deutsch](#-deutsche-version)

---

# 🇬🇧 English Version

![Stars](https://img.shields.io/github/stars/maikksmt/mentoro-ai?style=for-the-badge)
![License](https://img.shields.io/github/license/maikksmt/mentoro-ai?style=for-the-badge)
![Python](https://img.shields.io/badge/Python-3.12-blue?style=for-the-badge)
![Django](https://img.shields.io/badge/Django-5.2-092E20?style=for-the-badge)

**MentoroAI** is a multilingual **AI learning platform** designed to make Artificial Intelligence accessible for
beginners, creators, and professionals.
It brings together **AI tools**, **guides**, **prompt libraries**, **use cases**, and **tool comparisons** in a clean
and structured way.

- **Tech Stack:** Django 5.2 (Python 3.12), Tailwind CSS, DaisyUI, Heroicons
- **License:** GPL-3.0-or-later
- **Live Site:** <https://www.mentoro-ai.com>

---

## ⭐ Why MentoroAI Exists

Many newcomers struggle with AI: unclear answers, hallucinations, poor prompts, or overwhelming information.
MentoroAI solves this by offering a friendly, structured, and practical entry into the AI world.

If you support this mission, leaving a ⭐ helps others discover the project.

---

## 🚀 Key Features

| Feature                | Description                                |
|------------------------|--------------------------------------------|
| **AI Tool Catalog**    | Find tools quickly with filtering & search |
| **Guides**             | Beginner-friendly explanations             |
| **Prompts**            | Ready-to-use prompts                       |
| **Use Cases**          | Real-world examples                        |
| **Comparisons**        | Tool vs Tool insights                      |
| **Multilingual**       | English + German content                   |
| **Starter Guide**      | Step-by-step introduction                  |
| **Editorial Workflow** | Author / Editor / Admin roles              |

---

## 🧪 Tech & Architecture

- **Backend:** Django 5.2
- **Frontend:** Tailwind CSS, DaisyUI
- **Database:** PostgreSQL
- **Rich Text:** TinyMCE
- **Translations:** django-parler
- **CI/CD:** GitHub Actions
- **Cache:** Django `DatabaseCache` (table `mentoroai_cache_table`)

---

## 🛠️ Development & Deployment

Common commands (see `docs/mentoroai-cheatsheet.md` for the full list):

```bash
make test           # full Django test suite with coverage
make lint           # ruff check .
make check          # python manage.py check
make check-deploy   # python manage.py check --deploy
python manage.py makemigrations --check --dry-run
```

### Cache table (required in production)

Production uses Django's `DatabaseCache` backend with the table
`mentoroai_cache_table` (see `mentoroai/settings/base.py`). **The table must
exist before the cache is first used.** Django creates it automatically only
in the test setup — never on a normal deploy — so the deployment must run:

```bash
python manage.py createcachetable
```

The command is idempotent and safe to run on every deploy. Without it, any
page relying on the cached public inventory (homepage, global footer) will
fail. This is not a migration; `manage.py migrate` does **not** create it.

> Note: deployment is triggered by `.github/workflows/deploy.yml`, which runs
> `~/deploy_mentoroai.sh` on the target host. That script is not part of this
> repository and therefore cannot be verified here — make sure it contains the
> `createcachetable` step.

---

## 📝 License & Contributions

MentoroAI is licensed under the **GNU GPLv3 (or later)**.

- Contributions are welcome
- All contributions are GPLv3+
- Commercial use allowed
- Redistribution must follow GPL rules

See: `LICENSE.txt`

---

## 👥 Contributors

- **Lead Developer:** Maik Kusmat
- Contributions welcome → see `CONTRIBUTING.md`

---

## 📬 Contact

- Website: <https://www.mentoro-ai.com>
- GitHub: <https://github.com/maikksmt/mentoro-ai>
- Email: <contact@mentoro-ai.com>

---

# 🇩🇪 Deutsche Version

![Stars](https://img.shields.io/github/stars/maikksmt/mentoro-ai?style=for-the-badge)
![License](https://img.shields.io/github/license/maikksmt/mentoro-ai?style=for-the-badge)
![Python](https://img.shields.io/badge/Python-3.12-blue?style=for-the-badge)
![Django](https://img.shields.io/badge/Django-5.2-092E20?style=for-the-badge)

**MentoroAI** ist eine moderne, mehrsprachige **Lernplattform für Künstliche Intelligenz**, die Anfänger, Kreative und
Profis unterstützt, KI richtig zu verstehen und praktisch einzusetzen.
Die Plattform bietet **AI-Tools**, **Guides**, **Prompts**, **Use Cases** und **Vergleiche** in einer klar
strukturierten Oberfläche.

- **Tech Stack:** Django 5.2 (Python 3.12), Tailwind CSS, DaisyUI, Heroicons
- **Lizenz:** GPL-3.0-or-later
- **Live:** <https://www.mentoro-ai.com>

---

## ⭐ Warum gibt es MentoroAI?

Viele Menschen probieren KI-Tools aus und sind enttäuscht: unklare Antworten, Halluzinationen oder einfach
Überforderung.
MentoroAI schafft Klarheit, Orientierung und verständliche Inhalte — ohne Fachjargon.

Wenn dir die Mission gefällt, freue ich mich über ein ⭐ Sternchen.

---

## 🚀 Hauptfunktionen

| Funktion               | Beschreibung                               |
|------------------------|--------------------------------------------|
| **AI Tool Catalog**    | durchsuchbare Übersicht aktueller KI-Tools |
| **Guides**             | Verständliche Erklärungen                  |
| **Prompts**            | Nutzbare Prompts                           |
| **Use Cases**          | Praxisbeispiele                            |
| **Comparisons**        | Tool-Vergleiche                            |
| **Mehrsprachig**       | Deutsch + Englisch                         |
| **Starter Guide**      | Leichter Einstieg                          |
| **Editorial Workflow** | Rollen-System                              |

---

## 🧪 Technik & Architektur

- **Backend:** Django 5.2
- **Frontend:** Tailwind CSS, DaisyUI
- **Datenbank:** PostgreSQL
- **Rich Text:** TinyMCE
- **Übersetzungen:** django-parler
- **CI/CD:** GitHub Actions
- **Cache:** Django `DatabaseCache` (Tabelle `mentoroai_cache_table`)

---

## 🛠️ Entwicklung & Deployment

Häufige Kommandos (vollständige Liste: `docs/mentoroai-cheatsheet.md`):

```bash
make test           # komplette Django-Testsuite mit Coverage
make lint           # ruff check .
make check          # python manage.py check
make check-deploy   # python manage.py check --deploy
python manage.py makemigrations --check --dry-run
```

### Cache-Tabelle (in Produktion erforderlich)

Produktion nutzt Djangos `DatabaseCache` mit der Tabelle
`mentoroai_cache_table` (siehe `mentoroai/settings/base.py`). **Die Tabelle
muss vor der ersten Nutzung existieren.** Django legt sie nur im Test-Setup
automatisch an — nie bei einem normalen Deployment. Das Deployment muss daher
ausführen:

```bash
python manage.py createcachetable
```

Der Befehl ist idempotent und kann bei jedem Deployment laufen. Ohne ihn
schlagen alle Seiten fehl, die das gecachte Public Inventory nutzen
(Startseite, globaler Footer). Es handelt sich nicht um eine Migration;
`manage.py migrate` legt die Tabelle **nicht** an.

> Hinweis: Das Deployment wird über `.github/workflows/deploy.yml` angestoßen
> und führt `~/deploy_mentoroai.sh` auf dem Zielhost aus. Dieses Skript liegt
> nicht im Repository und kann hier deshalb nicht geprüft werden — stelle
> sicher, dass es den `createcachetable`-Schritt enthält.

---

## 📝 Lizenz & Beiträge

MentoroAI steht unter der **GNU GPLv3 (oder später)**.

- Beiträge sind willkommen
- Alle Beiträge werden GPLv3+
- Kommerzielle Nutzung erlaubt
- Weitergabe muss GPL-konform erfolgen

Siehe: `LICENSE.txt`

---

## 👥 Mitwirkende

- **Lead Developer:** Maik Kusmat
- Beiträge willkommen → siehe `CONTRIBUTING.md`

---

## 📬 Kontakt

- Webseite: <https://www.mentoro-ai.com>
- GitHub: <https://github.com/maikksmt/mentoro-ai>
- Email: <contact@mentoro-ai.com>
