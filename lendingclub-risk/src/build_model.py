"""Generate the Power BI semantic model (TMDL) from the DuckDB mart schema.

    uv run python src/build_model.py [--data-dir PATH]

Column types are read from DuckDB, so the model never drifts from the SQL.
--data-dir is the folder Power BI reads the Parquet marts from (default: this
repo's data/marts). On Windows, pass e.g. --data-dir "C:\\lendingclub-risk\\data\\marts"
or change it later in Power BI: Transform data > Edit parameters > DataFolder.
"""
import argparse
import json
import re
import shutil
import uuid
from pathlib import Path

import duckdb

from glossary import LC_FIELD_OF

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "powerbi" / "LendingClub.SemanticModel"
FABRIC_OUT = ROOT / "powerbi" / "LendingClub.Fabric.SemanticModel"
T = "\t"

TYPES = {  # duckdb -> (TMDL dataType, Power Query type)
    "BIGINT": ("int64", "Int64.Type"), "INTEGER": ("int64", "Int64.Type"),
    "DOUBLE": ("double", "type number"), "DATE": ("dateTime", "type date"),
    "BOOLEAN": ("boolean", "type logical"), "VARCHAR": ("string", "type text"),
}
USD0, USD2, PCT1, PCT2, NUM0, NUM1, NUM3 = "\\$#,0", "\\$#,0.00", "#,0.0%;-#,0.0%;#,0.0%", "0.00%", "#,0", "#,0.0", "0.000"

# Power BI table -> (mart table, config)
TABLES = {
    "Date": ("dim_date", dict(
        key="date", date_table=True,
        sort={"month_name": "month", "month_year": "month_year_sort", "year_quarter": "year_quarter_sort"},
        hidden=["month", "month_year_sort", "year_quarter_sort"], fmt={"date": "dd mmm yyyy", "month_start": "mmm yyyy"})),
    "Grade": ("dim_grade", dict(
        key="sub_grade", sort={"grade": "grade_sort", "sub_grade": "sub_grade_sort", "risk_tier": "risk_tier_sort"},
        hidden=["grade_sort", "sub_grade_sort", "risk_tier_sort"],
        # Calculated columns (3-s11): a row of Grade becomes a filter on Loans (context transition / RELATEDTABLE).
        calc_cols=[("Sub Grade Avg Rate", "double", PCT2, "CALCULATE ( AVERAGE ( Loans[Interest Rate] ) )", []),
                   ("Sub Grade Loans", "int64", NUM0, "COUNTROWS ( RELATEDTABLE ( Loans ) )", [])])),
    "State": ("dim_state", dict(
        key="state", category={"state_name": "StateOrProvince", "country": "Country"},
        hidden=["tile_col", "tile_row"], names={"state": "State Code"})),
    "Cash Bridge": ("dim_cash_bridge", dict(key="step", sort={"step": "step_sort"}, hidden=["step_sort"])),
    "Purpose": ("dim_purpose", dict(key="purpose", hidden=["purpose"], names={"purpose": "Purpose Code"},
        # Power Query (s8): the display label is rebuilt from the code with Replace Values (credit_card -> Credit card)
        drop=["purpose_label"],
        steps=[('#"Duplicated Purpose Code"', 'Table.DuplicateColumn({prev}, "purpose", "purpose_label")'),
               ('#"Replaced Underscores"', 'Table.ReplaceValue({prev}, "_", " ", Replacer.ReplaceText, {"purpose_label"})'),
               ('#"Capitalized First Letter"', 'Table.TransformColumns({prev}, {{"purpose_label", each Text.Upper(Text.Start(_, 1)) & Text.Middle(_, 1), type text}})')],
        extra_cols=[("Purpose", "purpose_label", "string", [])],
        # Data group (16-s4) built on Purpose, the way Desktop stores "New group": a SWITCH calculated column
        # with GroupingMetadata. It replaces the mart's purpose_group column in the Import model.
        calc_replaces=["purpose_group"],
        calc_cols=[("Purpose Group", "string", None,
                    "SWITCH (\n    TRUE (),\n    ISBLANK ( Purpose[Purpose] ), \"(Blank)\",\n"
                    "    Purpose[Purpose] IN { \"Debt consolidation\", \"Credit card\" }, \"Refinancing\",\n"
                    "    Purpose[Purpose] IN { \"Home improvement\", \"House\", \"Moving\", \"Renewable energy\" }, \"Home\",\n"
                    "    Purpose[Purpose] IN { \"Small business\", \"Educational\" }, \"Business & education\",\n"
                    "    Purpose[Purpose] IN { \"Car\", \"Major purchase\", \"Vacation\", \"Wedding\", \"Medical\" }, \"Personal spending\",\n"
                    "    \"Other\"\n)",
                    ["", "extendedProperty GroupingMetadata =", "\t\t{", '\t\t  "version": 0,',
                     '\t\t  "groupedColumns": [{"Column": {"Expression": {"SourceRef": {"Entity": "Purpose"}}, "Property": "Purpose"}}]',
                     "\t\t}"])])),
    "Loans": ("fact_loans", dict(
        sort={"outcome": "outcome_sort", "income_band": "income_band_sort", "dti_band": "dti_band_sort",
              "emp_length": "emp_length_sort", "revol_util_band": "revol_util_band_sort"},
        hidden=["issue_date", "last_payment_date", "sub_grade", "state", "purpose", "outcome_sort", "income_band_sort", "dti_band_sort",
                "emp_length_sort", "revol_util_band_sort"],
        fmt={"int_rate": PCT2, "revol_util": PCT1, "pd": PCT2, "dti": NUM1, "credit_history_years": NUM1},
        names={"pd": "PD", "dti": "DTI", "dti_band": "DTI Band", "int_rate": "Interest Rate",
               "revol_util": "Revolving Utilisation", "revol_util_band": "Revolving Utilisation Band",
               "pd_band": "PD Band", "in_model_test_set": "In Model Test Set", "emp_length": "Employment Length",
               "funded_amount": "Funded Amt", "outstanding_principal": "Outstanding Principal Amt",
               "net_recoveries": "Net Recoveries Amt", "net_profit": "Net Profit Amt", "expected_loss": "Expected Loss Amt",
               "exposure_at_default": "EAD Amt", "net_loss": "Net Loss Amt", "interest_paid": "Interest Paid Amt",
               "late_fees": "Late Fees Amt", "loan_id": "Loan ID"},
        # 16-s12: the ID is unique per row -> value encoding; amount columns are measure inputs, so Excel/MDX
        # clients don't need them as hierarchies.
        col_extra={"loan_id": ["encodingHint: Value", "isAvailableInMdx: false"],
                   **{c: ["isAvailableInMdx: false"] for c in ("principal_paid", "interest_paid", "late_fees", "net_recoveries",
                                                               "exposure_at_default", "net_loss", "net_profit", "outstanding_principal",
                                                               "expected_loss", "loan_amount")}},
        # Incremental refresh (11-s10/11): monthly tapes land by issue date; keep 12 years, refresh the last 3 months.
        refresh=dict(column="issue_date", rolling=("year", 12), incremental=("month", 3)),
        calc_cols=[("Interest Rate Band", "string", None,
                    "VAR _b = INT ( Loans[Interest Rate] * 20 ) * 5\nRETURN FORMAT ( _b, \"0\" ) & \"–\" & FORMAT ( _b + 5, \"0\" ) & \"%\"",
                    ["sortByColumn: 'Interest Rate Band Sort'"]),
                   ("Interest Rate Band Sort", "int64", "0", "INT ( Loans[Interest Rate] * 20 )", ["isHidden"])],
        sum=["loan_amount", "funded_amount", "installment", "total_paid", "principal_paid", "interest_paid",
             "net_recoveries", "exposure_at_default", "net_loss", "net_profit", "outstanding_principal",
             "expected_loss", "is_default", "late_fees"],
        # Power Query (s6/s7): keep only the columns the report uses, keep funded loans, split Term into months.
        drop=["status", "outside_policy", "had_hardship", "had_settlement", "total_paid", "installment", "emp_years",
              "loan_amount", "credit_history_years", "revol_util", "in_model_test_set"],  # last four: flagged by build_report unused_loans_columns()
        row_filter="each [funded_amount] > 0",
        steps=[('#"Duplicated Term"', 'Table.DuplicateColumn({prev}, "term", "term_split")'),
               ('#"Split Term"', 'Table.SplitColumn({prev}, "term_split", Splitter.SplitTextByDelimiter(" ", QuoteStyle.None), '
                                 '{"term_months", "term_unit"})'),
               ('#"Typed Term Months"', 'Table.TransformColumnTypes({prev}, {{"term_months", Int64.Type}})'),
               ('#"Removed Term Unit"', 'Table.RemoveColumns({prev}, {"term_unit"})')],
        extra_cols=[("Term Months", "term_months", "int64", ["formatString: 0"])])),
    "Vintage Curve": ("vintage_curve", dict(
        sort={"vintage": "vintage_sort"}, hidden=["vintage_sort"],
        fmt={"cum_default_rate": PCT2, "cum_default_rate_dollar": PCT2, "funded": USD0, "defaulted_funded": USD0},
        names={"mob": "Months on Book", "grade": "Vintage Grade", "loans": "Vintage Loans", "defaults": "Vintage Defaults",
               "funded": "Vintage Funded", "defaulted_funded": "Vintage Defaulted Funded"})),
    "Grade LGD": ("grade_lgd", dict(
        key="grade",
        fmt={"lgd": PCT1, "exposure_at_default": USD0, "net_recoveries": USD0, "avg_months_to_default": NUM1},
        names={"grade": "LGD Grade", "lgd": "Grade LGD Rate", "exposure_at_default": "Grade EAD",
               "net_recoveries": "Grade Net Recoveries", "defaulted_loans": "Grade Defaulted Loans",
               "avg_months_to_default": "Grade Months to Default"})),
    # Semi-additive stock (6-s14): estimated outstanding principal per month end (sql/30_analytics.sql).
    "Book Balance": ("book_balance", dict(
        hidden=["month_end", "sub_grade"], fmt={"est_outstanding": USD0, "active_loans": NUM0},
        names={"month_end": "Book Month End", "sub_grade": "Book Sub Grade", "est_outstanding": "Est Outstanding",
               "active_loans": "Book Active Loans"})),
    "Model Calibration": ("model_calibration", dict(
        sort={"decile_label": "decile"}, hidden=["decile"],
        fmt={"avg_pd": PCT1, "default_rate": PCT1, "cum_default_capture": PCT1, "cum_population": PCT1, "lift": "0.00x"},
        names={"loans": "Decile Loans", "defaults": "Decile Defaults", "avg_pd": "Predicted PD",
               "default_rate": "Observed Default Rate", "decile_label": "Risk Decile"})),
    "Model ROC": ("model_roc", dict(
        fmt={"fpr": PCT1, "tpr": PCT1, "random": PCT1},
        names={"fpr": "False Positive Rate", "tpr": "True Positive Rate", "random": "Random Classifier"})),
    "Model Importance": ("model_importance", dict(
        hidden=["feature"], fmt={"auc_drop": "0.0000"}, names={"feature": "Feature Code", "auc_drop": "AUC Drop", "feature_label": "Feature"},
        # Merge (s10): each feature gets the official LendingClub description from Field Glossary (left outer join).
        steps=[("LcFieldOf", "[" + ", ".join(f'{k} = "{v}"' for k, v in LC_FIELD_OF.items()) + "]"),
               ('#"Added LC Field"', 'Table.AddColumn({prev}, "lc_field", each Record.FieldOrDefault(LcFieldOf, [feature], [feature]), type text)'),
               ('#"Merged Field Glossary"', 'Table.NestedJoin({prev}, {"lc_field"}, #"Field Glossary", {"Field Code"}, "Glossary", JoinKind.LeftOuter)'),
               ('#"Expanded Description"', 'Table.ExpandTableColumn({prev}, "Glossary", {"Description"}, {"feature_description"})')],
        extra_cols=[("LC Field", "lc_field", "string", ["isHidden"]),
                    ("Feature Description", "feature_description", "string", [])])),
    "Model Metrics": ("model_metrics", dict(
        fmt={"auc": NUM3, "gini": NUM3, "ks": NUM3, "test_default_rate": PCT1},
        names={"auc": "AUC Value", "gini": "Gini Value", "ks": "KS Value"})),
}

RELATIONSHIPS = [  # (from table, from col, to table, to col, active, cardinality)
    ("Loans", "issue_date", "Date", "date", True, None),
    # Role-playing Date (2-s8/2-s9): inactive, switched on per measure with USERELATIONSHIP ('Loans Closed').
    ("Loans", "last_payment_date", "Date", "date", False, None),
    ("Loans", "sub_grade", "Grade", "sub_grade", True, None),
    ("Loans", "state", "State", "state", True, None),
    ("Loans", "purpose", "Purpose", "purpose", True, None),
    # Snowflake (2-s5): Loans -> Grade -> 'Grade LGD', so RELATED reaches the LGD from a loan row.
    ("Grade", "grade", "Grade LGD", "grade", True, None),
    # Many-to-many (11-s3): a curve row per vintage x grade, many sub-grades per grade. Single direction
    # Grade -> Vintage Curve, so the Risk tier slicer now filters the vintage curves.
    ("Vintage Curve", "grade", "Grade", "grade", True, "m2m"),
    ("Book Balance", "month_end", "Date", "date", True, None),
    ("Book Balance", "sub_grade", "Grade", "sub_grade", True, None),
]

