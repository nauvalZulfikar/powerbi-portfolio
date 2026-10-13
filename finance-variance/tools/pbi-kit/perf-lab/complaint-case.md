# Case study template — "the report is slow"

1. **Complaint** (who, which page, how slow, since when): ____
2. **Reproduce**: Performance Analyzer on that page → slowest visual and which of DAX / display / other dominates. Evidence: ____
3. **Hypothesis**: (e.g. iterator with IF → CallbackDataID; high-cardinality column; too many visuals)
4. **Measure**: DAX Studio Server Timings cold + warm median (5×). Evidence: ____
5. **Fix**: one change at a time. Commit hash: ____
6. **Re-measure**: same query, same steps. Before → after: ____ ms → ____ ms (____ %)
7. **Guard**: add the query to `DAXQueries/` with an `.assert.json` (value must not change) so CI keeps the result correct; note the timing in `evidence/`.
8. **Reply to the complainant** in one paragraph: cause, fix, new time.
