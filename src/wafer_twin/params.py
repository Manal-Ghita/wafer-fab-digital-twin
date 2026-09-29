from pathlib import Path

import pandas as pd
from scipy import stats

PARAMS_DIR = Path(__file__).resolve().parents[2] / "params"


def fit_gamma_by_recipe(process, workshop):
    # one gamma per recipe, loc fixed at 0, theta in minutes
    ops = process[process["WORKSHOP"] == workshop]
    rows = []
    for recipe, g in ops.groupby("RECIPE"):
        x = g["PROCESS_TIME_MIN"].to_numpy()
        k, _, theta = stats.gamma.fit(x, floc=0)
        rows.append({"RECIPE": recipe, "n": len(x), "k": k, "theta_min": theta})
    return pd.DataFrame(rows).set_index("RECIPE")