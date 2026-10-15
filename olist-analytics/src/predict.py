"""Predictive marts, written into the DuckDB warehouse by src/pipeline.py (and charted by src/analysis.py).

- Revenue Forecast (10-s15): ETS (statsmodels ETSModel, additive damped trend) on monthly revenue Jan 2017-Aug 2018,
  forecast Sep-Dec 2018 with 80% and 95% prediction intervals.
- Predicted CLV 12M (10-s9): BG/NBD (purchase count) x Gamma-Gamma (order value), the Fader-Hardie models the
  `lifetimes` package implements. The likelihoods are written out here with scipy instead of adding `lifetimes`
  (unmaintained, no pandas 3 support); same models, same parameters (r, alpha, a, b / p, q, v), fitted by MLE.
"""
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import gammaln, hyp2f1
from statsmodels.tsa.exponential_smoothing.ets import ETSModel

HORIZON_DAYS = 365
GG_PENALTY = 1e-3


def revenue_forecast(con):
    s = con.execute("""
        select date_trunc('month', order_date)::date m, sum(price) rev from mart.fact_order_items
        where is_valid and order_date between date '2017-01-01' and date '2018-08-31' group by 1 order by 1""").df()
    y = pd.Series(s.rev.values, index=pd.period_range("2017-01", periods=len(s), freq="M"))
    fit = ETSModel(y, error="add", trend="add", damped_trend=True).fit(disp=False)
    pred = fit.get_prediction(start=len(y), end=len(y) + 3)
    f80, f95 = pred.summary_frame(alpha=0.2), pred.summary_frame(alpha=0.05)
    out = pd.DataFrame({"month_start": [p.to_timestamp().date() for p in f80.index], "forecast_revenue": f80["mean"].values,
                        "lower_80": f80["pi_lower"].values, "upper_80": f80["pi_upper"].values,
                        "lower_95": f95["pi_lower"].values, "upper_95": f95["pi_upper"].values})
    con.execute("create or replace table mart.revenue_forecast as select * from out")
    return out, fit.aic


def _bgnbd_ll(params, x, tx, T):
    r, alpha, a, b = np.exp(params)
    a1 = gammaln(r + x) - gammaln(r) + r * np.log(alpha)
    a2 = gammaln(a + b) + gammaln(b + x) - gammaln(b) - gammaln(a + b + x)
    a3 = -(r + x) * np.log(alpha + T)
    a4 = np.where(x > 0, np.log(a) - np.log(np.maximum(b + x - 1, 1e-9)) - (r + x) * np.log(alpha + tx), -np.inf)
    return -(a1 + a2 + np.logaddexp(a3, a4)).sum()


def _gg_ll(params, x, m):
    p, q, v = np.exp(params)
    return -(gammaln(p * x + q) - gammaln(p * x) - gammaln(q) + q * np.log(v) + (p * x - 1) * np.log(m)
             + p * x * np.log(x) - (p * x + q) * np.log(x * m + v)).sum()


def predicted_clv(con):
    """Adds dim_customer.predicted_clv_12m (expected item revenue in the next 12 months). Returns the fitted params."""
    d = con.execute("""
        with o as (select customer_unique_id c, order_date d, items_value v from mart.fact_orders where is_valid),
        days as (select c, d, sum(v) v, min(d) over (partition by c) first_d from o group by 1, 2),  -- purchase days, not orders
        endd as (select max(d) e from days)
        select c, count(*) - 1 x, date_diff('day', min(d), max(d)) tx, date_diff('day', min(d), any_value(e)) T,
               coalesce(avg(v) filter (where d > first_d), 0) m       -- Gamma-Gamma: value of the repeat purchases
        from days, endd group by c""").df()
    x, tx, T, m = (d[k].to_numpy(float) for k in ("x", "tx", "T", "m"))
    bg = minimize(_bgnbd_ll, np.zeros(4), args=(x, tx, T), method="Nelder-Mead", options={"maxiter": 4000, "xatol": 1e-6, "fatol": 1e-6})
    r, alpha, a, b = np.exp(bg.x)
    t = HORIZON_DAYS
    exp_purch = ((a + b + x - 1) / (a - 1) * (1 - ((alpha + T) / (alpha + T + t)) ** (r + x)
                 * hyp2f1(r + x, b + x, a + b + x - 1, t / (alpha + T + t)))
                 / (1 + (x > 0) * a / np.maximum(b + x - 1, 1e-9) * ((alpha + T) / (alpha + tx)) ** (r + x)))
    rep = (x > 0) & (m > 0)
    # Unpenalised, the Gamma-Gamma fit runs off to p -> inf, v -> 0 (2,065 repeat buyers, almost all with one repeat):
    # a small L2 penalty on the parameters, the same `penalizer_coef` lifetimes offers, keeps it at a sane optimum
    gg = minimize(lambda th: _gg_ll(th, x[rep], m[rep]) + GG_PENALTY * (np.exp(th) ** 2).sum(), np.zeros(3),
                  method="Nelder-Mead", options={"maxiter": 8000})
    p, q, v = np.exp(gg.x)
    exp_value = np.where(rep, p * (v + x * m) / (p * x + q - 1), p * v / (q - 1))
    d["predicted_clv_12m"] = np.round(exp_purch * exp_value, 2)
    clv = d[["c", "predicted_clv_12m"]]
    con.execute("alter table mart.dim_customer drop column if exists predicted_clv_12m")
    con.execute("alter table mart.dim_customer add column predicted_clv_12m double")
    con.execute("update mart.dim_customer set predicted_clv_12m = clv.predicted_clv_12m from clv "
                "where dim_customer.customer_unique_id = clv.c")
    return {"r": r, "alpha": alpha, "a": a, "b": b, "p": p, "q": q, "v": v, "bg_ok": bg.success, "gg_ok": gg.success,
            "repeat_customers": int(rep.sum()), "customers": len(d), "total": float(d.predicted_clv_12m.sum())}