# ---------------------------------------------------------------- Power Query
# Staging queries: TMDL `expression` = a query with "Enable load" off. The loaded tables Reference them
# (one copy of the steps, not Duplicate). Every step has a descriptive name so Applied Steps reads as a recipe.
STAGING = {
    # The official LendingClub data dictionary, both field sheets stacked. Each sheet begins with its own header
    # row ("LoanStatNew | Description", "BrowseNotesFile | Description"); Field Glossary turns those into a Sheet column.
    "Dictionary Raw": [
        '    Source = Excel.Workbook(File.Contents(DataFolder & "LCDataDictionary.xlsx"), null, true),',
        '    #"Kept Dictionary Sheets" = Table.SelectRows(Source, each [Kind] = "Sheet" and List.Contains({"LoanStats", "browseNotes"}, [Item])),',
        '    #"Appended Sheets" = Table.Combine(#"Kept Dictionary Sheets"[Data]),',
        '    #"Kept Two Columns" = Table.SelectColumns(#"Appended Sheets", {"Column1", "Column2"}),',
        '    #"Skipped Blank Leading Rows" = Table.Skip(#"Kept Two Columns", each [Column1] = null and [Column2] = null),',
        '    #"Promoted Headers" = Table.PromoteHeaders(#"Skipped Blank Leading Rows", [PromoteAllScalars = true]),',
        '    #"Renamed Columns" = Table.RenameColumns(#"Promoted Headers", {{"LoanStatNew", "Field Code"}})',
        "in", '    #"Renamed Columns"'],
}
NBSP = 'Text.Replace(Text.From(_), "#(00A0)", " ")'
# Loaded M tables: name -> ([(column, sourceColumn, dataType, extra lines)], M steps after `let`)
M_TABLES = {
    "Field Glossary": ([
        ("Field Order", "Field Order", "int64", ["formatString: 0", "isHidden"]),
        ("Sheet", "Sheet", "string", []),
        ("Field Code", "Field Code", "string", ["isKey"]),
        ("Field Label", "Field Label", "string", ["sortByColumn: 'Field Order'"]),
        ("Description", "Description", "string", [])], [
        '    Source = #"Dictionary Raw",',
        '    #"Marked Sheet Headers" = Table.AddColumn(Source, "Sheet", each if [Description] = "Description" then [Field Code] else null, type nullable text),',
        '    #"Filled Down Sheet" = Table.FillDown(#"Marked Sheet Headers", {"Sheet"}),',
        '    // rows above the second header belong to the first sheet, whose header became the column names',
        '    #"Replaced First Sheet" = Table.ReplaceValue(#"Filled Down Sheet", null, "LoanStatNew", Replacer.ReplaceValue, {"Sheet"}),',
        '    #"Renamed LoanStats" = Table.ReplaceValue(#"Replaced First Sheet", "LoanStatNew", "LoanStats", Replacer.ReplaceValue, {"Sheet"}),',
        '    #"Renamed browseNotes" = Table.ReplaceValue(#"Renamed LoanStats", "BrowseNotesFile", "browseNotes", Replacer.ReplaceValue, {"Sheet"}),',
        '    #"Removed Sheet Header Rows" = Table.SelectRows(#"Renamed browseNotes", each [Description] <> "Description"),',
        '    #"Removed Footnotes" = Table.SelectRows(#"Removed Sheet Header Rows", each [Field Code] <> null and Text.Trim(Text.From([Field Code])) <> ""),',
        f'    #"Cleaned Text" = Table.TransformColumns(#"Removed Footnotes", {{{{"Field Code", each Text.Lower(Text.Trim({NBSP})), type text}},',
        f'        {{"Description", each try Text.Clean(Text.Trim({NBSP})) otherwise null, type nullable text}}}}),',
        '    // LCDataDictionary.xlsx swaps the two secondary-applicant FICO descriptions',
        '    #"Fixed Swapped FICO Labels" = Table.ReplaceValue(#"Cleaned Text", each [Description], each',
        '        if [Field Code] = "sec_app_fico_range_low" then Text.Replace([Description], "(high)", "(low)")',
        '        else if [Field Code] = "sec_app_fico_range_high" then Text.Replace([Description], "(low)", "(high)")',
        '        else [Description], Replacer.ReplaceValue, {"Description"}),',
        '    #"Removed Duplicate Codes" = Table.Distinct(#"Fixed Swapped FICO Labels", {"Field Code"}),',
        '    #"Added Field Label" = Table.AddColumn(#"Removed Duplicate Codes", "Field Label", each Text.Proper(Text.Replace([Field Code], "_", " ")), type text),',
        '    #"Added Field Order" = Table.AddIndexColumn(#"Added Field Label", "Field Order", 1, 1, Int64.Type),',
        '    #"Reordered Columns" = Table.SelectColumns(#"Added Field Order", {"Field Order", "Sheet", "Field Code", "Field Label", "Description"})',
        "in", '    #"Reordered Columns"']),
    # AUC / Gini / KS as rows (unpivot), so one card visual can show them as small multiples.
    "Model Metrics Long": ([
        ("Metric", "Metric", "string", ["sortByColumn: 'Metric Order'"]),
        ("Metric Score", "Value", "double", [f"formatString: {NUM3}"]),
        ("Metric Order", "Metric Order", "int64", ["formatString: 0", "isHidden"])], [
        '    Source = Parquet.Document(File.Contents(DataFolder & "model_metrics.parquet")),',
        '    #"Kept Metric Columns" = Table.SelectColumns(Source, {"auc", "gini", "ks"}),',
        '    #"Renamed Metrics" = Table.RenameColumns(#"Kept Metric Columns", {{"auc", "AUC"}, {"gini", "Gini"}, {"ks", "KS"}}),',
        '    #"Unpivoted Metrics" = Table.Unpivot(#"Renamed Metrics", {"AUC", "Gini", "KS"}, "Metric", "Value"),',
        '    #"Added Metric Order" = Table.AddIndexColumn(#"Unpivoted Metrics", "Metric Order", 1, 1, Int64.Type),',
        '    #"Changed Type" = Table.TransformColumnTypes(#"Added Metric Order", {{"Metric", type text}, {"Value", type number}})',
        "in", '    #"Changed Type"']),
    # Group By: one row per vintage × grade with the last (lifetime-so-far) point of each curve.
    "Vintage Summary": ([
        ("Summary Vintage", "vintage", "string", ["sortByColumn: 'Summary Vintage Sort'"]),
        ("Summary Vintage Sort", "vintage_sort", "int64", ["formatString: 0", "isHidden"]),
        ("Summary Grade", "grade", "string", []),
        ("Lifetime Cum Default Rate", "lifetime_cum_default", "double", [f"formatString: {PCT1}"]),
        ("Months Observed", "months_observed", "int64", ["formatString: 0"]),
        ("Cohort Loans", "cohort_loans", "int64", ["formatString: #,0"])], [
        "    Source = #\"Vintage Curve\",",
        '    #"Grouped Rows" = Table.Group(Source, {"vintage", "vintage_sort", "grade"}, {',
        '        {"lifetime_cum_default", each List.Max([cum_default_rate]), type number},',
        '        {"months_observed", each List.Max([mob]), Int64.Type},',
        '        {"cohort_loans", each List.Max([loans]), Int64.Type}})',
        "in", '    #"Grouped Rows"']),
}

# ---------------------------------------------------------------- model objects beyond tables
HIERARCHIES = {
    "Date": [("Calendar", ["Year", "Year Quarter", "Month Year"])],
    "Grade": [("Credit Grade", ["Risk Tier", "Grade", "Sub Grade"])],
    "State": [("Geo", ["Region", "Division", "State Name"])],
}

# Calendar-based time intelligence (6-s16, 9-s10): (calendar, [(time unit, primary column, associated column)]).
# The primary column must be unique per period (Year Quarter Sort = 20151, Month Year Sort = 201501); the
# associated label column is what visuals show. A filter on Month Year is read by the engine as a month filter,
# so DATEADD ( 'Gregorian', -1, YEAR ) shifts it to the same month a year back instead of clearing it.
CALENDARS = [
    ("Gregorian", [("year", "Year", None), ("quarter", "Year Quarter Sort", "Year Quarter"),
                   ("month", "Month Year Sort", "Month Year"), ("date", "Date", None)]),
]

# DAX user-defined functions (9-s11..9-s14): (name, description, signature => body). One file, LC. prefix.
FUNCTIONS = [
    ("LC.MaturedOnly",
     "Evaluates a measure on matured, resolved loans only: the methodology rule behind every lifetime rate, in one place. "
     "EXPR: the measure is evaluated inside the CALCULATE, after the filter is applied.",
     "( m : SCALAR NUMERIC EXPR ) =>\n\tCALCULATE ( m, Loans[Is Resolved] = TRUE (), Loans[Is Matured] = TRUE () )"),
    ("LC.SafeRate",
     "Part ÷ whole, blank when the whole is empty. VAL: both numbers are computed once, in the caller's filter context.",
     "( num : SCALAR NUMERIC VAL, den : SCALAR NUMERIC VAL ) => DIVIDE ( num, den )"),
    ("LC.VsPortfolio",
     "A measure minus the same measure over everything selected on the page (percentage points for rates). "
     "MEASUREREF: the measure itself is passed, so it can be re-evaluated under ALLSELECTED.",
     "( m : MEASUREREF ) => m - CALCULATE ( m, ALLSELECTED () )"),
]
DYNFMT = "VAR _v = ABS ( SELECTEDMEASURE () )\nRETURN\n    SWITCH ( TRUE (),\n        _v >= 1e9, \"$#,0.0,,,\"\"bn\"\"\",\n        _v >= 1e6, \"$#,0.0,,\"\"m\"\"\",\n        _v >= 1e3, \"$#,0,\"\"k\"\"\",\n        \"$#,0\"\n    )"
RATIOS = "[Default Rate], [Loss Rate], [Realised ROI], [Avg Interest Rate], [Delinquency Rate], [Dollar Default Rate]"

# Calculation group 'Time Calc' (7-s3/7-s4) on the Gregorian calendar: (item, DAX, format string expression).
# Ratios are not additive over time, so YTD and 12M Rolling leave them as they are (ISSELECTEDMEASURE).
TIME_ITEMS = [
    ("Current", "SELECTEDMEASURE ()", None),
    ("PY", "CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, YEAR ) )", None),
    ("YoY %", "VAR _py = CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, YEAR ) )\n"
              "RETURN DIVIDE ( SELECTEDMEASURE () - _py, _py )", '"+0.0%;-0.0%;0.0%"'),
    ("YTD", f"IF (\n    ISSELECTEDMEASURE ( {RATIOS} ),\n    SELECTEDMEASURE (),\n"
            "    CALCULATE ( SELECTEDMEASURE (), DATESYTD ( 'Gregorian' ) )\n)", None),
    ("12M Rolling", f"IF (\n    ISSELECTEDMEASURE ( {RATIOS} ),\n    SELECTEDMEASURE (),\n"
                    "    CALCULATE ( SELECTEDMEASURE (), DATESINPERIOD ( 'Date'[Date], MAX ( 'Date'[Date] ), -12, MONTH ) )\n)", None),
]

# DAX-only tables: name -> (DAX source, [(column, sourceColumn, dataType, extra lines)])
WHATIF = ['', 'extendedProperty ParameterMetadata =', '\t\t{', '\t\t  "version": 0', '\t\t}']
CALCULATED = {
    # What-if parameters (19-s2): stress the live book's expected loss.
    "PD Multiplier": ("GENERATESERIES ( 0.5, 2, 0.1 )",
                      [("PD Multiplier", "[Value]", "double", ["formatString: 0.0x", *WHATIF])]),
    "LGD Shock": ("GENERATESERIES ( -0.2, 0.1, 0.05 )",
                  [("LGD Shock", "[Value]", "double", ["formatString: +0%;-0%;0%", *WHATIF])]),
    # Disconnected month-on-book axis (16-s8): cohort default curve computed in DAX from loan rows.
    "MOB": ('SELECTCOLUMNS ( GENERATESERIES ( 0, 60, 1 ), "Month On Book", [Value] )',
            [("Month On Book", "[Month On Book]", "int64", ["formatString: 0"])]),
    # Dynamic segmentation (10-s3): DTI bands live in the model, not in SQL; edit the rows to re-band.
    "DTI Segments": ('DATATABLE ( "DTI Segment", STRING, "Min DTI", DOUBLE, "Max DTI", DOUBLE, "Segment Sort", INTEGER, { '
                     '{ "Under 10", 0, 10, 1 }, { "10–20", 10, 20, 2 }, { "20–30", 20, 30, 3 }, '
                     '{ "30–40", 30, 40, 4 }, { "40 and over", 40, 1000, 5 } } )',
                     [("DTI Segment", "[DTI Segment]", "string", ["sortByColumn: 'Segment Sort'"]),
                      ("Min DTI", "[Min DTI]", "double", ["isHidden"]), ("Max DTI", "[Max DTI]", "double", ["isHidden"]),
                      ("Segment Sort", "[Segment Sort]", "int64", ["isHidden"])]),
    # Loan-lifecycle funnel stages (10-s12) and the two steps of the mix / rate variance waterfall (10-s13).
    "Funnel Stage": ('DATATABLE ( "Stage", STRING, "Stage Sort", INTEGER, { { "Issued", 1 }, { "Matured", 2 }, '
                     '{ "Resolved", 3 }, { "Defaulted", 4 }, { "Recovered > 0", 5 } } )',
                     [("Stage", "[Stage]", "string", ["sortByColumn: 'Stage Sort'"]),
                      ("Stage Sort", "[Stage Sort]", "int64", ["isHidden"])]),
    "Variance Step": ('DATATABLE ( "Step", STRING, "Step Sort", INTEGER, { { "Grade mix", 1 }, { "Rate within grade", 2 } } )',
                      [("Step", "[Step]", "string", ["sortByColumn: 'Step Sort'"]),
                       ("Step Sort", "[Step Sort]", "int64", ["isHidden"])]),
    # RLS mapping (16-s13). SYNTHETIC: example.com mailboxes, not real users — replace with your directory.
    "Region Access": ('DATATABLE ( "User Email", STRING, "Region", STRING, { '
                      '{ "analyst.west@example.com", "West" }, { "analyst.south@example.com", "South" }, '
                      '{ "analyst.east@example.com", "Northeast" }, { "analyst.east@example.com", "Midwest" } } )',
                      [("User Email", "[User Email]", "string", ["isHidden"]), ("Region", "[Region]", "string", ["isHidden"])]),
}
CALC_HIDDEN = {"Region Access"}

# Field parameters (7-s1/7-s2): (label, NAMEOF target). Risk Metric swaps the matrix measure on Vintage & Loss,
# Borrower Attribute swaps the column chart's axis on Borrower Profile. Import model only.
FIELD_PARAMS = {
    "Risk Metric": [("Default Rate", "[Default Rate]"), ("Loss Rate", "[Loss Rate]"),
                    ("Realised ROI", "[Realised ROI]"), ("Avg Interest Rate", "[Avg Interest Rate]")],
    "Borrower Attribute": [("Income", "Loans[Income Band]"), ("Debt-to-income", "Loans[DTI Band]"),
                           ("Employment length", "Loans[Employment Length]"),
                           ("Card utilisation", "Loans[Revolving Utilisation Band]"),
                           ("Home ownership", "Loans[Home Ownership]")],
}

