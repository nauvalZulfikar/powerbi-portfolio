# DAX Studio checklist

Install: https://daxstudio.org (Windows). Launch from Power BI Desktop → External Tools → DAX Studio (connects to the open model).

## A. Server Timings (one query)
1. [ ] Paste the query (from Performance Analyzer or `DAXQueries/*.dax`).
2. [ ] Ribbon: **Server Timings** ON, **Query Plan** ON.
3. [ ] **Cold**: Run → "Clear Cache then Run". Note Total, FE (formula engine), SE (storage engine), SE queries, SE cache hits. → `evidence/<m>-s<n>-cold.txt`
4. [ ] **Warm**: run 5× without clearing. Take the **median** Total/FE/SE. (Optional: Run → "Benchmark" does cold+warm × N automatically and exports.)
5. [ ] Server Timings tab: read each **xmSQL** storage-engine query:
   - many small SE queries for one measure → iterating row by row (CALLBACK / context transition)
   - `CallbackDataID` in xmSQL → the SE called back to FE (IF/SWITCH/DIVIDE inside an iterator): candidate fix
   - large "Rows" / "KB" → materialisation; check filters, use KEEPFILTERS/TREATAS
   Copy the worst xmSQL → `evidence/<m>-s<n>-xmsql.txt`
6. [ ] **Query Plan** tab: physical plan row counts; spool with huge records = materialisation. → `evidence/<m>-s<n>-plan.txt`

## B. All Queries (whole page)
7. [ ] Ribbon **All Queries** ON → in Desktop, refresh the page → DAX Studio captures every visual's query with duration. Sort by duration; export. → `evidence/<m>-s<n>-allqueries.txt`

## C. EVALUATEANDLOG (debugging intermediate tables)
8. [ ] Wrap a sub-expression: `EVALUATEANDLOG ( <table expr>, "label" )`, run in Desktop with DAX Studio **Trace → DAX Evaluation Log** ON. Shows what the measure saw. Remove before committing (it's for debug only).

## D. VertiPaq Analyzer (model size)
9. [ ] Advanced → **View Metrics**. Note: total size, largest tables/columns, cardinality, **Encoding** (HASH vs VALUE), **Segments**/partitions, relationship sizes and missing keys (RI violations).
10. [ ] Advanced → **Export Metrics** → `evidence/<m>-s<n>.vpax` (re-openable in DAX Studio / Bravo / Tabular Editor 3).
11. [ ] Fix candidates: high-cardinality columns not used (remove), datetime → split date/time, float → fixed decimal, text keys → integer keys, auto date/time tables (disable).

## E. Re-measure
12. [ ] After the fix: repeat A3–A4 on the **same** query. Fill the before/after table in `results-template.md`.
