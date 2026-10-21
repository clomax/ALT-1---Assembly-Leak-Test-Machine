# Project website

Static site for GitHub Pages: plain HTML and CSS, no build step.

```
docs/
├── index.html          single page, one <section> per topic
├── .nojekyll           serve files as-is (skip Jekyll)
└── assets/
    ├── css/style.css
    └── img/            screenshots, diagrams, GIFs
```

## Preview locally

```
python3 -m http.server -d docs 8000
```

Then open http://localhost:8000.

## Publish

On GitHub: **Settings → Pages → Build and deployment**, source **Deploy from a branch**,
branch `master`, folder `/docs`. The site appears at
https://clomax.github.io/ALT-1---Assembly-Leak-Test-Machine/.

Keep asset links relative (`assets/...`, not `/assets/...`), because the site is
served from a sub-path.

## To do

Search `index.html` for `TODO` to find the placeholders for content, screenshots and diagrams.
