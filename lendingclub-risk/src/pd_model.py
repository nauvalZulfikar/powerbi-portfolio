"""Probability-of-default model.

Gradient-boosted trees on origination-time features only (nothing that is
known after the loan is issued). Only matured loans (full term elapsed before
the tape ends) are used, so the label is final. Out-of-time validation: train
on loans issued 2010–2014, test on 36-month loans issued 2015.
Scores every loan and writes model diagnostics as mart tables.
"""
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import roc_auc_score, roc_curve

NUMERIC = [
    "loan_amount", "term_months", "int_rate", "installment", "emp_years", "annual_income",
    "dti", "delinq_2yrs", "inq_last_6mths", "open_acc", "pub_rec", "revol_bal",
    "revol_util", "total_acc", "mort_acc", "pub_rec_bankruptcies", "credit_history_years",
]
CATEGORICAL = ["sub_grade", "home_ownership", "verification_status", "purpose", "application_type"]
FEATURES = NUMERIC + CATEGORICAL
LABELS = {
    "loan_amount": "Loan amount", "term_months": "Term", "int_rate": "Interest rate",
    "installment": "Monthly instalment", "emp_years": "Employment length",
    "annual_income": "Annual income", "dti": "Debt-to-income", "delinq_2yrs": "Delinquencies (2y)",
    "inq_last_6mths": "Credit inquiries (6m)", "open_acc": "Open accounts", "pub_rec": "Public records",
    "revol_bal": "Revolving balance", "revol_util": "Revolving utilisation",
    "total_acc": "Total accounts", "mort_acc": "Mortgage accounts",
    "pub_rec_bankruptcies": "Bankruptcies", "credit_history_years": "Credit history length",
    "sub_grade": "Sub-grade", "home_ownership": "Home ownership",
    "verification_status": "Income verification", "purpose": "Loan purpose",
    "application_type": "Application type",
}


def ks_stat(y, p):
    fpr, tpr, _ = roc_curve(y, p)
    return float(np.max(tpr - fpr))


def run(con, seed=42):
    cols = ", ".join(FEATURES)
    df = con.execute(f"""
        select loan_id, year(issue_date) as vintage, {cols},
               status in ('Charged Off', 'Default') as defaulted,
               status in ('Charged Off', 'Default', 'Fully Paid')
                 and issue_date + to_months(term_months) <= date '2018-09-01' as resolved
        from stg.loans
    """).df()
    df[NUMERIC] = df[NUMERIC].astype("float64")
    for c in CATEGORICAL:
        df[c] = df[c].astype("category")
    X = df[FEATURES]

    train = df.resolved & df.vintage.between(2010, 2014)
    test = df.resolved & (df.vintage == 2015) & (df.term_months == 36)

    model = HistGradientBoostingClassifier(
        max_iter=400, learning_rate=0.06, max_leaf_nodes=48, min_samples_leaf=200,
        l2_regularization=1.0, categorical_features="from_dtype",
        early_stopping=True, validation_fraction=0.1, random_state=seed,
    )
    model.fit(X[train], df.defaulted[train])

    df["pd"] = model.predict_proba(X)[:, 1]
    y, p = df.defaulted[test].astype(int), df.pd[test]
    auc = roc_auc_score(y, p)
    ks = ks_stat(y, p)
    print(f"  PD model  train {train.sum():,}  test {test.sum():,}  "
          f"AUC {auc:.3f}  Gini {2 * auc - 1:.3f}  KS {ks:.3f}")

    con.execute("create schema if not exists mart")
    scores = df[["loan_id", "pd"]].assign(is_test=test)
    con.execute("create or replace table stg.pd_scores as select * from scores")

    # Diagnostics on the out-of-time test set -------------------------------
    t = pd.DataFrame({"pd": p, "y": y})
    t["decile"] = pd.qcut(t.pd.rank(method="first"), 10, labels=range(1, 11)).astype(int)
    calib = (t.groupby("decile")
              .agg(loans=("y", "size"), defaults=("y", "sum"), avg_pd=("pd", "mean"), default_rate=("y", "mean"))
              .reset_index())
    calib["decile_label"] = "D" + calib.decile.astype(str).str.zfill(2)
    calib = calib.sort_values("decile", ascending=False)
    calib["cum_default_capture"] = calib.defaults.cumsum() / calib.defaults.sum()
    calib["cum_population"] = calib.loans.cumsum() / calib.loans.sum()
    calib["lift"] = calib.default_rate / y.mean()

    fpr, tpr, _ = roc_curve(y, p)
    idx = np.unique(np.linspace(0, len(fpr) - 1, 201).astype(int))
    roc = pd.DataFrame({"fpr": fpr[idx], "tpr": tpr[idx], "random": fpr[idx]})

    sample = t.sample(40_000, random_state=seed).index
    imp = permutation_importance(model, X.loc[sample], y.loc[sample], scoring="roc_auc",
                                 n_repeats=3, random_state=seed, n_jobs=-1)
    importance = (pd.DataFrame({"feature": FEATURES, "auc_drop": imp.importances_mean})
                  .assign(feature_label=lambda d: d.feature.map(LABELS))
                  .sort_values("auc_drop", ascending=False))

    metrics = pd.DataFrame([{
        "model": "HistGradientBoosting", "train_window": "2010–2014 issued, matured",
        "test_window": "2015 issued, 36-month, matured",
        "train_loans": int(train.sum()), "test_loans": int(test.sum()),
        "test_default_rate": float(y.mean()), "auc": auc, "gini": 2 * auc - 1, "ks": ks,
    }])

    for name, frame in [("model_calibration", calib), ("model_roc", roc),
                        ("model_importance", importance), ("model_metrics", metrics)]:
        con.execute(f"create or replace table mart.{name} as select * from frame")