ROLES = {
    # Dynamic RLS: the signed-in user sees the regions mapped to them in 'Region Access'.
    "Regional Analyst": {"filters": {
        "State": "VAR _u = USERPRINCIPALNAME () RETURN NOT ISEMPTY ( FILTER ( 'Region Access', "
                 "'Region Access'[User Email] = _u && 'Region Access'[Region] = State[Region] ) )",
        "Region Access": "'Region Access'[User Email] = USERPRINCIPALNAME ()"},
        "hide_tables": [], "hide_columns": {}},
    # Static RLS (5-s12).
    "West Region": {"filters": {"State": "State[Region] = \"West\""}, "hide_tables": [], "hide_columns": {}},
    # OLS + CLS (11-s13/11-s14): business users see the portfolio, not the model-risk internals or borrower income/DTI.
    "Business Viewer": {"filters": {}, "hide_tables": ["Model ROC", "Model Calibration", "Model Importance"],
                        "hide_columns": {"Loans": ["Annual Income", "DTI"]}},
}

PERSPECTIVES = {
    "Credit Risk": ["Loans", "Date", "Grade", "State", "Purpose", "Cash Bridge", "Vintage Curve", "Vintage Summary",
                    "Grade LGD", "Book Balance", "PD Multiplier", "LGD Shock", "MOB", "Time Calc", "DTI Segments",
                    "Funnel Stage", "Variance Step", "_Measures"],
    "Model Risk": ["Loans", "Grade", "Model Calibration", "Model ROC", "Model Importance", "Model Metrics",
                   "Model Metrics Long", "Field Glossary", "_Measures"],
}

SYNONYMS = {  # Q&A / Copilot terms
    "Loans": ["loans", "notes", "credits"], "Default Rate": ["bad rate", "charge-off rate", "default ratio"],
    "Funded Amount": ["originations", "volume", "lending"], "Grade": ["rating", "credit grade"],
    "Expected Loss": ["EL", "provision"], "State": ["region", "location"],
}

# Composite / Fabric variant (15-s6, 11-s1, 11-s2): LendingClub.Fabric.SemanticModel reads the same marts from a
# Lakehouse SQL endpoint. Loans is DirectQuery (2.26M rows stay in the Lakehouse), the small dims it joins are Dual,
# Grade stays Import (it carries calculated columns), and 'Loans Agg' (Import) answers totals via alternateOf.
FABRIC_MODE = {"Loans": "directQuery", "Date": "dual", "State": "dual", "Purpose": "dual"}
LOANS_AGG = [  # (column, mart column, dataType, summarization, base column in Loans)
    ("Agg Issue Date", "issue_date", "dateTime", "groupBy", "Issue Date"),
    ("Agg Sub Grade", "sub_grade", "string", "groupBy", "Sub Grade"),
    ("Agg State", "state", "string", "groupBy", "State"),
    ("Agg Purpose", "purpose", "string", "groupBy", "Purpose"),
    ("Agg Funded Amt", "funded_amount", "double", "sum", "Funded Amt"),
    ("Agg Loans", "loans", "int64", "count", None),
]
FABRIC_RELATIONSHIPS = [("Loans Agg", "Agg Issue Date", "Date", "date", True, None),
                        ("Loans Agg", "Agg Sub Grade", "Grade", "sub_grade", True, None),
                        ("Loans Agg", "Agg State", "State", "state", True, None),
                        ("Loans Agg", "Agg Purpose", "Purpose", "purpose", True, None)]
LOCAL_ONLY = {"Field Glossary"}  # reads the LendingClub xlsx from the dev machine; not in the Lakehouse

# Mix / rate variance (10-s13): weights and rates per grade in the latest matured vintage year vs the year before.
YEAR_BLOCK = "VAR _y = CALCULATE ( MAX ( Loans[Vintage Year] ), Loans[Is Matured] = TRUE () )\nVAR _n1 = CALCULATE ( [Matured Loans], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y )\nVAR _n0 = CALCULATE ( [Matured Loans], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y - 1 )\n"
MIX_EFFECT = "VAR _y = CALCULATE ( MAX ( Loans[Vintage Year] ), Loans[Is Matured] = TRUE () )\nVAR _n1 = CALCULATE ( [Matured Loans], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y )\nVAR _n0 = CALCULATE ( [Matured Loans], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y - 1 )\nRETURN\n    IF ( _n0 > 0,\n        SUMX ( VALUES ( Grade[Grade] ),\n            VAR _w1 = DIVIDE ( CALCULATE ( [Matured Loans], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y ), _n1 )\n            VAR _w0 = DIVIDE ( CALCULATE ( [Matured Loans], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y - 1 ), _n0 )\n            VAR _r1 = DIVIDE ( CALCULATE ( [Matured Defaults], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y ), CALCULATE ( [Matured Loans], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y ) )\n            VAR _r0 = DIVIDE ( CALCULATE ( [Matured Defaults], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y - 1 ), CALCULATE ( [Matured Loans], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y - 1 ) )\n            RETURN ( _w1 - _w0 ) * _r0\n        )\n    )"
RATE_EFFECT = "VAR _y = CALCULATE ( MAX ( Loans[Vintage Year] ), Loans[Is Matured] = TRUE () )\nVAR _n1 = CALCULATE ( [Matured Loans], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y )\nVAR _n0 = CALCULATE ( [Matured Loans], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y - 1 )\nRETURN\n    IF ( _n0 > 0,\n        SUMX ( VALUES ( Grade[Grade] ),\n            VAR _w1 = DIVIDE ( CALCULATE ( [Matured Loans], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y ), _n1 )\n            VAR _w0 = DIVIDE ( CALCULATE ( [Matured Loans], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y - 1 ), _n0 )\n            VAR _r1 = DIVIDE ( CALCULATE ( [Matured Defaults], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y ), CALCULATE ( [Matured Loans], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y ) )\n            VAR _r0 = DIVIDE ( CALCULATE ( [Matured Defaults], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y - 1 ), CALCULATE ( [Matured Loans], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y - 1 ) )\n            RETURN _w1 * ( _r1 - _r0 )\n        )\n    )"

