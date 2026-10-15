"""Python mirror of the Power Query glossary steps (Dictionary Raw -> Field Glossary, Model Importance merge).

Power BI runs the M in build_model.py; this file repeats the same steps step-for-step so the pipeline can
test them (every PD-model feature must find an official LendingClub description) and the HTML twin can
show the glossary. Reads the .xlsx with the standard library only (no openpyxl).
"""
import html
import re
import zipfile
from pathlib import Path

SHEETS = ("LoanStats", "browseNotes")
SHEET_OF_HEADER = {"LoanStatNew": "LoanStats", "BrowseNotesFile": "browseNotes"}
# Model features renamed in sql/10_staging.sql -> the LendingClub field they come from (M: LcFieldOf record)
LC_FIELD_OF = {"annual_income": "annual_inc", "loan_amount": "loan_amnt", "emp_years": "emp_length",
               "credit_history_years": "earliest_cr_line", "term_months": "term"}
# LCDataDictionary.xlsx has the two secondary-applicant FICO descriptions swapped (low says "high" and vice versa)
SWAPPED = {"sec_app_fico_range_low": ("(high)", "(low)"), "sec_app_fico_range_high": ("(low)", "(high)")}


def _sheets(path):
    z = zipfile.ZipFile(path)
    strings = [html.unescape(re.sub(r"<[^>]+>", "", s)) for s in re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode(), re.S)]
    rels_xml = z.read("xl/_rels/workbook.xml.rels").decode()
    rels = {re.search(r'Id="(rId\d+)"', r).group(1): re.search(r'Target="([^"]+)"', r).group(1)
            for r in re.findall(r"<Relationship [^>]*>", rels_xml)}
    out = {}
    for name, rid in re.findall(r'<sheet name="([^"]+)"[^>]*r:id="(rId\d+)"', z.read("xl/workbook.xml").decode()):
        xml = z.read("xl/" + rels[rid].lstrip("/").removeprefix("xl/")).decode()
        rows = []
        for row in re.findall(r"<row [^>]*>(.*?)</row>", xml, re.S):
            cells = {re.match(r"[A-Z]+", ref).group(): strings[int(v)] if 't="s"' in attrs else v
                     for ref, attrs, v in re.findall(r'<c r="([A-Z]+\d+)"([^>]*)>(?:<f>.*?</f>)?<v>(.*?)</v>', row)}
            rows.append((cells.get("A"), cells.get("B")))
        out[name] = rows
    return out


def clean(v):
    return None if v is None else re.sub(r"[\x00-\x1f]", "", str(v).replace("\xa0", " ").strip())


def field_glossary(path):
    """Same order of steps as the M partition of 'Field Glossary'."""
    sheets = _sheets(path)
    appended = [r for s in SHEETS for r in sheets[s]]                       # Appended Sheets (Table.Combine)
    while appended and appended[0] == (None, None):                         # Skipped Blank Leading Rows
        appended.pop(0)
    header, body = appended[0], appended[1:]                                # Promoted Headers
    sheet, marked = header[0], []
    for code, desc in body:                                                 # Marked Sheet Headers + Filled Down Sheet
        if desc == "Description":
            sheet = code
        marked.append((SHEET_OF_HEADER.get(sheet, sheet), code, desc))      # Renamed Sheets (ReplaceValue)
    rows = [(s, c, d) for s, c, d in marked if d != "Description"]          # Removed Sheet Header Rows
    rows = [(s, c, d) for s, c, d in rows if c is not None and str(c).strip()]  # Removed Footnotes
    out, seen = [], set()
    for s, c, d in rows:                                                    # Cleaned Text (try … otherwise null)
        c, d = clean(c).lower(), clean(d)
        if c in SWAPPED and d:                                              # Fixed Swapped FICO Labels
            d = d.replace(*SWAPPED[c])
        if c in seen:                                                       # Removed Duplicate Codes
            continue
        seen.add(c)
        out.append({"Sheet": s, "Field Code": c, "Description": d,
                    "Field Label": " ".join(w.capitalize() for w in c.replace("_", " ").split(" ")),
                    "Field Order": len(out) + 1})
    return out


def feature_descriptions(path, features):
    """Model Importance merge (left outer on the LendingClub field code)."""
    by_code = {r["Field Code"]: r["Description"] for r in field_glossary(path)}
    return {f: by_code.get(LC_FIELD_OF.get(f, f)) for f in features}
