import numpy as np
import pandas as pd
import simpy

from wafer_twin.params import PARAMS_DIR


def load_twin_inputs(tables, process, workshop="LITHOGRAPHY"):
    ops = process[process["WORKSHOP"] == workshop]

    # real queue entries in time order
    arrivals = (
        ops[["LOT", "PRODUCT", "RECIPE", "QUEUE_ENTRY"]]
        .sort_values("QUEUE_ENTRY")
        .reset_index(drop=True)
    )

    io = tables["interopt_table"]
    io = io[io["WORKSHOP"] == workshop]
    # sorted list, not a set: set order changes between sessions and breaks reproducibility
    machine_recipes = io.groupby("MACHINE")["RECIPE"].apply(lambda s: sorted(set(s))).to_dict()

    gamma = pd.read_csv(PARAMS_DIR / "litho_processing_time_gamma.csv", index_col="RECIPE")
    scores = pd.read_csv(PARAMS_DIR / "litho_product_priority.csv", index_col="PRODUCT")["score"]

    return arrivals, machine_recipes, gamma, scores.to_dict()


def load_priority(version="v2"):
    # v1: product scores only, v2: product x recipe scores
    if version == "v1":
        product = pd.read_csv(PARAMS_DIR / "litho_product_priority.csv", index_col="PRODUCT")["score"]
        return product.to_dict(), {}

    product = pd.read_csv(PARAMS_DIR / f"litho_priority_{version}_product.csv", index_col="PRODUCT")["score"]
    recipe = pd.read_csv(PARAMS_DIR / f"litho_priority_{version}_recipe.csv", index_col="RECIPE")["score"]
    return product.to_dict(), recipe.to_dict()


def load_same_recipe_factor(version="v3"):
    path = PARAMS_DIR / f"litho_priority_{version}_extra.csv"
    if not path.exists():
        return 1.0  # v1, v2: no same-recipe effect
    extra = pd.read_csv(path, index_col="parameter")["value"]
    return float(extra["same_recipe_factor"])


def load_load_effect():
    p = pd.read_csv(PARAMS_DIR / "litho_load_effect.csv", index_col="parameter")["value"]
    return p["intercept"], p["slope"], p["load_min"], p["load_max"]


class LithoTwin:
    def __init__(self, env, machine_recipes, gamma, scores, rng,
                 recipe_scores=None, same_recipe_factor=1.0, load_effect=None):
        self.env = env
        self.gamma = gamma  # recipe -> (k, theta in min)
        self.scores = scores
        self.recipe_scores = recipe_scores or {}  # missing recipe = weight 1
        self.same_recipe_factor = same_recipe_factor  # 1 = no effect
        self.load_effect = load_effect  # (a, b, load_min, load_max) or None
        self.rng = rng
        self.queue = {}  # recipe -> waiting lots
        self.n_inside = 0  # lots waiting or being processed
        self.log = []
        self.new_lot = env.event()  # wakes idle machines

        for machine, recipes in machine_recipes.items():
            env.process(self.machine(machine, recipes))

    def arrive(self, lot):
        lot["QUEUE_ENTRY"] = self.env.now
        # weight never changes, compute it once
        lot["WEIGHT"] = self.scores[lot["PRODUCT"]] * self.recipe_scores.get(lot["RECIPE"], 1.0)
        self.n_inside += 1
        self.queue.setdefault(lot["RECIPE"], []).append(lot)
        self.new_lot.succeed()
        self.new_lot = self.env.event()

    def pick(self, recipes, last_recipe):
        # every waiting lot this machine can run
        pool = [
            (r, lots, i)
            for r in recipes
            if (lots := self.queue.get(r))
            for i in range(len(lots))
        ]
        if not pool:
            return None

        # random pick, weighted, boost for the recipe the machine just ran
        w = np.cumsum([
            lots[i]["WEIGHT"] * (self.same_recipe_factor if r == last_recipe else 1.0)
            for r, lots, i in pool
        ])
        k = int(np.searchsorted(w, self.rng.random() * w[-1], side="right"))
        _, lots, i = pool[k]
        return lots.pop(i)

    def speed_factor(self):
        # processing gets faster when the workshop is crowded
        if self.load_effect is None:
            return 1.0
        a, b, lo, hi = self.load_effect
        load = min(max(self.n_inside, lo), hi)  # don't extrapolate past what we saw
        return a + b * load

    def machine(self, name, recipes):
        last_recipe = None  # nothing run yet
        while True:
            lot = self.pick(recipes, last_recipe)
            if lot is None:
                yield self.new_lot  # nothing it can run, sleep
                continue

            lot["MACHINE"] = name
            lot["PROCESS_BEGIN_DATE"] = self.env.now
            k, theta = self.gamma[lot["RECIPE"]]
            duration = self.rng.gamma(k, theta) * self.speed_factor()
            yield self.env.timeout(duration)

            lot["PROCESS_END_DATE"] = self.env.now
            self.n_inside -= 1
            self.log.append(lot)
            last_recipe = lot["RECIPE"]


def feed(env, twin, arrivals, t0):
    # lots enter the queue at their real time
    times = ((arrivals["QUEUE_ENTRY"] - t0).dt.total_seconds() / 60).to_numpy()
    lots = arrivals[["LOT", "PRODUCT", "RECIPE"]].to_dict("records")
    for t, lot in zip(times, lots):
        yield env.timeout(t - env.now)
        twin.arrive(lot)


def run_twin(arrivals, machine_recipes, gamma, scores, seed=0,
             recipe_scores=None, same_recipe_factor=1.0, load_effect=None):
    rng = np.random.default_rng(seed)
    env = simpy.Environment()

    gamma_params = {r: (row.k, row.theta_min) for r, row in gamma.iterrows()}
    twin = LithoTwin(env, machine_recipes, gamma_params, scores, rng,
                     recipe_scores, same_recipe_factor, load_effect)

    # sim clock in minutes from the first arrival
    t0 = arrivals["QUEUE_ENTRY"].min()
    env.process(feed(env, twin, arrivals, t0))
    env.run()

    # back to real dates, same columns as the real data
    sim = pd.DataFrame(twin.log).drop(columns="WEIGHT")
    for c in ["QUEUE_ENTRY", "PROCESS_BEGIN_DATE", "PROCESS_END_DATE"]:
        sim[c] = t0 + pd.to_timedelta(sim[c], unit="min")
    sim["WORKSHOP"] = "LITHOGRAPHY"
    sim["WAIT_MIN"] = (sim["PROCESS_BEGIN_DATE"] - sim["QUEUE_ENTRY"]).dt.total_seconds() / 60
    sim["PROCESS_TIME_MIN"] = (sim["PROCESS_END_DATE"] - sim["PROCESS_BEGIN_DATE"]).dt.total_seconds() / 60
    return sim