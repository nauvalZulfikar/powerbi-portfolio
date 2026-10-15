"""reports/dax.html: the shared measure gallery (../tools/dax_gallery.py) plus three F1 notes on top.

    .venv/bin/python src/dax_notes.py

Notes: window functions, VAL vs EXPR parameters, and a problem → tool cheat sheet. Every example names a measure
or function that exists in powerbi/F1.SemanticModel (the gallery below the notes shows its DAX).
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "dax.html"

CHEAT = [  # (problem, tool, F1 example)
    ("Add a condition without wiping the filter already on that column", "KEEPFILTERS", "Front Row Wins, Podiums From Pole (KF)"),
    ("Condition on a row-by-row test, not a single column value", "FILTER inside CALCULATE", "Wins From Outside Top 10"),
    ("Share of a total that keeps only some filters", "ALLEXCEPT", "Share of Season Wins"),
    ("Share of what the visual shows", "ALLSELECTED", "% of Visible Wins"),
    ("Use a second relationship between the same tables", "USERELATIONSHIP on an inactive relationship", "Teammate Points Delta"),
    ("Filter a dimension by values from another dimension", "TREATAS (expanded table carries it to Results)", "Wins at Home Circuits"),
    ("Best / worst of many sub-totals", "MAXX over VALUES (context transition)", "Best Season Points"),
    ("Count groups that pass a test", "COUNTROWS ( FILTER ( VALUES (...), [measure] > 0 ) )", "Seasons With a Win"),
    ("Name of the top item", "TOPN over ADDCOLUMNS, or INDEX", "Top Team of Selection, Top Driver of Decade"),
    ("Previous row without a date table", "OFFSET", "Points Prev Round"),
    ("Running total / moving average over rounds", "WINDOW", "Running Points (Window), Points 3-Race Avg"),
    ("Rank inside the visual", "RANK DENSE (or a RANK visual calculation)", "Season Rank by Wins; Pace Rank on Pit Wall"),
    ("Year-to-date that restarts each season", "DATESYTD / TOTALYTD on a calendar", "Points STD, Points STD (Cal)"),
    ("Same period last season, YoY", "SAMEPERIODLASTYEAR, DATEADD, PREVIOUSYEAR", "Points PY, Points YoY %, Win Rate Prev Season"),
    ("Rolling average over years", "DATESINPERIOD", "DNF Rate 5Y Avg"),
    ("Closing balance instead of a sum", "LASTNONBLANKVALUE", "Points at Season End"),
    ("The same time logic for many measures", "Calculation group, or a UDF with an EXPR parameter", "Period group; F1.PrevSeason, F1.SeasonToDate"),
    ("One formula reused by many measures", "User-defined function", "F1.Rate in Win Rate, Podium Rate, DNF Rate"),
    ("A number shown in a custom shape", "Dynamic format string", "Avg Lap Time, Best Lap Time: 90.013 s → 1:30.013"),
    ("Let the reader swap the measure or the axis", "Field parameter", "Dynasty Metric, Legends By"),
]

NOTES = f"""
<section><h2>Notes · window functions</h2><div class="cols" style="column-count:1">
<article class="card"><p class="desc">OFFSET, WINDOW, INDEX and RANK work on a <b>relation</b>: a table of rows put in order.
Three arguments shape it: the relation itself (here <code>ALLSELECTED ( Race[Round] )</code>, the rounds the visual can see),
<code>ORDERBY</code> (round number), and an optional <code>PARTITIONBY</code> that restarts the order inside each group.
The F1 measures need no PARTITIONBY because the season already comes from the filter context: one season's rounds at a time.</p>
<pre><code>OFFSET ( -1, relation, ORDERBY ( Race[Round] ) )            -- the row before the current one   → Points Prev Round
WINDOW ( 1, ABS, 0, REL, relation, ORDERBY ( Race[Round] ) ) -- first row up to the current one  → Running Points (Window)
WINDOW ( -2, REL, 0, REL, relation, ORDERBY ( Race[Round] ) ) -- current and two before          → Points 3-Race Avg
INDEX ( 1, ALL ( Driver[Driver Name] ), ORDERBY ( [Wins], DESC ) ) -- the first row               → Top Driver of Decade
RANK ( DENSE, ALLSELECTED ( Driver[Driver Name] ), ORDERBY ( [Wins], DESC ) ) -- position          → Season Rank by Wins</code></pre>
<p class="desc">ABS counts from the start of the relation, REL from the current row. Visual calculations (RUNNINGSUM,
MOVINGAVERAGE, RANK on the Season Lens, Grid and Pit Wall pages) do the same on the visual's own rows, without a model measure.</p></article>
</div></section>
<section><h2>Notes · VAL vs EXPR parameters</h2><div class="cols" style="column-count:1">
<article class="card"><p class="desc">A <b>VAL</b> parameter is computed once, where the function is called, and arrives as a number.
An <b>EXPR</b> parameter arrives as the expression and is computed again inside the function, in whatever filter context the
function sets up.</p>
<pre><code>F1.Share ( value : SCALAR NUMERIC VAL, total : SCALAR NUMERIC VAL ) => DIVIDE ( value, total )
F1.PrevSeason ( value : SCALAR NUMERIC EXPR ) => CALCULATE ( value, DATEADD ( 'Calendar'[Date], -1, YEAR ), REMOVEFILTERS ( Season ) )</code></pre>
<p class="desc">F1.Share only divides two finished numbers, so VAL is right: nothing would change if they were re-computed.
F1.PrevSeason must be EXPR: with VAL, <code>[Wins]</code> would arrive already computed for the current season and the
DATEADD inside would have nothing left to shift, so 'Wins Prev Season' would equal 'Wins'. F1.SeasonToDate takes a typed
CALENDARREF as well, so one function serves Wins STD and Podiums STD on the 'F1 Calendar'.</p></article>
</div></section>
<section><h2>Notes · problem → tool</h2><div class="cols" style="column-count:1"><article class="card">
<table style="width:100%;border-collapse:collapse;font-size:13px">
<tr><th style="text-align:left">Problem</th><th style="text-align:left">Tool</th><th style="text-align:left">F1 measure</th></tr>
{''.join(f'<tr><td style="padding:4px 8px 4px 0;border-top:1px solid #e4e2dc">{p}</td><td style="padding:4px 8px;border-top:1px solid #e4e2dc"><code>{t}</code></td><td style="padding:4px 0;border-top:1px solid #e4e2dc">{e}</td></tr>' for p, t, e in CHEAT)}
</table></article></div></section>
"""


def main():
    subprocess.run([sys.executable, str(ROOT.parent / "tools" / "dax_gallery.py"), str(ROOT)], check=True)
    page = OUT.read_text()
    assert page.count("</main>") == 1
    head, tail = page.split("</main>")
    first = head.index("<section>")  # notes go right under the title, above the gallery
    OUT.write_text(head[:first] + NOTES + head[first:] + "</main>" + tail)
    print(f"  ✓ added 3 notes to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