# (folder, name, format, description, DAX[, format string expression])
MEASURES = [
    # ------------------------------------------------------------ Portfolio
    ("Portfolio", "Loans Issued", NUM0, "Number of loans originated.", "COUNTROWS ( Loans )"),
    ("Portfolio", "Funded Amount", None, "Principal funded by investors. Dynamic format: $bn / $m / $k by size.", "SUM ( Loans[Funded Amt] )",
     DYNFMT),
    ("Portfolio", "Avg Loan Size", USD0, "Average funded amount per loan.", "DIVIDE ( [Funded Amount], [Loans Issued] )"),
    ("Portfolio", "Avg Interest Rate", PCT2, "Funded-amount-weighted interest rate.",
     "DIVIDE ( SUMX ( Loans, Loans[Funded Amt] * Loans[Interest Rate] ), [Funded Amount] )"),
    ("Portfolio", "Avg Annual Income", USD0, "Average self-reported annual income of the borrowers.", "AVERAGE ( Loans[Annual Income] )"),
    ("Portfolio", "Avg DTI", NUM1, "Average debt-to-income ratio (%).", "AVERAGE ( Loans[DTI] )"),
    ("Portfolio", "Active Loans", NUM0, "Loans still being repaid (current or delinquent).",
     "CALCULATE ( [Loans Issued], Loans[Outcome] IN { \"Current\", \"Delinquent\" } )"),
    ("Portfolio", "Outstanding Principal", USD0, "Principal still owed on the tape at the data cut-off.", "SUM ( Loans[Outstanding Principal Amt] )"),
    ("Portfolio", "Delinquency Rate", PCT2, "Share of active loans that are late or in grace period.",
     "DIVIDE ( CALCULATE ( [Loans Issued], Loans[Outcome] = \"Delinquent\" ), [Active Loans] )"),
    ("Portfolio", "Funded Share", PCT1, "Share of funded amount within the visual selection.",
     "DIVIDE ( [Funded Amount], CALCULATE ( [Funded Amount], ALLSELECTED () ) )"),
    # --------------------------------------------------------- Credit risk
    ("Credit Risk", "Matured Loans", NUM0, "Resolved loans whose full term ended before the data cut-off (Sep 2018) — their outcome is final.",
     "LC.MaturedOnly ( [Loans Issued] )"),
    ("Credit Risk", "Matured Defaults", NUM0, "Defaults among matured loans (LC.MaturedOnly) — numerator of Default Rate.", "LC.MaturedOnly ( [Defaults] )"),
    ("Credit Risk", "Defaults", NUM0, "Charged-off or defaulted loans.", "SUM ( Loans[Is Default] )"),
    ("Credit Risk", "Default Rate", PCT1, "Lifetime default rate on matured loans. Young loans are excluded: early defaults resolve before good payers do, which would overstate risk.",
     "LC.SafeRate ( [Matured Defaults], [Matured Loans] )"),
    ("Credit Risk", "Dollar Default Rate", PCT1, "Funded amount of defaulted loans ÷ funded amount of resolved loans.",
     "DIVIDE (\n"
     "    CALCULATE ( [Funded Amount], Loans[Is Default] = 1, Loans[Is Matured] = TRUE () ),\n"
     "    CALCULATE ( [Funded Amount], Loans[Is Resolved] = TRUE (), Loans[Is Matured] = TRUE () )\n"
     ")"),
    ("Credit Risk", "Exposure at Default", USD0, "Unpaid principal at charge-off.", "SUM ( Loans[EAD Amt] )"),
    ("Credit Risk", "Net Recoveries", USD0, "Post charge-off recoveries net of collection fees.", "SUM ( Loans[Net Recoveries Amt] )"),
    ("Credit Risk", "Net Credit Loss", USD0, "Exposure at default minus net recoveries.", "SUM ( Loans[Net Loss Amt] )"),
    ("Credit Risk", "LGD", PCT1, "Loss given default = net credit loss ÷ exposure at default.",
     "DIVIDE ( [Net Credit Loss], [Exposure at Default] )"),
    ("Credit Risk", "Recovery Rate", PCT1, "Net recoveries ÷ exposure at default on charged-off loans.", "DIVIDE ( [Net Recoveries], [Exposure at Default] )"),
    ("Credit Risk", "Loss Rate", PCT2, "Lifetime net credit loss ÷ funded amount, matured loans.",
     "LC.MaturedOnly ( DIVIDE ( [Net Credit Loss], [Funded Amount] ) )"),
    ("Credit Risk", "Avg Months to Default", NUM1, "Months on book when the loan was charged off.",
     "AVERAGE ( Loans[Months To Default] )"),
    ("Credit Risk", "Default Rate vs Portfolio", "+0.0%;-0.0%;0.0%", "Default rate minus the all-portfolio default rate (percentage points).",
     "LC.VsPortfolio ( [Default Rate] )"),
    ("Credit Risk", "Default Rate Color", None, "Traffic-light colour for default-rate conditional formatting.",
     "VAR _r = [Default Rate]\n"
     "RETURN\n"
     "    SWITCH ( TRUE (),\n"
     "        ISBLANK ( _r ), \"#9AA4B2\",\n"
     "        _r >= 0.30, \"#C2410C\",\n"
     "        _r >= 0.20, \"#F59E0B\",\n"
     "        _r >= 0.12, \"#EAB308\",\n"
     "        \"#0F766E\"\n"
     "    )"),
    # -------------------------------------------------------------- Returns
    ("Returns", "Interest Earned", USD0, "Interest paid by borrowers (all loans).", "SUM ( Loans[Interest Paid Amt] )"),
    ("Returns", "Net Profit", None, "Cash received minus principal funded, resolved loans only. Dynamic format.", "SUM ( Loans[Net Profit Amt] )",
     DYNFMT),
    ("Returns", "Realised ROI", PCT2, "Lifetime net profit ÷ funded amount on matured loans (final outcomes only).",
     "LC.MaturedOnly ( DIVIDE ( [Net Profit], [Funded Amount] ) )"),
    ("Returns", "ROI Color", None, "Series blue; status red only when a segment lost money.",
     "IF ( [Realised ROI] < 0, \"#d03b3b\", \"#2a78d6\" )"),
    # ------------------------------------------------------- Expected loss
    ("Expected Loss", "Avg PD", PCT2, "Average model probability of default (lifetime, at origination).",
     "AVERAGE ( Loans[PD] )"),
    ("Expected Loss", "Expected Loss", USD0, "Live book: outstanding principal × PD × grade LGD.", "SUM ( Loans[Expected Loss Amt] )"),
    ("Expected Loss", "Expected Loss Rate", PCT2, "Expected loss ÷ outstanding principal of the live book.",
     "DIVIDE ( [Expected Loss], CALCULATE ( [Outstanding Principal], Loans[Outcome] IN { \"Current\", \"Delinquent\" } ) )"),
    ("Expected Loss", "Predicted Default Rate", PCT1, "Average PD on matured loans — compare with Default Rate.",
     "LC.MaturedOnly ( [Avg PD] )"),
    ("Expected Loss", "AUC", NUM3, "Area under the ROC curve on the 2015 out-of-time test set.", "MAX ( 'Model Metrics'[AUC Value] )"),
    ("Expected Loss", "Gini", NUM3, "Gini coefficient of the PD model on the 2015 out-of-time test set (2 × AUC − 1).", "MAX ( 'Model Metrics'[Gini Value] )"),
    ("Expected Loss", "KS", NUM3, "Kolmogorov–Smirnov separation between defaulters and payers.", "MAX ( 'Model Metrics'[KS Value] )"),
    ("Expected Loss", "Top Decile Capture", PCT1, "Share of all test-set defaults caught in the riskiest 10% of loans.",
     "CALCULATE ( MAX ( 'Model Calibration'[Cum Default Capture] ), 'Model Calibration'[Risk Decile] = \"D10\" )"),
    ("Expected Loss", "Top Decile Lift", "0.00x", "Default rate in the riskiest 10% of loans ÷ the overall default rate (2015 test set).",
     "CALCULATE ( MAX ( 'Model Calibration'[Lift] ), 'Model Calibration'[Risk Decile] = \"D10\" )"),
    # ------------------------------------------------------------- Vintage
    ("Vintage", "Cumulative Default Rate", PCT1,
     "Share of a vintage's 36-month loans defaulted by the month on book. Shows all grades unless one grade is selected.",
     "IF (\n"
     "    HASONEVALUE ( 'Vintage Curve'[Vintage Grade] ),\n"
     "    AVERAGE ( 'Vintage Curve'[Cum Default Rate] ),\n"
     "    IF (\n"
     "        ISCROSSFILTERED ( 'Vintage Curve'[Vintage Grade] ),\n"
     "        -- Risk tier / grade filter arrives through the many-to-many relationship: pool the selected grades\n"
     "        DIVIDE ( SUM ( 'Vintage Curve'[Vintage Defaults] ), SUM ( 'Vintage Curve'[Vintage Loans] ) ),\n"
     "        CALCULATE ( AVERAGE ( 'Vintage Curve'[Cum Default Rate] ), 'Vintage Curve'[Vintage Grade] = \"All\" )\n"
     "    )\n"
     ")"),
    ("Vintage", "Peak Cohort Cum Default", PCT1,
     "Highest cumulative default rate any issue cohort reached, all grades (Vintage Summary, grouped in Power Query).",
     "CALCULATE ( MAX ( 'Vintage Summary'[Lifetime Cum Default Rate] ), 'Vintage Summary'[Summary Grade] = \"All\" )"),
    # ---------------------------------------------------- Model diagnostics
    ("Model Diagnostics", "Metric Value", NUM3, "AUC, Gini or KS of the PD model, one per row of Model Metrics Long.",
     "SUM ( 'Model Metrics Long'[Metric Score] )"),
    ("Model Diagnostics", "ROC True Positive Rate", PCT1, "Share of defaulters caught at this ROC threshold (Model ROC).", "MAX ( 'Model ROC'[True Positive Rate] )"),
    ("Model Diagnostics", "ROC Random Baseline", PCT1, "Diagonal of the ROC chart: what a random classifier would catch.", "MAX ( 'Model ROC'[Random Classifier] )"),
    ("Model Diagnostics", "Decile Predicted PD", PCT1, "Average model PD in the risk decile (2015 test set).",
     "AVERAGE ( 'Model Calibration'[Predicted PD] )"),
    ("Model Diagnostics", "Decile Observed Default Rate", PCT1, "Actual default rate in the risk decile (2015 test set).",
     "AVERAGE ( 'Model Calibration'[Observed Default Rate] )"),
    ("Model Diagnostics", "Cumulative Default Capture", PCT1, "Share of all defaults caught when rejecting the riskiest X% of loans.",
     "MAX ( 'Model Calibration'[Cum Default Capture] )"),
    ("Model Diagnostics", "Feature Importance", "0.0000", "Drop in test AUC when the feature is shuffled (permutation importance).",
     "SUM ( 'Model Importance'[AUC Drop] )"),
    # -------------------------------------------------------------- Visuals
    ("Visual Helpers", "Tile X", "0", "Tile-grid map column.", "MAX ( State[Tile Col] )"),
    ("Visual Helpers", "Tile Y", "0", "Tile-grid map row (negated so row 0 plots at the top).", "- MAX ( State[Tile Row] )"),
    ("Visual Helpers", "Cash Bridge Amount", USD0, "Waterfall step value for matured loans: funded principal out, repayments in.",
     "VAR _step = SELECTEDVALUE ( 'Cash Bridge'[Step Sort] )\n"
     "RETURN\n"
     "    CALCULATE (\n"
     "        SWITCH ( _step,\n"
     "            1, - SUM ( Loans[Funded Amt] ),\n"
     "            2, SUM ( Loans[Principal Paid] ),\n"
     "            3, SUM ( Loans[Interest Paid Amt] ),\n"
     "            4, SUM ( Loans[Late Fees Amt] ),\n"
     "            5, SUM ( Loans[Net Recoveries Amt] )\n"
     "        ),\n"
     "        Loans[Is Resolved] = TRUE (),\n"
     "        Loans[Is Matured] = TRUE ()\n"
     "    )"),
    ("Vintage", "Cum Default 2008–09 crisis", PCT1, "Cumulative default rate by month on book, 2008–09 crisis vintage (one line per vintage).",
     "CALCULATE ( [Cumulative Default Rate], 'Vintage Curve'[Vintage] = \"2008–09 crisis\" )"),
    ("Vintage", "Cum Default 2010–12", PCT1, "Cumulative default rate by month on book, 2010–12 vintage (one line per vintage).",
     "CALCULATE ( [Cumulative Default Rate], 'Vintage Curve'[Vintage] = \"2010–12\" )"),
    ("Vintage", "Cum Default 2013–14", PCT1, "Cumulative default rate by month on book, 2013–14 vintage (one line per vintage).",
     "CALCULATE ( [Cumulative Default Rate], 'Vintage Curve'[Vintage] = \"2013–14\" )"),
    ("Vintage", "Cum Default 2015–16", PCT1, "Cumulative default rate by month on book, 2015–16 vintage (one line per vintage).",
     "CALCULATE ( [Cumulative Default Rate], 'Vintage Curve'[Vintage] = \"2015–16\" )"),
    ("Vintage", "Cum Default 2017", PCT1, "Cumulative default rate by month on book, 2017 vintage (one line per vintage).",
     "CALCULATE ( [Cumulative Default Rate], 'Vintage Curve'[Vintage] = \"2017\" )"),
    # --------------------------------------------------- Time intelligence
    ("Time Intelligence", "Funded PY", USD0, "Funded amount in the same period last year (SAMEPERIODLASTYEAR).", "CALCULATE ( [Funded Amount], SAMEPERIODLASTYEAR ( 'Date'[Date] ) )"),
    ("Time Intelligence", "Funded YoY %", PCT1, "Change in funded amount vs the same period last year; blank when there is no prior year.",
     "VAR _py = [Funded PY]\nRETURN IF ( NOT ISBLANK ( _py ), DIVIDE ( [Funded Amount] - _py, _py ) )"),
    ("Time Intelligence", "Funded YTD", USD0, "Funded amount year-to-date (TOTALYTD on the Date table).", "TOTALYTD ( [Funded Amount], 'Date'[Date] )"),
    ("Time Intelligence", "Funded 12M Rolling", USD0, "Funded amount over the trailing 12 months.",
     "CALCULATE ( [Funded Amount], DATESINPERIOD ( 'Date'[Date], MAX ( 'Date'[Date] ), -12, MONTH ) )"),
    ("Time Intelligence", "Funded YoY Label", None, "Card subtitle: ▲/▼ change vs same period last year.",
     "VAR _g = [Funded YoY %]\n"
     "RETURN\n"
     "    IF ( ISBLANK ( _g ), \"no prior-year data\",\n"
     "        IF ( _g >= 0, \"▲ \", \"▼ \" ) & FORMAT ( ABS ( _g ), \"0.0%\" ) & \" vs prior year\" )"),
    # ----------------------------------------------------------- Report UI
    ("Report UI", "Period Label", None, "Selected issue-date range, for page subtitles.",
     "FORMAT ( MIN ( Loans[Issue Date] ), \"mmm yyyy\" ) & \" – \" & FORMAT ( MAX ( Loans[Issue Date] ), \"mmm yyyy\" )"),
    ("Report UI", "Headline Insight", None, "One-sentence dynamic insight for the overview page.",
     "\"Every 1 in \" & FORMAT ( DIVIDE ( 1, [Default Rate] ), \"0.0\" ) & \" resolved loans defaulted, and lenders kept \"\n"
     "    & FORMAT ( [Realised ROI], \"0.0%\" ) & \" of funded principal as lifetime profit.\""),
    # ------------------------------------------------- Filter context (B)
    ("Filter Context", "States Lent In", NUM0, "Distinct states with at least one loan in the selection.", "DISTINCTCOUNT ( Loans[State] )"),
    ("Filter Context", "Sub Grades Active", NUM0, "Distinct sub-grades with at least one loan in the selection.",
     "DISTINCTCOUNT ( Loans[Sub Grade] )"),
    ("Filter Context", "Default Rate 60M", PCT1,
     "Default rate of 60-month loans. KEEPFILTERS intersects with the Term already on the row: blank on 36-month rows.",
     "CALCULATE ( [Default Rate], KEEPFILTERS ( Loans[Term] = \"60 months\" ) )"),
    ("Filter Context", "Default Rate 60M (Replace)", PCT1,
     "Same filter without KEEPFILTERS: it overwrites the row's Term, so every row shows the 60-month rate.",
     "CALCULATE ( [Default Rate], Loans[Term] = \"60 months\" )"),
    ("Filter Context", "Share of All Defaults", PCT1,
     "Matured defaults in the row ÷ all matured defaults. REMOVEFILTERS(Loans) clears the expanded fact table, dimensions included.",
     "DIVIDE ( [Matured Defaults], CALCULATE ( [Matured Defaults], REMOVEFILTERS ( Loans ) ) )"),
    ("Filter Context", "Sub Grade vs Grade Default", "+0.0%;-0.0%;0.0%",
     "Sub-grade default rate minus its letter grade's rate (ALLEXCEPT keeps only the Grade filter).",
     "[Default Rate] - CALCULATE ( [Default Rate], ALLEXCEPT ( Grade, Grade[Grade] ) )"),
    ("Filter Context", "High Rate Default Rate", PCT1,
     "Default rate of loans priced at 20% or more. FILTER over ALL of one column replaces only that column's filter.",
     "CALCULATE ( [Default Rate], FILTER ( ALL ( Loans[Interest Rate] ), Loans[Interest Rate] >= 0.20 ) )"),
    ("Filter Context", "Default Rate Sub-prime", PCT1,
     "Default rate of Sub-prime grades. The Grade filter reaches Loans because Loans' expanded table includes Grade.",
     "CALCULATE ( [Default Rate], Grade[Risk Tier] = \"Sub-prime\" )"),
    ("Filter Context", "Loans Closed", NUM0,
     "Resolved loans by the month of their last payment: the inactive Last Payment Date relationship (role-playing Date).",
     "CALCULATE (\n    [Loans Issued],\n    Loans[Is Resolved] = TRUE (),\n    USERELATIONSHIP ( Loans[Last Payment Date], 'Date'[Date] )\n)"),
    # ------------------------------------------------- Row context (B)
    ("Row Context", "Expected Loss (DAX)", USD0,
     "Expected loss recomputed loan by loan in DAX: outstanding × PD × grade LGD reached through Loans → Grade → 'Grade LGD'.",
     "CALCULATE (\n    SUMX ( Loans, Loans[Outstanding Principal Amt] * Loans[PD] * RELATED ( 'Grade LGD'[Grade LGD Rate] ) ),\n"
     "    Loans[Outcome] IN { \"Current\", \"Delinquent\" }\n)"),
    ("Row Context", "Expected Loss Gap", USD0, "Expected Loss (DAX) minus the SQL Expected Loss: 0 when both agree.",
     "[Expected Loss (DAX)] - [Expected Loss]"),
    ("Row Context", "Rate Premium", "+0.00%;-0.00%;0.00%",
     "Average gap between a loan's rate and its sub-grade's average rate (RELATED to a calculated column).",
     "AVERAGEX ( Loans, Loans[Interest Rate] - RELATED ( Grade[Sub Grade Avg Rate] ) )"),
    ("Row Context", "Worst Sub Grade Default Rate", PCT1, "Highest sub-grade default rate in the selection (MAXX over VALUES).",
     "MAXX ( VALUES ( Grade[Sub Grade] ), [Default Rate] )"),
    ("Row Context", "Worst State", None, "State with the highest default rate among states with 1,000+ matured loans.",
     "CONCATENATEX (\n    TOPN (\n        1,\n        FILTER (\n            ADDCOLUMNS ( VALUES ( State[State Name] ), \"@dr\", [Default Rate], \"@n\", [Matured Loans] ),\n"
     "            [@n] >= 1000\n        ),\n        [@dr], DESC\n    ),\n    State[State Name],\n    \", \"\n)"),
    ("Row Context", "Cohort Default by MOB", PCT1,
     "Share of matured 36-month loans that defaulted by the month on book picked on the disconnected MOB axis.",
     "VAR _k = MAX ( MOB[Month On Book] )\nRETURN\n    DIVIDE (\n"
     "        CALCULATE ( [Defaults], Loans[Months To Default] <= _k, Loans[Term] = \"36 months\", Loans[Is Matured] = TRUE () ),\n"
     "        CALCULATE ( [Loans Issued], Loans[Term] = \"36 months\", Loans[Is Matured] = TRUE () )\n    )"),
    # ------------------------------------------------- Window functions (B)
    ("Window", "Default Rate Prev Vintage", PCT1, "Default rate of the previous issue year (OFFSET -1 on Vintage Year).",
     "CALCULATE ( [Default Rate], OFFSET ( -1, ALLSELECTED ( Loans[Vintage Year] ), ORDERBY ( Loans[Vintage Year] ) ) )"),
    ("Window", "Funded Running Total", USD0, "Funded amount from the first selected month to the current one (WINDOW).",
     "CALCULATE (\n    [Funded Amount],\n    WINDOW ( 1, ABS, 0, REL, ALLSELECTED ( 'Date'[Month Year], 'Date'[Month Year Sort] ), ORDERBY ( 'Date'[Month Year Sort] ) )\n)"),
    ("Window", "State Default Rank", "0", "Rank of the state by default rate, 1 = worst (RANK DENSE).",
     "RANK ( DENSE, ALLSELECTED ( State[State Name] ), ORDERBY ( [Default Rate], DESC ) )"),
    ("Window", "Worst Sub Grade", None, "Sub-grade with the highest default rate in the current grade (INDEX 1).",
     "VAR _t = ADDCOLUMNS ( CALCULATETABLE ( VALUES ( Grade[Sub Grade] ) ), \"@dr\", [Default Rate] )\n"
     "RETURN MAXX ( INDEX ( 1, _t, ORDERBY ( [@dr], DESC ) ), Grade[Sub Grade] )"),
    # ------------------------------------------------- Time intelligence (B)
    ("Time Intelligence", "Funded QTD", USD0, "Funded amount quarter to date.", "TOTALQTD ( [Funded Amount], 'Date'[Date] )"),
    ("Time Intelligence", "Funded MTD", USD0, "Funded amount month to date.", "TOTALMTD ( [Funded Amount], 'Date'[Date] )"),
    ("Time Intelligence", "Funded PM", USD0, "Funded amount in the previous month.",
     "CALCULATE ( [Funded Amount], DATEADD ( 'Date'[Date], -1, MONTH ) )"),
    ("Time Intelligence", "Funded PQ", USD0, "Funded amount in the previous quarter.",
     "CALCULATE ( [Funded Amount], PREVIOUSQUARTER ( 'Date'[Date] ) )"),
    ("Time Intelligence", "Funded 3M Avg", USD0, "Average monthly funded amount over the trailing 3 months.",
     "CALCULATE (\n    AVERAGEX ( VALUES ( 'Date'[Month Year] ), [Funded Amount] ),\n"
     "    DATESINPERIOD ( 'Date'[Date], MAX ( 'Date'[Date] ), -3, MONTH )\n)"),
    ("Time Intelligence", "Funded PY (Cal)", USD0, "Prior-year funded amount on the Gregorian calendar (calendar-based TI).",
     "CALCULATE ( [Funded Amount], DATEADD ( 'Gregorian', -1, YEAR ) )"),
    ("Time Intelligence", "Funded YoY % (Cal)", "+0.0%;-0.0%;0.0%",
     "YoY growth with calendar-based TI; equals Funded YoY % on a Gregorian calendar.",
     "VAR _py = [Funded PY (Cal)]\nRETURN DIVIDE ( [Funded Amount] - _py, _py )"),
    ("Book", "Book Outstanding", USD0,
     "Estimated outstanding principal at the last date in context: semi-additive, a stock is not summed over months.",
     "CALCULATE ( SUM ( 'Book Balance'[Est Outstanding] ), LASTDATE ( 'Date'[Date] ) )"),
    # ------------------------------------------------- Many-to-many vs TREATAS (B)
    ("Vintage", "Vintage Cum Default (Tier)", PCT1,
     "Same curve as Cumulative Default Rate, but filtered by a virtual relationship (TREATAS) with the physical one switched off.",
     "CALCULATE (\n    [Cumulative Default Rate],\n    TREATAS ( VALUES ( Grade[Grade] ), 'Vintage Curve'[Vintage Grade] ),\n"
     "    CROSSFILTER ( 'Vintage Curve'[Vintage Grade], Grade[Grade], NONE )\n)"),
    # ------------------------------------------------- UDF consumers + what-if (B)
    ("Returns", "ROI vs Portfolio", "+0.0%;-0.0%;0.0%", "Realised ROI minus the all-selection ROI (LC.VsPortfolio).",
     "LC.VsPortfolio ( [Realised ROI] )"),
    ("Scenario", "PD Multiplier Value", "0.0x", "Selected PD stress multiplier (1.0x when nothing is selected).",
     "SELECTEDVALUE ( 'PD Multiplier'[PD Multiplier], 1 )"),
    ("Scenario", "LGD Shock Value", "+0%;-0%;0%", "Selected LGD shock in points (0 when nothing is selected).",
     "SELECTEDVALUE ( 'LGD Shock'[LGD Shock], 0 )"),
    ("Scenario", "Stressed Expected Loss", USD0,
     "Live-book expected loss with PD × multiplier (capped at 100%) and LGD + shock (kept within 0–100%).",
     "VAR _m = [PD Multiplier Value]\nVAR _s = [LGD Shock Value]\nRETURN\n    CALCULATE (\n"
     "        SUMX ( Loans,\n            Loans[Outstanding Principal Amt] * MIN ( 1, Loans[PD] * _m )\n"
     "                * MAX ( 0, MIN ( 1, RELATED ( 'Grade LGD'[Grade LGD Rate] ) + _s ) ) ),\n"
     "        Loans[Outcome] IN { \"Current\", \"Delinquent\" }\n    )"),
    # ------------------------------------------------------- Report & UX (C)
    ("Vintage", "Cumulative Dollar Default Rate", PCT1,
     "Share of a vintage's funded dollars (36-month loans) defaulted by the month on book. Same grade logic as "
     "Cumulative Default Rate; pooled over the selected grades by dollars.",
     "-- a VAR would be evaluated once, before CALCULATE could change its filter, so the ratio is written twice\n"
     "IF (\n"
     "    HASONEVALUE ( 'Vintage Curve'[Vintage Grade] ) || ISCROSSFILTERED ( 'Vintage Curve'[Vintage Grade] ),\n"
     "    DIVIDE ( SUM ( 'Vintage Curve'[Vintage Defaulted Funded] ), SUM ( 'Vintage Curve'[Vintage Funded] ) ),\n"
     "    CALCULATE (\n"
     "        DIVIDE ( SUM ( 'Vintage Curve'[Vintage Defaulted Funded] ), SUM ( 'Vintage Curve'[Vintage Funded] ) ),\n"
     "        'Vintage Curve'[Vintage Grade] = \"All\"\n"
     "    )\n"
     ")"),
    *[("Vintage", f"Cum Dollar Default {v}", PCT1, f"Cumulative dollar default rate (defaulted ÷ funded $) by month on book, {v} vintage.",
       f"CALCULATE ( [Cumulative Dollar Default Rate], 'Vintage Curve'[Vintage] = \"{v}\" )")
      for v in ("2008–09 crisis", "2010–12", "2013–14", "2015–16", "2017")],
    # Interest-rate distribution: the five numbers of the Deneb box plot per grade (17-s13).
    *[("Pricing", f"Rate P{p:02d}", PCT2, f"{p}th percentile of the loan interest rate.",
       f"PERCENTILE.INC ( Loans[Interest Rate], {p / 100} )") for p in (5, 25, 50, 75, 95)],
    # Analytics-pane reference lines (17-s17) read these at the visual total.
    ("Visual Helpers", "Sub Grade Avg Default Rate", PCT1,
     "Unweighted mean of the sub-grade default rates on show: the scatter's average line.",
     "AVERAGEX ( ALLSELECTED ( Grade[Sub Grade] ), [Default Rate] )"),
    # ------------------------------------------------- Business analysis (D)
    ("Business Analysis", "Loss Share", PCT1, "Net credit loss in the row ÷ net credit loss of all states selected.",
     "DIVIDE ( [Net Credit Loss], CALCULATE ( [Net Credit Loss], ALLSELECTED ( State[State Code] ) ) )"),
    ("Business Analysis", "Cum Loss Share", PCT1,
     "Pareto: share of net credit loss carried by this state and every state with a bigger loss (WINDOW over the "
     "states selected, biggest first).",
     "DIVIDE (\n"
     "    CALCULATE ( [Net Credit Loss], WINDOW ( 1, ABS, 0, REL, ALLSELECTED ( State[State Code] ), ORDERBY ( [Net Credit Loss], DESC ) ) ),\n"
     "    CALCULATE ( [Net Credit Loss], ALLSELECTED ( State[State Code] ) )\n)"),
    ("Business Analysis", "Cum Funded Share", PCT1,
     "Share of funded amount carried by this purpose and every purpose with more funding (WINDOW over the purposes selected).",
     "DIVIDE (\n"
     "    CALCULATE ( [Funded Amount], WINDOW ( 1, ABS, 0, REL, ALLSELECTED ( Purpose[Purpose] ), ORDERBY ( [Funded Amount], DESC ) ) ),\n"
     "    CALCULATE ( [Funded Amount], ALLSELECTED ( Purpose[Purpose] ) )\n)"),
    ("Business Analysis", "Purpose ABC", None,
     "ABC class by funded amount within the current slicers: A = the purposes that reach the first 80%, B = up to 95%, "
     "C = the long tail. A purpose belongs to the class in which its first dollar falls.",
     "VAR _before = [Cum Funded Share] - DIVIDE ( [Funded Amount], CALCULATE ( [Funded Amount], ALLSELECTED ( Purpose[Purpose] ) ) )\n"
     "RETURN\n    IF ( NOT ISBLANK ( [Funded Amount] ),\n"
     "        SWITCH ( TRUE (), _before < 0.80, \"A\", _before < 0.95, \"B\", \"C\" ) )"),
    ("Business Analysis", "Default Rate by DTI Segment", PCT1,
     "Default rate of loans whose DTI falls in the segment row ('DTI Segments' is disconnected: edit its rows to re-band).",
     "VAR _lo = MIN ( 'DTI Segments'[Min DTI] )\nVAR _hi = MAX ( 'DTI Segments'[Max DTI] )\n"
     "RETURN CALCULATE ( [Default Rate], Loans[DTI] >= _lo, Loans[DTI] < _hi )"),
    ("Business Analysis", "Loans by DTI Segment", NUM0, "Matured loans in the DTI segment.",
     "VAR _lo = MIN ( 'DTI Segments'[Min DTI] )\nVAR _hi = MAX ( 'DTI Segments'[Max DTI] )\n"
     "RETURN CALCULATE ( [Matured Loans], Loans[DTI] >= _lo, Loans[DTI] < _hi )"),
    ("Business Analysis", "Prepayment Rate", PCT1,
     "Lender 'churn': share of matured loans repaid in full before the scheduled end of the term "
     "(last payment month earlier than issue month + term).",
     "LC.MaturedOnly (\n    DIVIDE (\n"
     "        COUNTROWS ( FILTER ( Loans, Loans[Outcome] = \"Fully Paid\"\n"
     "            && Loans[Last Payment Date] < EDATE ( Loans[Issue Date], Loans[Term Months] ) ) ),\n"
     "        [Loans Issued]\n    )\n)"),
    ("Business Analysis", "Lifecycle Loans", NUM0,
     "Loans reaching each funnel stage: issued → term ended → resolved → defaulted → anything recovered.",
     "SWITCH ( SELECTEDVALUE ( 'Funnel Stage'[Stage Sort] ),\n"
     "    1, [Loans Issued],\n"
     "    2, CALCULATE ( [Loans Issued], Loans[Is Matured] = TRUE () ),\n"
     "    3, [Matured Loans],\n"
     "    4, [Matured Defaults],\n"
     "    5, CALCULATE ( [Matured Defaults], Loans[Net Recoveries Amt] > 0 )\n)"),
    ("Business Analysis", "Mix Effect", "+0.00%;-0.00%;0.00%",
     "Change in default rate from the latest matured vintage year in the filter vs the year before, caused by the grade "
     "mix: Σ (weight now − weight before) × rate before. Whole issue years (the year slicer only picks the year).",
     MIX_EFFECT),
    ("Business Analysis", "Rate Effect", "+0.00%;-0.00%;0.00%",
     "Change in default rate caused by the rates inside each grade: Σ weight now × (rate now − rate before). "
     "Mix Effect + Rate Effect = the total change.",
     RATE_EFFECT),
    ("Business Analysis", "Default Rate Change", "+0.00%;-0.00%;0.00%",
     "Default rate of the latest matured vintage year in the filter minus the year before (whole years).",
     YEAR_BLOCK + "RETURN\n    IF ( _n0 > 0, DIVIDE ( "
     + "CALCULATE ( [Matured Defaults], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y ), _n1 ) - DIVIDE ( "
     + "CALCULATE ( [Matured Defaults], REMOVEFILTERS ( 'Date' ), Loans[Vintage Year] = _y - 1 ), _n0 ) )"),
    ("Business Analysis", "Variance Step Value", "+0.00%;-0.00%;0.00%", "Waterfall step: Mix Effect or Rate Effect.",
     "SWITCH ( SELECTEDVALUE ( 'Variance Step'[Step Sort] ), 1, [Mix Effect], 2, [Rate Effect] )"),
    ("Business Analysis", "Variance Title", None, "Waterfall title naming the two vintage years compared.",
     "VAR _y = CALCULATE ( MAX ( Loans[Vintage Year] ), Loans[Is Matured] = TRUE () )\n"
     "RETURN \"Why the default rate moved: \" & _y - 1 & \" → \" & _y & \" vintage\""),
    *[("Business Analysis", f"Sub Grade Rate P{p}", PCT2,
       f"{p}th percentile of the interest rate inside the sub-grade (PERCENTILEX.INC over its loans).",
       f"PERCENTILEX.INC ( Loans, Loans[Interest Rate], {p / 100} )") for p in (5, 95)],
    ("Business Analysis", "Pricing Outliers", NUM0,
     "Loans priced outside their own sub-grade's P5–P95 rate band. Mostly rate-card changes over the years, not mistakes.",
     "SUMX ( VALUES ( Grade[Sub Grade] ),\n"
     "    VAR _lo = [Sub Grade Rate P5]\n    VAR _hi = [Sub Grade Rate P95]\n"
     "    RETURN CALCULATE ( COUNTROWS ( Loans ), Loans[Interest Rate] < _lo || Loans[Interest Rate] > _hi )\n)"),
    ("Business Analysis", "Pricing Outlier Share", PCT1, "Pricing Outliers ÷ loans issued.", "DIVIDE ( [Pricing Outliers], [Loans Issued] )"),
    ("Business Analysis", "Funded Trend", USD0,
     "Least-squares line through monthly funded amount (LINESTX over the months selected), read at this month.",
     "VAR _pts =\n    FILTER (\n        ADDCOLUMNS ( ALLSELECTED ( 'Date'[Month Start] ),\n"
     "            \"@x\", YEAR ( 'Date'[Month Start] ) * 12 + MONTH ( 'Date'[Month Start] ),\n"
     "            \"@y\", [Funded Amount] ),\n        NOT ISBLANK ( [@y] )\n    )\n"
     "VAR _fit = LINESTX ( _pts, [@y], [@x] )\nVAR _d = MAX ( 'Date'[Month Start] )\n"
     "RETURN\n    IF ( NOT ISBLANK ( [Funded Amount] ),\n"
     "        MAXX ( _fit, [Intercept] ) + MAXX ( _fit, [Slope1] ) * ( YEAR ( _d ) * 12 + MONTH ( _d ) ) )"),
    *[("Business Analysis", f"Cohort {y}", PCT1, f"Cohort Default by MOB for the {y} issue-year cohort.",
       f"CALCULATE ( [Cohort Default by MOB], Loans[Vintage Year] = {y} )") for y in range(2010, 2016)],
    ("Scenario", "Stress Uplift", "+0.0%;-0.0%;0.0%", "Stressed expected loss ÷ expected loss − 1.",
     "DIVIDE ( [Stressed Expected Loss], [Expected Loss (DAX)] ) - 1"),
    ("Visual Helpers", "Grade ROI P25", PCT2,
     "25th percentile of the grade ROIs on show: the ROI chart's percentile line.",
     "PERCENTILEX.INC ( ALLSELECTED ( Grade[Grade] ), [Realised ROI], 0.25 )"),
]


