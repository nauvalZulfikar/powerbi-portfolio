# Performance Analyzer checklist (Power BI Desktop)

Project: ____  Page: ____  Desktop version: ____  Date: ____

1. [ ] Close other reports. Open the .pbip. View → **Performance analyzer** → **Start recording**.
2. [ ] Switch to a blank page, then back to the page under test (forces every visual to re-query). Or **Refresh visuals**.
3. [ ] For each visual read the three numbers:
   - **DAX query** — engine time (model/measure problem if large)
   - **Visual display** — rendering (too many points/visual type problem)
   - **Other** — waiting on other visuals / queueing (too many visuals on the page)
   Screenshot the pane → `evidence/<m>-s1.png`.
4. [ ] **Export** → JSON → `evidence/<m>-s2.json`.
5. [ ] Single visual: select the slowest visual, Clear, **Refresh visuals** with only it changed (or copy it to an empty page) so timings are not polluted by its neighbours. → `evidence/<m>-s3.png`
6. [ ] Expand the slow visual → **Copy query** → paste into **DAX query view** (or DAX Studio). Save as `<model>/DAXQueries/<m>-slow-visual.dax`.
7. [ ] In DAX query view, run it, then use **Optimize** ribbon (if offered in your Desktop build: "Optimize → Pause visuals / Refresh") to compare. Note results.
8. [ ] Record numbers in `results-template.md`. If DAX query time dominates → go to `dax-studio.md`.
