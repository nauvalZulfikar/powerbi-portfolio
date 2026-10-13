# Perf results — <project> / method <m> — <date>

Desktop version: ____  DAX Studio version: ____  Model size (vpax): ____ MB  Machine: ____

## Query under test
- Visual / page: ____
- File: `DAXQueries/____.dax`

## Timings (ms)
| Run | Total | FE | SE | SE queries | SE cache hits | Notes |
|---|---:|---:|---:|---:|---:|---|
| Before — cold | | | | | | |
| Before — warm (median of 5) | | | | | | |
| After — cold | | | | | | |
| After — warm (median of 5) | | | | | | |

Performance Analyzer (slowest visual): DAX query ____ ms / Visual display ____ ms / Other ____ ms

## What the evidence showed
- xmSQL: (CallbackDataID? materialisation? number of SE queries)
- Query plan: (largest spool / record count)
- VertiPaq: (largest column, encoding, cardinality)

## Change made
- (measure rewrite / column removed / data type / relationship …) — commit: ____

## Evidence files
- `evidence/<m>-s1.png` …