def kit_tests(con):
    """pbi-kit DAX tests (14-s9): name.dax + name.assert.json. The expected values are the same numbers the DuckDB
    pipeline asserts in sql/90_tests.sql and pd_model.py, read from the warehouse at build time. CI checks them
    offline (syntax, model references, assert files); online they run through executeQueries on the Test workspace."""
    one = lambda sql: con.execute(sql).fetchone()
    mat = "is_resolved and is_matured"
    n, funded, dr, mfunded, profit, bridge = one(f"""
        select count(*), sum(funded_amount), sum(is_default) filter (where {mat}) / count(*) filter (where {mat}),
               sum(funded_amount) filter (where {mat}), sum(net_profit) filter (where {mat}),
               sum(principal_paid + interest_paid + late_fees + net_recoveries - funded_amount) filter (where {mat})
        from mart.fact_loans where funded_amount > 0""")
    auc, gini = one("select auc, gini from mart.model_metrics")
    tol = round(0.0001 * mfunded)  # 90_tests.sql: bridge vs net profit within 0.01% of funded
    chk = lambda col, v, t=0: {"column": f"[{col}]", "equals": v, "tolerance": t}
    js = lambda d: json.dumps(d, indent=2, ensure_ascii=False) + "\n"
    head = "// pbi-kit CI test, generated by src/build_model.py (kit_tests). Expected values: assert file next to this one.\n"
    return {
        "ci-key-measures.dax": head + "// Headline numbers vs the DuckDB pipeline: row count, funded $ (to the dollar), matured default rate, PD model AUC/Gini.\n"
        "EVALUATE\nROW (\n    \"Loans Issued\", [Loans Issued],\n    \"Funded Amount\", [Funded Amount],\n"
        "    \"Default Rate\", [Default Rate],\n    \"AUC\", [AUC],\n    \"Gini\", [Gini]\n)\n",
        "ci-key-measures.assert.json": js({"rows": 1, "checks": [
            chk("Loans Issued", n), chk("Funded Amount", round(funded), 1), chk("Default Rate", round(dr, 6), 0.00005),
            chk("AUC", round(auc, 4), 0.0005), chk("Gini", round(gini, 4), 0.001)]}),
        "ci-cash-bridge.dax": head + "// Cash bridge (funded out, principal + interest + late fees + recoveries in) must add up to Net Profit on matured loans.\n"
        "EVALUATE\nROW (\n    \"Bridge Total\", SUMX ( VALUES ( 'Cash Bridge'[Step Sort] ), [Cash Bridge Amount] ),\n"
        "    \"Matured Net Profit\", LC.MaturedOnly ( [Net Profit] )\n)\n",
        "ci-cash-bridge.assert.json": js({"rows": 1, "checks": [
            chk("Bridge Total", round(bridge), tol), chk("Matured Net Profit", round(profit), tol)]}),
        "ci-data-integrity.dax": head + "// Mirrors sql/90_tests.sql inside the model: every count must be 0.\n"
        "EVALUATE\nROW (\n    \"Duplicate Loan IDs\", COUNTROWS ( Loans ) - DISTINCTCOUNT ( Loans[Loan ID] ),\n"
        "    \"Loans Without Grade\", COUNTROWS ( FILTER ( Loans, ISBLANK ( RELATED ( Grade[Sub Grade] ) ) ) ) + 0,\n"
        "    \"Loans Without State\", COUNTROWS ( FILTER ( Loans, ISBLANK ( RELATED ( State[State Code] ) ) ) ) + 0,\n"
        "    \"Loans Without Purpose\", COUNTROWS ( FILTER ( Loans, ISBLANK ( RELATED ( Purpose[Purpose Code] ) ) ) ) + 0,\n"
        "    \"PD Out Of Range\", COUNTROWS ( FILTER ( Loans, ISBLANK ( Loans[PD] ) || Loans[PD] < 0 || Loans[PD] > 1 ) ) + 0,\n"
        "    \"Rates Out Of Range\", COUNTROWS ( FILTER ( Loans, Loans[Interest Rate] < 0.05 || Loans[Interest Rate] > 0.31 ) ) + 0,\n"
        "    \"LGD Out Of Range\", COUNTROWS ( FILTER ( 'Grade LGD', 'Grade LGD'[Grade LGD Rate] < 0 || 'Grade LGD'[Grade LGD Rate] > 1 ) ) + 0\n)\n",
        "ci-data-integrity.assert.json": js({"rows": 1, "checks": [chk(c, 0) for c in (
            "Duplicate Loan IDs", "Loans Without Grade", "Loans Without State", "Loans Without Purpose",
            "PD Out Of Range", "Rates Out Of Range", "LGD Out Of Range")]}),
    }


