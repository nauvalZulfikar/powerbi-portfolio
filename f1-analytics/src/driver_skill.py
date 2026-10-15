"""Who is the best driver once you take the car away?

Model (per classified or crash-ended result, 1950+, Indy 500 and mechanical DNFs excluded):

    finish_percentile ~ driver effect + team-season effect

The team-season effect absorbs how good that year's car was, so a driver's effect is
identified by beating teammates who then beat *their* other teammates, and so on.
Ridge shrinkage (lsqr damp) pulls drivers with few races toward average instead of
letting a 4-race cameo top the table. Writes mart.driver_skill.
"""
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix, hstack
from scipy.sparse.linalg import lsqr

MIN_RACES_RANKED = 40
DAMP = 3.0


def fit(con):
    df = con.execute("""
        select fr.driver_id, fr.constructor_id || '-' || fr.year as team_season, fr.finish_percentile
        from mart.fact_results fr join mart.dim_race r using (race_id)
        where not r.is_indy500 and fr.status_group in ('Finished', 'Finished (lapped)', 'Crash / driver')
    """).df()
    d_codes, drivers = pd.factorize(df.driver_id)
    t_codes, _ = pd.factorize(df.team_season)
    n = len(df)
    rows = np.arange(n)
    X = hstack([csr_matrix((np.ones(n), (rows, d_codes))), csr_matrix((np.ones(n), (rows, t_codes)))]).tocsr()
    y = df.finish_percentile.to_numpy() - df.finish_percentile.mean()
    coef = lsqr(X, y, damp=DAMP, atol=1e-10, btol=1e-10, iter_lim=20000)[0]

    resid = y - X @ coef
    skill = pd.DataFrame({"driver_id": drivers, "skill": coef[: len(drivers)],
                          "races_modelled": np.bincount(d_codes)})
    # standard error from residual variance, only meaningful as a relative band
    skill["skill_se"] = resid.std() / np.sqrt(skill.races_modelled)
    ranked = skill.races_modelled >= MIN_RACES_RANKED
    skill["skill"] -= skill.loc[ranked, "skill"].mean()          # 0 = average ranked driver
    skill["skill_rank"] = skill.skill.where(ranked).rank(ascending=False, method="min").astype("Int64")
    skill["is_ranked"] = ranked
    skill["skill_band"] = pd.cut(skill.skill, [-1, -0.08, -0.03, 0.03, 0.08, 0.15, 1],
                                 labels=["Well below avg", "Below avg", "Average", "Above avg", "Elite", "All-time great"]).astype(str)

    r2 = 1 - resid.var() / y.var()
    con.register("skill_df", skill)
    con.execute("create or replace table mart.driver_skill as select * from skill_df")
    con.unregister("skill_df")
    print(f"  ✓ driver_skill          {len(df):,} results · {len(drivers)} drivers · R² {r2:.2f}")
