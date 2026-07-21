# jacobmonzel.com

Personal website. Static HTML, no build step. Hosted on Cloudflare Pages,
auto-deployed from the `main` branch of this repository.

## Structure

```
/
├── index.html          Homepage
├── _redirects          Old URL -> new URL mappings (Cloudflare reads this)
├── assets/images/      Site-wide images (headshots, etc.)
├── cv/                 Curriculum vitae
├── papers/             Papers & preprints (index.html + PDFs)
├── notes/              Expository notes (index.html + PDFs)
├── teaching/           Courses & seminars (index.html + one folder per course)
├── problems/           Quick math problems
└── misc/               Games, lists, personal pages
```

## The rules (read before adding anything)

1. **Bundles.** Anything with more than one file gets its own folder
   containing an `index.html` plus its files. A course folder holds its
   page, syllabus, notes, and problem sets — everything about it, forever.
   Single-file items (a lone PDF, a self-contained game) stay flat inside
   their section.

2. **Chronological slugs.** Course folders are named `YYYY-term-slug`,
   e.g. `2024-fall-gaga-seminar`. The filesystem then sorts into a timeline.

3. **Link style.** Links *between* sections are root-relative
   (`/teaching/`, `/notes/foo.pdf`). Links *within* a bundle are bare
   filenames (`syllabus.pdf`). Never use `../`.

4. **URLs are forever.** Once something is committed and deployed, never
   rename or move it. If a move is truly unavoidable, add a 301 line to
   `_redirects` and never delete old redirect lines.

5. **Lowercase filenames.** The server is case-sensitive; macOS is not.
   Lowercase everything to keep the mismatch from ever mattering.

## Recipes

**Add a paper:** drop the PDF in `papers/` (lowercase, year-prefixed:
`2027-period-index-bounds.pdf`), add a list entry in `papers/index.html`.

**Add a course:** create `teaching/YYYY-term-slug/` with an `index.html`
(copy an existing course page as a template); put its PDFs in the same
folder, linked by bare filename; add one entry in `teaching/index.html`.

**Add a note:** PDF into `notes/`, entry in `notes/index.html`.

**Add a game/page:** single HTML file into `misc/`, entry in `misc/index.html`.

**New top-level section** (talks, students, software, ...): create a
sibling folder with an `index.html`, link it from the homepage list.
The schema doesn't change.
