"""Draft monthly variance commentary for FY2013 with a local LLM (Ollama), for a human to review.

    uv run python src/commentary.py          # needs a built data/warehouse/finance.duckdb and Ollama on :11434
    COMMENTARY_MODEL=qwen3:8b uv run python src/commentary.py

What goes to the model: only aggregated rows of mart.variance_cube (month x entity x P&L line totals), reduced
here to ~10 pre-computed, pre-formatted figures per month. No journal lines, no account detail, no Entity Access
emails. The model only words the figures. Each answer goes through a gate before it is written:
  1. number check: every $ amount must be one of the supplied figures; no percentages;
  2. attribution check: an entity/line amount must sit in a sentence that names that entity/line, and a sentence
     that talks about the overall/total/group result may only carry group-level figures.
A month that fails is regenerated up to 3 times with the failures fed back. If it still fails, the LLM text is
dropped and a deterministic template sentence built from the same figures is used instead (marked as such).
The result is still a DRAFT for a person to review, not a published text.
"""
import json
import os
import re
import urllib.request
from pathlib import Path

import duckdb

AMT = re.compile(r"\$[\d,.]*\d[MK]?")
OVERALL = re.compile(r"\b(overall|total|group|whole|combined|consolidated)\b", re.I)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "commentary_FY2013.md"
MODEL = os.environ.get("COMMENTARY_MODEL", "qwen3:8b")
RETRIES = 3
YEAR = 2013

con = duckdb.connect(str(ROOT / "data" / "warehouse" / "finance.duckdb"), read_only=True)
OPER = "from mart.variance_cube v join mart.dim_pnl_line l using (line_id) join mart.dim_entity e using (entity_key) " \
       "where l.is_operating and l.line_kind = 'detail'"


def money(v):
    a = abs(v)
    return f"${a / 1e6:,.2f}M" if a >= 1e6 else f"${a / 1e3:,.0f}K" if a >= 1e3 else f"${a:,.0f}"


def facts(month):
    """The figures for one month, already worded with their sign so the model never does arithmetic."""
    p = f"date '{YEAR}-{month:02d}-01'"
    act, bud, fx, opv = con.execute(f"select sum(actual), sum(budget), sum(fx_effect), sum(operational_variance) "
                                    f"{OPER} and v.period = {p}").fetchone()
    ytd = con.execute(f"select sum(actual) - sum(budget) {OPER} and year(v.period) = {YEAR} and v.period <= {p}").fetchone()[0]
    ent = con.execute(f"select e.entity, sum(operational_variance) o {OPER} and v.period = {p} "
                      f"group by 1 order by 2").fetchall()
    line = con.execute(f"select l.line, sum(operational_variance) o {OPER} and v.period = {p} "
                       f"group by 1 order by 2 limit 1").fetchone()
    side = lambda v: "ahead of" if v >= 0 else "behind"
    var = act - bud
    f = {
        "month": f"{['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][month - 1]} {YEAR}",
        "operating profit": money(act),
        "budget": money(bud),
        "variance": f"{money(var)} {side(var)} budget",
        "of which exchange rates": f"{money(fx)} {'favourable' if fx >= 0 else 'unfavourable'}",
        "of which operations (at budget rates)": f"{money(opv)} {'favourable' if opv >= 0 else 'unfavourable'}",
        "year to date": f"{money(ytd)} {side(ytd)} budget",
        "worst entity": f"{ent[0][0]} ({money(ent[0][1])} {'favourable' if ent[0][1] >= 0 else 'unfavourable'})",
        "second worst entity": f"{ent[1][0]} ({money(ent[1][1])} {'favourable' if ent[1][1] >= 0 else 'unfavourable'})",
        "best entity": f"{ent[-1][0]} ({money(ent[-1][1])} {'favourable' if ent[-1][1] >= 0 else 'unfavourable'})",
        "worst P&L line": f"{line[0]} ({money(line[1])} {'favourable' if line[1] >= 0 else 'unfavourable'})",
    }
    # who each amount belongs to: group-level figures vs one named entity / P&L line
    own = {}
    for k in ("operating profit", "budget", "variance", "of which exchange rates",
              "of which operations (at budget rates)", "year to date"):
        own.setdefault(AMT.search(f[k]).group(), set()).add("group")
    for name, v in (ent[0], ent[1], ent[-1], line):
        own.setdefault(money(v), set()).add(name)
    return f, own


