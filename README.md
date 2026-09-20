# CineReco Fresh Reset v13 — Final Polish Build

CineReco is a local movie discovery app built from the supplied catalogue data.

## Launch
Double-click **Launch CineReco.vbs** for hidden startup (no Command Prompt window).
`Run CineReco.bat` and `Start CineReco.bat` also delegate to the hidden launcher.

First run creates `.venv` and installs **Flask only**. Later launches reuse the environment.

## v13 final polish
- Faster movie detail navigation: persisted similarity cache for the most popular poster-backed films.
- Faster live suggestions: prefix-indexed search instead of scanning the full catalogue on every keystroke.
- Cached popular/newest catalogue queries and long-lived browser cache headers for static assets.
- Movie cards prefetch their detail documents on hover/focus when the browser allows it.
- Navigation loader is now a non-blocking top progress line instead of a full-page overlay.
- Cleaner card/button hover and active states, focus states, reduced-motion support, and content-visibility for long sections.
- Horizontal movie/cast rails now have smart disabled arrows at the ends.
- Hero posters use high-priority image loading.
- Existing ratings, favorites, wishlist, watched, playlists, For You, activity tracking, filters, suggestions, and movie-detail cast showcase are preserved.
- No external movie API or API key is required.
- Port: 5083.