def dax_queries(con):
    """DAX query view files (<model>/DAXQueries). Expected results are computed here from the DuckDB warehouse
    with the same definitions as the measures; none of these has been run in Power BI (no Desktop on macOS)."""
    q_ = lambda sql: con.execute(sql).fetchall()
    base = "from mart.fact_loans f join mart.dim_grade g using (sub_grade) where f.funded_amount > 0"
    dr = q_(f"""select g.grade, f.term, avg(f.is_default) filter (where f.is_resolved and f.is_matured) {base}
                group by all order by 1, 2""")
    r60 = {g: v for g, t, v in dr if t == "60 months"}
    pct = lambda v: "(blank)" if v is None else f"{v:.1%}"
    kf = "\n".join(f"//   {g}  {t}   Default Rate {pct(v)}   60M KEEPFILTERS {pct(v if t == '60 months' else None)}"
                   f"   60M Replace {pct(r60.get(g))}" for g, t, v in dr)
    el = q_(f"""select g.grade, sum(f.expected_loss),
                       sum(f.outstanding_principal * f.pd * l.lgd) filter (where f.outcome in ('Current', 'Delinquent'))
                {base.replace('using (sub_grade)', 'using (sub_grade) join mart.grade_lgd l on l.grade = g.grade')}
                group by 1 order by 1""")
    elt = "\n".join(f"//   {g}  Expected Loss {a:,.0f}   Expected Loss (DAX) {b:,.0f}   Gap {round(b - a) + 0:,}" for g, a, b in el)
    m2m = q_("""select g.risk_tier, v.mob, sum(v.defaults) / sum(v.loans)
                from mart.vintage_curve v join (select distinct grade, risk_tier from mart.dim_grade) g using (grade)
                where v.vintage = '2013–14' and v.mob in (12, 24, 36) group by all order by 1, 2""")
    m2t = "\n".join(f"//   {t}  MOB {m}   {pct(r)} (both measures)" for t, m, r in m2m)
    coh = q_("""select v.mob, v.cum_default_rate,
                       (select count(*) filter (where is_default = 1 and months_to_default <= v.mob) / count(*)
                        from mart.fact_loans where term = '36 months' and is_matured and vintage_year in (2013, 2014))
                from mart.vintage_curve v where v.vintage = '2013–14' and v.grade = 'All' and v.mob in (12, 24, 36) order by 1""")
    coht = "\n".join(f"//   MOB {m}   Vintage Curve {pct(a)}   Cohort Default by MOB {pct(b)}" for m, a, b in coh)
    mr = q_(f"""with t as (select g.grade, f.vintage_year y, count(*) filter (where is_resolved and is_matured) n,
                                  sum(is_default) filter (where is_resolved and is_matured) d {base} and f.vintage_year in (2014, 2015)
                           group by all),
                     w as (select grade, y, n / sum(n) over (partition by y) w, d / n r from t)
                select sum((a.w - b.w) * b.r), sum(a.w * (a.r - b.r)), sum(a.w * a.r) - sum(b.w * b.r)
                from w a join w b using (grade) where a.y = 2015 and b.y = 2014""")[0]
    return kit_tests(con) | {
        "cohort-reconcile.dax": f"""// DAX cohort vs the SQL vintage curve (10-s7). Cohort Default by MOB recomputes, from loan rows, the share of
// matured 36-month loans defaulted by month on book k (disconnected MOB table). Pooled over vintage years 2013–14 it
// must equal 'Vintage Curve' for the 2013–14 cohort, all grades (every 36-month loan of 2013–14 has matured).
// Expected (DuckDB):
{coht}
// Not run yet: Power BI Desktop is not available on the build machine.
EVALUATE
CALCULATETABLE (
    ADDCOLUMNS (
        FILTER ( VALUES ( MOB[Month On Book] ), MOB[Month On Book] IN {{ 12, 24, 36 }} ),
        "Cohort Default by MOB", [Cohort Default by MOB],
        "Vintage Curve", CALCULATE ( MAX ( 'Vintage Curve'[Cum Default Rate] ), 'Vintage Curve'[Vintage] = "2013–14",
            'Vintage Curve'[Vintage Grade] = "All", TREATAS ( {{ MOB[Month On Book] }}, 'Vintage Curve'[Months on Book] ) )
    ),
    Loans[Vintage Year] IN {{ 2013, 2014 }}
)
""",
        "mix-rate-effect.dax": f"""// Mix vs rate effect (10-s13): default-rate change 2014 → 2015 vintage split into grade mix and rate within grade.
// Expected (DuckDB, matured loans, weights = share of matured loans per grade):
//   Mix Effect {mr[0]:+.2%}   Rate Effect {mr[1]:+.2%}   Default Rate Change {mr[2]:+.2%}   (mix + rate = change)
// Not run yet: Power BI Desktop is not available on the build machine.
EVALUATE
ROW ( "Mix Effect", [Mix Effect], "Rate Effect", [Rate Effect], "Default Rate Change", [Default Rate Change] )
""",
        "keepfilters.dax": f"""// Replace vs add (6-s3, 9-s2). The row already filters Term; KEEPFILTERS intersects with it
// (blank on 36-month rows), a plain filter argument overwrites it (every row shows the 60-month rate).
// Expected (DuckDB, matured resolved funded loans):
{kf}
// Not run yet: Power BI Desktop is not available on the build machine.
EVALUATE
SUMMARIZECOLUMNS (
    Grade[Grade],
    Loans[Term],
    "Default Rate", [Default Rate],
    "60M KEEPFILTERS", [Default Rate 60M],
    "60M Replace", [Default Rate 60M (Replace)]
)
ORDER BY Grade[Grade], Loans[Term]
""",
        "expected-loss-reconcile.dax": f"""// Row context + RELATED over two hops (3-s9): Expected Loss (DAX) recomputes outstanding x PD x grade LGD
// loan by loan through Loans -> Grade -> 'Grade LGD' and must match the SQL column summed by Expected Loss.
// Expected (DuckDB):
{elt}
// Not run yet: Power BI Desktop is not available on the build machine.
EVALUATE
SUMMARIZECOLUMNS (
    Grade[Grade],
    "Expected Loss", [Expected Loss],
    "Expected Loss (DAX)", [Expected Loss (DAX)],
    "Gap", [Expected Loss Gap]
)
ORDER BY Grade[Grade]
""",
        "m2m-vs-treatas.dax": f"""// Many-to-many relationship vs TREATAS (11-s3, 9-s4). Risk Tier filters Grade, which reaches 'Vintage Curve'
// through the m2m relationship on the grade letter; the TREATAS measure switches that relationship off
// (CROSSFILTER NONE) and builds the same filter at query time. Both columns must be equal on every row.
// Expected (DuckDB, 2013–14 vintage, pooled defaults / pooled loans of the tier's grades):
{m2t}
// Not run yet: Power BI Desktop is not available on the build machine.
EVALUATE
CALCULATETABLE (
    SUMMARIZECOLUMNS (
        Grade[Risk Tier],
        'Vintage Curve'[Months on Book],
        "m2m relationship", [Cumulative Default Rate],
        "TREATAS", [Vintage Cum Default (Tier)]
    ),
    'Vintage Curve'[Vintage] = "2013–14",
    'Vintage Curve'[Months on Book] IN {{ 12, 24, 36 }}
)
ORDER BY Grade[Risk Tier], 'Vintage Curve'[Months on Book]
""",
        "ols-test.dax": """// Object- and column-level security test (11-s13, 11-s14). In DAX query view pick View as > Business Viewer.
// Expected: query 1 fails (Loans[Annual Income] cannot be found), query 2 fails ('Model ROC' cannot be found),
// query 3 returns the default rate per grade (the role still sees every loan).
// Not run yet: Power BI Desktop is not available on the build machine.
EVALUATE
TOPN ( 5, SELECTCOLUMNS ( Loans, "Income", Loans[Annual Income] ) )

EVALUATE
TOPN ( 5, 'Model ROC' )

EVALUATE
SUMMARIZECOLUMNS ( Grade[Grade], "Default Rate", [Default Rate] )
""",
    }


def pretty(col):
    return " ".join(w.upper() if w in ("id", "ead", "lgd") else w.capitalize() for w in col.split("_"))


def q(name):
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else f"'{name}'"