def template(f):
    """Deterministic fallback, same figures, no model."""
    v, fx, op = f["variance"], f["of which exchange rates"], f["of which operations (at budget rates)"]
    return (f"{f['month']} operating profit was {f['operating profit']} against a budget of {f['budget']}, "
            f"{v}: {op} from operations at budget rates and {fx} from exchange rates. "
            f"The largest operational shortfall was {f['worst entity']}, and the weakest P&L line was {f['worst P&L line']}. "
            f"Year to date the group is {f['year to date']}.")


PROMPT = """You write the monthly variance commentary for a group finance pack.
Write 3 short sentences in plain English about {month}, using ONLY the figures below.
Copy every amount exactly as written (same digits, same $ and M/K). Do not add, compute or round any number.
Do not use bullet points or headings. Only attach an entity or P&L line amount to that entity or line, in the same
sentence. Do not call an entity or line figure the overall or total result.

{lines}{feedback}"""


def ask(prompt):
    body = {"model": MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.2, "num_ctx": 2048}}
    if MODEL.startswith("qwen3"):
        body["think"] = False
    req = urllib.request.Request("http://localhost:11434/api/generate", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)["response"].strip()


def problems(text, f, own):
    """Why a draft fails the gate (empty list = passes)."""
    given = set(own)
    bad = [f"{a} is not one of the figures" for a in AMT.findall(text) if a not in given]
    bad += [f"'{p}' is a percentage; use only the $ figures" for p in re.findall(r"\d[\d.]*\s?%", text)]
    for sent in re.split(r"(?<=[.!?])\s+", text):
        for a in AMT.findall(sent):
            owners = own.get(a, set())
            if not owners or "group" in owners:
                continue
            if OVERALL.search(sent):
                bad.append(f"{a} belongs to {'/'.join(sorted(owners))}, not to the overall result: \"{sent}\"")
            elif not any(o.lower() in sent.lower() for o in owners):
                bad.append(f"{a} belongs to {'/'.join(sorted(owners))}, but that sentence does not name it: \"{sent}\"")
    return bad


def main():
    out = [f"# Variance commentary FY{YEAR} — DRAFT, needs human review", "",
           f"Drafted by a local LLM (`{MODEL}` on Ollama) from aggregated `mart.variance_cube` figures; operating lines "
           "only, vs budget. Nobody has reviewed this text yet. Every month passed a gate before it was written here: a "
           "number check (each $ amount is one of the supplied figures, no percentages) and an attribution check (an "
           "entity or P&L line amount only next to that entity or line, and an overall/total sentence only with "
           f"group-level figures). A month that failed was regenerated up to {RETRIES} times with the failures fed back; "
           "if it still failed, the LLM text was dropped and replaced by a fixed template built from the same figures.", ""]
    stats = {"llm": 0, "retried": 0, "template": 0}
    for month in range(1, 13):
        f, own = facts(month)
        lines = "\n".join(f"- {k}: {v}" for k, v in f.items() if k != "month")
        feedback, log = "", []
        for attempt in range(RETRIES + 1):
            text = ask(PROMPT.format(month=f["month"], lines=lines, feedback=feedback))
            bad = problems(text, f, own)
            if not bad:
                break
            log.append(f"attempt {attempt + 1}: " + "; ".join(bad))
            feedback = ("\n\nYour previous answer was rejected:\n" + "\n".join(f"- {b}" for b in bad)
                        + "\nWrite it again and fix these.")
        if bad:
            text, source = template(f), "fixed template (LLM draft rejected after retries)"
            stats["template"] += 1
        else:
            source = f"{MODEL}" + (f", passed on attempt {len(log) + 1}" if log else ", passed first time")
            stats["retried" if log else "llm"] += 1
        final = problems(text, f, own)
        assert not final, (f["month"], final)
        out += [f"## {f['month']}", "", text, "",
                f"*Source: {source}. Number and attribution check: OK.*", ""]
        if log:
            out += ["<details><summary>Rejected drafts</summary>", "", *[f"- {l}" for l in log], "", "</details>", ""]
        out += ["<details><summary>Figures given to the model</summary>", "", lines, "", "</details>", ""]
        print(f"  ✓ {f['month']}  {source}")
    OUT.write_text("\n".join(out))
    print(f"  ✓ wrote {OUT.relative_to(ROOT)}: 12 of 12 months pass the gate "
          f"({stats['llm']} first try, {stats['retried']} after retry, {stats['template']} template)")


if __name__ == "__main__":
    main()