def lineage(*key):
    """Deterministic lineageTag (uuid5 of the object path): a rebuild leaves unchanged objects byte-identical, so
    `git diff` on the TMDL shows only real edits (12-s7). Random uuid4 tags made every rebuild touch every object."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "lendingclub-risk/" + "/".join(map(str, key))))


def col_name(cfg, col):
    return cfg.get("names", {}).get(col) or pretty(col)


def disp(table, col):
    """Display name of a column given by its mart name (imported tables) or display name (other tables)."""
    return col_name(TABLES[table][1], col) if table in TABLES else col


def schema(con, mart):
    return con.execute(f"describe mart.{mart}").fetchall()


def dax_block(dax, indent=3):
    return [f"{T*indent}{l}" for l in dax.split("\n")]


def mode_of(name, fabric):
    return FABRIC_MODE.get(name, "import") if fabric else "import"


def dropped(cfg, mode):
    """Mart columns left out: report-unused ones, plus columns a calculated column replaces (Import only)."""
    return set(cfg.get("drop", ())) | (set(cfg.get("calc_replaces", ())) if mode == "import" else set())


def m_steps(mart, cfg, m_types, fabric=False, mode="import"):
    """Partition query. Filters and column picks sit right after Source: Parquet does not fold, so the
    earlier rows/columns are dropped the less every later step has to touch. On the Lakehouse SQL endpoint
    (fabric) the same steps fold into the SELECT; DirectQuery tables keep only foldable steps."""
    q_ = '"'
    if fabric:
        steps = [("Source", "Sql.Database(SqlEndpoint, LakehouseDb)"),
                 ('#"Navigated Table"', f'Source{{[Schema = "dbo", Item = "{mart}"]}}[Data]')]
    else:
        steps = [("Source", f'Parquet.Document(File.Contents(DataFolder & "{mart}.parquet"))')]
    drop = dropped(cfg, mode)
    if drop:
        kept = [c for c, *_ in cfg["_schema"] if c not in drop]
        steps.append(('#"Kept Report Columns"', "Table.SelectColumns({prev}, {" + ", ".join(q_ + c + q_ for c in kept) + "})"))
    if cfg.get("row_filter"):
        steps.append(('#"Kept Funded Loans"', "Table.SelectRows({prev}, " + cfg["row_filter"] + ")"))
    steps.append(('#"Changed Type"', "Table.TransformColumnTypes({prev}, {" + ", ".join(m_types) + "})"))
    if (r := cfg.get("refresh")) and mode == "import":
        c = r["column"]
        steps.append(('#"Filtered Refresh Range"', f"Table.SelectRows({{prev}}, each DateTime.From([{c}]) >= RangeStart "
                                                   f"and DateTime.From([{c}]) < RangeEnd)"))
    if mode == "import":
        steps += [st for st in cfg.get("steps", []) if not (fabric and "Field Glossary" in st[1]) and
                  not (fabric and st[0] in ('#"Expanded Description"', "LcFieldOf", '#"Added LC Field"'))]
    lines, prev = ["let"], None
    for i, (name, expr) in enumerate(steps):
        sep = "," if i < len(steps) - 1 else ""
        lines.append(f"    {name} = {expr.replace('{prev}', prev)}{sep}" if prev else f"    {name} = {expr}{sep}")
        if name.startswith('#"') or name == "Source":  # helper values (records) don't advance the chain
            prev = name
    return lines + ["in", f"    {prev}"]


def extra_cols(cfg, fabric, mode):
    if mode != "import":
        return []
    return [c for c in cfg.get("extra_cols", []) if not (fabric and c[1] in ("lc_field", "feature_description"))]


def table_tmdl(con, name, mart, cfg, fabric=False):
    mode = mode_of(name, fabric)
    lines = [f"table {q(name)}", f"{T}lineageTag: {lineage("table", name)}"]
    if cfg.get("date_table"):
        lines.append(f"{T}dataCategory: Time")
    lines.append("")
    m_types = []
    cfg["_schema"] = schema(con, mart)
    drop = dropped(cfg, mode)
    for col, dtype, *_ in cfg["_schema"]:
        if col in drop:
            continue
        tm, mq = TYPES[dtype]
        m_types.append(f'{{"{col}", {mq}}}')
        lines += [f"{T}column {q(col_name(cfg, col))}", f"{T*2}dataType: {tm}"]
        if col == cfg.get("key"):
            lines.append(f"{T*2}isKey")
        if col in cfg.get("hidden", []):
            lines.append(f"{T*2}isHidden")
        fmt = cfg.get("fmt", {}).get(col) or {"int64": "0", "dateTime": "dd mmm yyyy"}.get(tm)
        if col in cfg.get("sum", []) and tm == "double":
            fmt = USD0
        if fmt:
            lines.append(f"{T*2}formatString: {fmt}")
        if cat := cfg.get("category", {}).get(col):
            lines.append(f"{T*2}dataCategory: {cat}")
        lines += [f"{T*2}{e}" for e in cfg.get("col_extra", {}).get(col, [])]
        lines += [f"{T*2}lineageTag: {lineage("column", name, col)}",
                  f"{T*2}summarizeBy: none",  # BPA NUMERIC_COLUMN_SUMMARIZE_BY: model discourages implicit measures anyway
                  f"{T*2}sourceColumn: {col}"]
        if sort := cfg.get("sort", {}).get(col):
            lines.append(f"{T*2}sortByColumn: {q(col_name(cfg, sort))}")
        lines.append("")
    for cname, src, dtype, extra in extra_cols(cfg, fabric, mode):
        lines += [f"{T}column {q(cname)}", f"{T*2}dataType: {dtype}", f"{T*2}lineageTag: {lineage("column", name, cname)}",
                  f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: {src}", *[f"{T*2}{e}" for e in extra], ""]
    # Calculated columns need Import storage and Import neighbours; the composite variant leaves them out.
    for cname, dtype, fmt, dax, extra in (cfg.get("calc_cols", []) if not fabric else []):
        lines += [f"{T}column {q(cname)} =", *dax_block(dax)] if "\n" in dax else [f"{T}column {q(cname)} = {dax}"]
        lines += [f"{T*2}dataType: {dtype}"] + ([f"{T*2}formatString: {fmt}"] if fmt else [])
        lines += [f"{T*2}lineageTag: {lineage("column", name, cname)}", f"{T*2}summarizeBy: none"]
        lines += [f"{T*2}{e}" if e and not e.startswith("\t") else (f"{T*2}{e}" if e else "") for e in extra] + [""]
    for h, levels in HIERARCHIES.get(name, []):
        lines += [f"{T}hierarchy {q(h)}", f"{T*2}lineageTag: {lineage("hierarchy", name, h)}", ""]
        for lv in levels:
            lines += [f"{T*2}level {q(lv)}", f"{T*3}lineageTag: {lineage("level", name, h, lv)}", f"{T*3}column: {q(lv)}", ""]
    if cfg.get("date_table"):
        for cal, groups in CALENDARS:
            lines += [f"{T}calendar {q(cal)}", f"{T*2}lineageTag: {lineage("calendar", name, cal)}", ""]
            for unit, primary, assoc in groups:
                lines += [f"{T*2}calendarColumnGroup = {unit}", f"{T*3}primaryColumn: {q(primary)}"]
                lines += ([f"{T*3}associatedColumn: {q(assoc)}"] if assoc else []) + [""]
    m = m_steps(mart, cfg, m_types, fabric, mode)
    if (r := cfg.get("refresh")) and mode == "import":
        (rg, rp), (ig, ip) = r["rolling"], r["incremental"]
        lines += [f"{T}refreshPolicy", f"{T*2}policyType: basic",
                  f"{T*2}rollingWindowGranularity: {rg}", f"{T*2}rollingWindowPeriods: {rp}",
                  f"{T*2}incrementalGranularity: {ig}", f"{T*2}incrementalPeriods: {ip}",
                  f"{T*2}sourceExpression =", *[f"{T*4}{l}" for l in m], ""]
    lines += [f"{T}partition {q(name)} = m", f"{T*2}mode: {mode}", f"{T*2}source ="]
    lines += [f"{T*4}{l}" for l in m]
    lines += ["", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def m_table_tmdl(name, cols, steps):
    lines = [f"table {q(name)}", f"{T}lineageTag: {lineage("table", name)}", ""]
    for col, src, dtype, extra in cols:
        lines += [f"{T}column {q(col)}", f"{T*2}dataType: {dtype}", f"{T*2}lineageTag: {lineage("column", name, col)}",
                  f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: {src}", *[f"{T*2}{e}" for e in extra], ""]
    lines += [f"{T}partition {q(name)} = m", f"{T*2}mode: import", f"{T*2}source =",
              *[f"{T*4}{l}" for l in ["let", *steps]], "", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def calculated_tmdl(name, dax, cols):
    lines = [f"table {q(name)}"] + ([f"{T}isHidden"] if name in CALC_HIDDEN else []) + [f"{T}lineageTag: {lineage("table", name)}", ""]
    for col, src, dtype, extra in cols:
        lines += [f"{T}column {q(col)}", f"{T*2}dataType: {dtype}", f"{T*2}lineageTag: {lineage("column", name, col)}",
                  f"{T*2}summarizeBy: none", f"{T*2}isNameInferred", f"{T*2}sourceColumn: {src}"]
        lines += [f"{T*2}{e}" if e and not e.startswith("\t") else (f"{T*2}{e}" if e else "") for e in extra] + [""]
    lines += [f"{T}partition {q(name)} = calculated", f"{T*2}mode: import", f"{T*2}source = {dax}", ""]
    return "\n".join(lines)


def loans_agg_tmdl():
    """Import aggregation over the DirectQuery Loans; the engine rewrites matching queries to it (alternateOf)."""
    lines = ["table 'Loans Agg'", f"{T}isHidden", f"{T}lineageTag: {lineage("table", "Loans Agg")}", ""]
    for col, src, dtype, summ, base in LOANS_AGG:
        lines += [f"{T}column {q(col)}", f"{T*2}dataType: {dtype}", f"{T*2}isHidden",
                  f"{T*2}lineageTag: {lineage("column", "Loans Agg", col)}", f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: {src}", "",
                  f"{T*2}alternateOf", f"{T*3}summarization: {summ}"]
        lines += [f"{T*3}baseColumn: Loans.{q(base)}"] if base else [f"{T*3}baseTable: Loans"]
        lines.append("")
    keys = [src for _, src, _, s, _ in LOANS_AGG if s == "groupBy"]
    m = ["let", "    Source = Sql.Database(SqlEndpoint, LakehouseDb),",
         '    #"Navigated Table" = Source{[Schema = "dbo", Item = "fact_loans"]}[Data],',
         '    #"Kept Funded Loans" = Table.SelectRows(#"Navigated Table", each [funded_amount] > 0),',
         f'    #"Grouped Rows" = Table.Group(#"Kept Funded Loans", {{{", ".join(chr(34) + k + chr(34) for k in keys)}}}, {{',
         '        {"funded_amount", each List.Sum([funded_amount]), type number},',
         '        {"loans", each Table.RowCount(_), Int64.Type}})', "in", '    #"Grouped Rows"']
    lines += [f"{T}partition 'Loans Agg' = m", f"{T*2}mode: import", f"{T*2}source =", *[f"{T*4}{l}" for l in m],
              "", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def relationship_kind(ft, tt, fabric):
    """Regular when both ends sit in one source group; Dual counts as both."""
    def groups(t):
        mode = mode_of(t, fabric)
        return {"import", "directQuery"} if mode == "dual" else {mode}
    many, one = groups(ft), groups(tt)
    return "regular" if many & one == many or one >= many else "limited"


def calc_group_tmdl(table, column, precedence, items):
    lines = [f"table {q(table)}", f"{T}lineageTag: {lineage("table", table)}", "", f"{T}calculationGroup", f"{T*2}precedence: {precedence}", ""]
    for i, (name, dax, fmt) in enumerate(items):
        lines += [f"{T*2}calculationItem {q(name)} =", *dax_block(dax, 4)] if "\n" in dax \
            else [f"{T*2}calculationItem {q(name)} = {dax}"]
        lines.append(f"{T*3}ordinal: {i}")
        if fmt:
            lines.append(f"{T*3}formatStringDefinition = {fmt}")
        lines.append("")
    lines += [f"{T}column {q(column)}", f"{T*2}dataType: string", f"{T*2}lineageTag: {lineage("column", table, column)}",
              f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: Name", f"{T*2}sortByColumn: Ordinal", "",
              f"{T}column Ordinal", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
              f"{T*2}lineageTag: {lineage("column", table, "Ordinal")}", f"{T*2}summarizeBy: sum", f"{T*2}sourceColumn: Ordinal", ""]
    return "\n".join(lines)


def field_parameter_tmdl(t, items):
    rows = ",\n".join(f'    ( "{label}", NAMEOF ( {ref} ), {i} )' for i, (label, ref) in enumerate(items))
    return "\n".join([
        f"table {q(t)}", f"{T}lineageTag: {lineage("table", t)}", "",
        f"{T}column {q(t)}", f"{T*2}dataType: string", f"{T*2}lineageTag: {lineage("column", t, t)}",
        f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: [Value1]", f"{T*2}sortByColumn: '{t} Order'", "",
        f"{T*2}relatedColumnDetails", f"{T*3}groupByColumn: '{t} Fields'", "",
        f"{T}column '{t} Fields'", f"{T*2}dataType: string", f"{T*2}isHidden", f"{T*2}lineageTag: {lineage("column", t, f"{t} Fields")}",
        f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: [Value2]", f"{T*2}sortByColumn: '{t} Order'", "",
        f"{T*2}extendedProperty ParameterMetadata =", f"{T*4}{{", f'{T*4}  "version": 3,', f'{T*4}  "kind": 2',
        f"{T*4}}}", "",
        f"{T}column '{t} Order'", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
        f"{T*2}lineageTag: {lineage("column", t, f"{t} Order")}", f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: [Value3]", "",
        f"{T}partition {q(t)} = calculated", f"{T*2}mode: import", f"{T*2}source =",
        *dax_block("{\n" + rows + "\n}", 4), ""])


# Measures that return text or a colour hex: BPA format-string rules are ignored on these by annotation.
TEXT_MEASURES = {"Default Rate Color", "ROI Color", "Funded YoY Label", "Headline Insight", "Period Label",
                 "Purpose ABC", "Variance Title", "Worst State", "Worst Sub Grade"}


def measures_tmdl(measures):
    lines = ["table _Measures", f"{T}lineageTag: {lineage("table", "_Measures")}", ""]
    for folder, name, fmt, desc, dax, *fmt_dax in measures:
        if desc:
            lines.append(f"{T}/// {desc}")
        if "\n" in dax:
            lines += [f"{T}measure {q(name)} =", *dax_block(dax)]
        else:
            lines.append(f"{T}measure {q(name)} = {dax}")
        if fmt:
            lines.append(f"{T*2}formatString: {fmt}")
        lines += [f"{T*2}displayFolder: {folder}", f"{T*2}lineageTag: {lineage("measure", name)}"]
        if fmt_dax:
            lines += [f"{T*2}formatStringDefinition =", *dax_block(fmt_dax[0], 4)]
        if name in TEXT_MEASURES:  # BPA: a text/colour result has no number format to provide (deliberate ignore)
            lines += ["", f'{T*2}annotation BestPracticeAnalyzer_IgnoreRules = {{"RuleIDs":["PROVIDE_FORMAT_STRING_FOR_MEASURES","INTEGER_FORMATTING"]}}']
        lines.append("")
    lines += [f"{T}column Value", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
              f"{T*2}lineageTag: {lineage("column", "_Measures", "Value")}", f"{T*2}summarizeBy: none", f"{T*2}isNameInferred",
              f"{T*2}sourceColumn: [Value]", "",
              f"{T}partition _Measures = calculated", f"{T*2}mode: import", f"{T*2}source = {{1}}", ""]
    return "\n".join(lines)


def functions_tmdl():
    lines = []
    for name, desc, body in FUNCTIONS:
        lines.append(f"/// {desc}")
        lines += [f"function {q(name)} =", *dax_block(body, 2)] if "\n" in body else [f"function {q(name)} = {body}"]
        lines += [f"{T}lineageTag: {lineage("function", name)}", ""]
    return "\n".join(lines)


def role_tmdl(name, r):
    lines = [f"role {q(name)}", f"{T}modelPermission: read", ""]
    for table in sorted(set(r["filters"]) | set(r["hide_columns"])):
        dax = r["filters"].get(table)
        lines.append(f"{T}tablePermission {q(table)}" + (f" = {dax}" if dax else ""))
        lines += [""] + [f"{T*2}columnPermission {q(c)} = none" for c in r["hide_columns"].get(table, [])] + [""]
    for table in r["hide_tables"]:
        lines += [f"{T}tablePermission {q(table)}", f"{T*2}metadataPermission: none", ""]
    return "\n".join(lines)


def perspective_tmdl(name, tables):
    lines = [f"perspective {q(name)}", ""]
    for t in tables:
        lines += [f"{T}perspectiveTable {q(t)}", f"{T*2}includeAll", ""]
    return "\n".join(lines)


def culture_tmdl():
    entities = {}
    measure_names = {m[1] for m in MEASURES}
    for obj, terms in SYNONYMS.items():
        binding = {"ConceptualEntity": "_Measures", "ConceptualProperty": obj} if obj in measure_names \
            else {"ConceptualEntity": obj}
        entities[re.sub(r"\W", "_", obj.lower())] = {
            "Definition": {"Binding": binding}, "State": "Authored",
            "Terms": [{obj.lower(): {"State": "Authored"}}] + [{t: {"State": "Authored"}} for t in terms]}
    doc = {"Version": "1.0.0", "Language": "en-US", "DynamicImprovement": "HighConfidence", "Entities": entities}
    return "\n".join(["cultureInfo en-US", "", f"{T}linguisticMetadata = {json.dumps(doc, ensure_ascii=False)}",
                      f"{T*2}contentType: json", ""])


def model_columns(con, fabric=False):
    cols = {}
    for t, (mart, cfg) in TABLES.items():
        mode = mode_of(t, fabric)
        cols[t] = {col_name(cfg, c[0]) for c in schema(con, mart) if c[0] not in dropped(cfg, mode)} \
            | {c[0] for c in extra_cols(cfg, fabric, mode)} | ({c[0] for c in cfg.get("calc_cols", [])} if not fabric else set())
    cols |= {t: {c[0] for c in cs} for t, (cs, _) in M_TABLES.items() if not (fabric and t in LOCAL_ONLY)}
    cols |= {t: {c[0] for c in cs} for t, (_, cs) in CALCULATED.items()}
    cols |= {"Time Calc": {"Time Calc"}}
    if not fabric:
        cols |= {t: {t, f"{t} Fields", f"{t} Order"} for t in FIELD_PARAMS}
    return cols


def refs_ok(dax, columns, measures, tables, fns):
    """First unresolved reference in a DAX expression, or None."""
    dax = re.sub(r'"(?:[^"]|"")*"', '""', re.sub(r"--[^\n]*", "", dax))
    for a, b, col in re.findall(r"(?:'([^']+)'|\b([A-Za-z_][\w]*))\[([^\]]+)\]", dax):
        if col not in columns.get(a or b, ()):
            return f"column {a or b}[{col}]"
    for ref in re.findall(r"(?<![\w'\]])\[([^\]]+)\]", dax):
        if ref not in measures and ref not in ("Value", "Intercept", "Slope1") and not ref.startswith("@"):  # LINESTX output columns
            return f"measure [{ref}]"
    for ref in re.findall(r"'([^'\n]+)'", re.sub(r"'[^'\n]+'\[[^\]]*\]", "", dax)):
        if ref not in tables:
            return f"table/calendar '{ref}'"
    for fn in re.findall(r"\b([A-Z]+\.[A-Z][a-z][A-Za-z]*)\s*\(", dax):  # UDFs are LC.Mixed; PERCENTILEX.INC is built in
        if fn not in fns:
            return f"function {fn}"
    return None


def fabric_measures(con):
    """The composite variant has no calculated columns: measures that need one (directly or through another
    measure) are left out of it, and printed."""
    columns = model_columns(con, True)
    keep, names = [], {m[1] for m in MEASURES}
    tables, fns = set(columns) | {c for c, _ in CALENDARS}, {f[0] for f in FUNCTIONS}
    changed = True
    out = list(MEASURES)
    while changed:
        changed = False
        names = {m[1] for m in out}
        nxt = [m for m in out if refs_ok(m[4], columns, names, tables, fns) is None]
        changed = len(nxt) != len(out)
        out = nxt
    return out


def check(con):
    """Fail fast on name collisions, DAX references that don't resolve, and best-practice breaks."""
    for t, (mart, cfg) in TABLES.items():
        names = [col_name(cfg, c[0]).lower() for c in schema(con, mart) if c[0] not in dropped(cfg, "import")] \
            + [c[0].lower() for c in cfg.get("calc_cols", []) + cfg.get("extra_cols", [])]
        assert len(names) == len(set(names)), f"duplicate column names in {t}: {names}"
    columns = model_columns(con)
    measures = [m[1] for m in MEASURES]
    taken = {n.lower() for n in set().union(*columns.values(), columns)}
    clash = {m for m in measures if m.lower() in taken or measures.count(m) > 1}
    assert not clash, f"measure names collide with tables/columns: {clash}"
    tables, fns = set(columns) | {c for c, _ in CALENDARS}, {f[0] for f in FUNCTIONS}
    dax_all = [(m[1], m[4]) for m in MEASURES] + [(m[1] + " format", m[5]) for m in MEASURES if len(m) > 5] \
        + [(f"role {r}", d) for r, v in ROLES.items() for d in v["filters"].values()] \
        + [(f"calc {n}", d) for n, d, _ in TIME_ITEMS] + [(f"function {n}", b) for n, _, b in FUNCTIONS] \
        + [(f"table {n}", d) for n, (d, _) in CALCULATED.items()] \
        + [(f"field parameter {t}", " ".join(r for _, r in items)) for t, items in FIELD_PARAMS.items()] \
        + [(f"column {t}[{c[0]}]", c[3]) for t, (_, cfg) in TABLES.items() for c in cfg.get("calc_cols", [])]
    for name, dax in dax_all:
        bad = refs_ok(dax, columns, set(measures), tables, fns)
        assert bad is None, f"[{name}] references missing {bad}"
        assert dax.count("(") == dax.count(")"), f"[{name}] unbalanced parentheses"
    for _, groups in CALENDARS:
        assert all(p in columns["Date"] and (a is None or a in columns["Date"]) for _, p, a in groups)
    for t, hs in HIERARCHIES.items():
        assert all(lv in columns[t] for _, lvs in hs for lv in lvs), t
    for r in ROLES.values():
        assert all(t in columns for t in [*r["filters"], *r["hide_tables"], *r["hide_columns"]])
        assert all(c in columns[t] for t, cs in r["hide_columns"].items() for c in cs)
    for p, ts in PERSPECTIVES.items():
        assert all(t in columns or t == "_Measures" for t in ts), p
    assert all(o in measures or o in columns for o in SYNONYMS)
    for t, (mart, cfg) in TABLES.items():  # best practice: sort-by helpers are plumbing, never visible
        for sort_col in cfg.get("sort", {}).values():
            assert sort_col in cfg.get("hidden", []), f"{t}[{sort_col}] is a sort helper and should be hidden"
    for ft, fc, tt, tc, active, card in RELATIONSHIPS:
        if card != "m2m":
            assert TABLES[tt][1].get("key") == tc, f"relationship {ft}[{fc}] -> {tt}[{tc}] must point at a key column"
    print(f"  ✓ {len(MEASURES)} measures, {len(FUNCTIONS)} functions: every reference resolves; best-practice rules pass")


def folder(path):
    sep = "\\" if "\\" in path else "/"
    return path.rstrip("/\\") + sep


def write_model(con, out, fabric, data_dir):
    """fabric=False: local Parquet import (Desktop on any machine). fabric=True: Lakehouse SQL composite."""
    m_tables = {n: v for n, v in M_TABLES.items() if not (fabric and n in LOCAL_ONLY)}
    measures = fabric_measures(con) if fabric else MEASURES
    shutil.rmtree(out / "definition", ignore_errors=True)
    d = out / "definition"
    (d / "tables").mkdir(parents=True)
    for sub in ("roles", "perspectives", "cultures"):
        (d / sub).mkdir()
    display = "LendingClub (Fabric)" if fabric else "LendingClub"
    logical = "7d4b2e91-3c6a-4f85-b0d2-9e1a5c3f7b64" if fabric else "3c1f7a52-8d4e-4b6a-9f21-5e0d8c7b4a13"
    (out / "definition.pbism").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",\n'
        '  "version": "4.2",\n  "settings": {}\n}\n')
    (out / ".platform").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",\n'
        f'  "metadata": {{"type": "SemanticModel", "displayName": "{display}"}},\n'
        f'  "config": {{"version": "2.0", "logicalId": "{logical}"}}\n}}\n')
    # 1702: user-defined functions and calendars need the current compatibility level.
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1702\n")
    all_tables = [*TABLES, *(["Loans Agg"] if fabric else []), *m_tables, *CALCULATED, *([] if fabric else FIELD_PARAMS), "Time Calc", "_Measures"]
    (d / "model.tmdl").write_text("\n".join([
        "model Model", f"{T}culture: en-US", f"{T}defaultPowerBIDataSourceVersion: powerBI_V3",
        f"{T}discourageImplicitMeasures", f"{T}sourceQueryCulture: en-US", f"{T}dataAccessOptions",
        f"{T*2}legacyRedirects", f"{T*2}returnErrorValuesAsNull", "",
        "annotation __PBI_TimeIntelligenceEnabled = 0", "",
        *[f"ref table {q(t)}" for t in all_tables], "",
        *[f"ref role {q(r)}" for r in ROLES], "",
        *[f"ref perspective {q(p)}" for p in PERSPECTIVES], "",
        "ref cultureInfo en-US", ""]))
    param = 'meta [IsParameterQuery=true, Type="{t}", IsParameterQueryRequired=true]'

    def expr(name, value, rtype, kind):
        return [f"expression {name} = {value} {param.format(t=kind)}", f"{T}lineageTag: {lineage("expression", name)}", "",
                f"{T}annotation PBI_ResultType = {rtype}", ""]
    params = expr("SqlEndpoint", '"<workspace>.datawarehouse.fabric.microsoft.com"', "Text", "Text") \
        + expr("LakehouseDb", '"lendingclub_lakehouse"', "Text", "Text") if fabric \
        else expr("DataFolder", f'"{data_dir}"', "Text", "Text")
    # RangeStart/RangeEnd are the reserved names incremental refresh substitutes per partition. In Desktop they
    # bound the import to the whole history; the service rewrites them per partition.
    params += expr("RangeStart", "#datetime(2007, 1, 1, 0, 0, 0)", "DateTime", "DateTime")
    params += expr("RangeEnd", "#datetime(2019, 3, 1, 0, 0, 0)", "DateTime", "DateTime")
    if not fabric:  # staging queries: Enable load off, referenced by the loaded M tables
        for name, steps in STAGING.items():
            params += [f"expression {q(name)} =", *[f"{T*2}{x}" for x in ["let", *steps]], f"{T}lineageTag: {lineage("expression", name)}", "",
                       f"{T}annotation PBI_ResultType = Table", ""]
    (d / "expressions.tmdl").write_text("\n".join(params))

    for name, (mart, cfg) in TABLES.items():
        (d / "tables" / f"{name}.tmdl").write_text(table_tmdl(con, name, mart, cfg, fabric))
    if fabric:
        (d / "tables" / "Loans Agg.tmdl").write_text(loans_agg_tmdl())
    for name, (cols, steps) in m_tables.items():
        (d / "tables" / f"{name}.tmdl").write_text(m_table_tmdl(name, cols, steps))
    for name, (dax, cols) in CALCULATED.items():
        (d / "tables" / f"{name}.tmdl").write_text(calculated_tmdl(name, dax, cols))
    if not fabric:
        for name, items in FIELD_PARAMS.items():
            (d / "tables" / f"{name}.tmdl").write_text(field_parameter_tmdl(name, items))
    (d / "tables" / "Time Calc.tmdl").write_text(calc_group_tmdl("Time Calc", "Time Calc", 10, TIME_ITEMS))
    (d / "functions.tmdl").write_text(functions_tmdl())
    (d / "tables" / "_Measures.tmdl").write_text(measures_tmdl(measures))
    for name, r in ROLES.items():
        (d / "roles" / f"{name}.tmdl").write_text(role_tmdl(name, r))
    for name, tables in PERSPECTIVES.items():
        (d / "perspectives" / f"{name}.tmdl").write_text(perspective_tmdl(name, [t for t in tables if t in all_tables]))
    (d / "cultures" / "en-US.tmdl").write_text(culture_tmdl())
    if not fabric:
        shutil.rmtree(out / "DAXQueries", ignore_errors=True)
        (out / "DAXQueries").mkdir()
        for fname, text in dax_queries(con).items():
            (out / "DAXQueries" / fname).write_text(text)

    rel, kinds = [], []
    for ft, fc, tt, tc, active, card in RELATIONSHIPS + (FABRIC_RELATIONSHIPS if fabric else []):
        rel.append(f"relationship {lineage("relationship", ft, fc, tt, tc)}")
        if not active:
            rel.append(f"{T}isActive: false")
        if card == "m2m":
            rel += [f"{T}fromCardinality: many", f"{T}toCardinality: many"]
        rel += [f"{T}fromColumn: {q(ft)}.{q(disp(ft, fc))}", f"{T}toColumn: {q(tt)}.{q(disp(tt, tc))}", ""]
        kinds.append(f"{ft} -> {tt}: {relationship_kind(ft, tt, fabric)}")
    (d / "relationships.tmdl").write_text("\n".join(rel))
    limited = sorted({k for k in kinds if k.endswith("limited")})
    print(f"  ✓ wrote {out.relative_to(ROOT)} ({len(all_tables)} tables, {len(kinds)} relationships "
          f"[{len(kinds) - len(limited)} regular, {len(limited)} limited{': ' + '; '.join(limited) if limited else ''}], "
          f"{len(measures)} measures, {len(FUNCTIONS)} functions, {len(ROLES)} roles, {len(PERSPECTIVES)} perspectives)")
    if fabric and len(measures) < len(MEASURES):
        print(f"    composite variant leaves out {len(MEASURES) - len(measures)} measures that need calculated columns: "
              + ", ".join(sorted({m[1] for m in MEASURES} - {m[1] for m in measures})))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data" / "marts"))
    con = duckdb.connect(str(ROOT / "data" / "warehouse" / "lendingclub.duckdb"), read_only=True)
    check(con)
    write_model(con, OUT, False, folder(ap.parse_args().data_dir))
    write_model(con, FABRIC_OUT, True, None)


if __name__ == "__main__":
    main()
