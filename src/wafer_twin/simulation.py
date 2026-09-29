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
    machine_recipes = io.groupby("MACHINE")["RECIPE"].apply(set).to_dict()

    gamma = pd.read_csv(PARAMS_DIR / "litho_processing_time_gamma.csv", index_col="RECIPE")
    scores = pd.read_csv(PARAMS_DIR / "litho_product_priority.csv", index_col="PRODUCT")["score"]

    return arrivals, machine_recipes, gamma, scores.to_dict()


class LithoTwin:
    def __init__(self, env, machine_recipes, gamma, scores, rng):
        self.env = env
        self.gamma = gamma  # recipe -> (k, theta in min)
        self.scores = scores
        self.rng = rng
        self.queue = {}  # recipe -> waiting lots
        self.log = []
        self.new_lot = env.event()  # wakes idle machines

        for machine, recipes in machine_recipes.items():
            env.process(self.machine(machine, recipes))

    def arrive(self, lot):
        lot["QUEUE_ENTRY"] = self.env.now
        self.queue.setdefault(lot["RECIPE"], []).append(lot)
        self.new_lot.succeed()
        self.new_lot = self.env.event()

    def pick(self, recipes):
        # every waiting lot this machine can run
        pool = [
            (lots, i)
            for r in recipes
            if (lots := self.queue.get(r))
            for i in range(len(lots))
        ]
        if not pool:
            return None

        # random pick, weighted by product score
        w = np.cumsum([self.scores[lots[i]["PRODUCT"]] for lots, i in pool])
        k = int(np.searchsorted(w, self.rng.random() * w[-1], side="right"))
        lots, i = pool[k]
        return lots.pop(i)

    def machine(self, name, recipes):
        while True:
            lot = self.pick(recipes)
            if lot is None:
                yield self.new_lot  # nothing it can run, sleep
                continue

            lot["MACHINE"] = name
            lot["PROCESS_BEGIN_DATE"] = self.env.now
            k, theta = self.gamma[lot["RECIPE"]]
            yield self.env.timeout(self.rng.gamma(k, theta))
            lot["PROCESS_END_DATE"] = self.env.now
            self.log.append(lot)


def feed(env, twin, arrivals, t0):
    # lots enter the queue at their real time
    times = ((arrivals["QUEUE_ENTRY"] - t0).dt.total_seconds() / 60).to_numpy()
    lots = arrivals[["LOT", "PRODUCT", "RECIPE"]].to_dict("records")
    for t, lot in zip(times, lots):
        yield env.timeout(t - env.now)
        twin.arrive(lot)


def run_twin(arrivals, machine_recipes, gamma, scores, seed=0):
    rng = np.random.default_rng(seed)
    env = simpy.Environment()

    gamma_params = {r: (row.k, row.theta_min) for r, row in gamma.iterrows()}
    twin = LithoTwin(env, machine_recipes, gamma_params, scores, rng)

    # sim clock in minutes from the first arrival
    t0 = arrivals["QUEUE_ENTRY"].min()
    env.process(feed(env, twin, arrivals, t0))
    env.run()

    # back to real dates, same columns as the real data
    sim = pd.DataFrame(twin.log)
    for c in ["QUEUE_ENTRY", "PROCESS_BEGIN_DATE", "PROCESS_END_DATE"]:
        sim[c] = t0 + pd.to_timedelta(sim[c], unit="min")
    sim["WORKSHOP"] = "LITHOGRAPHY"
    sim["WAIT_MIN"] = (sim["PROCESS_BEGIN_DATE"] - sim["QUEUE_ENTRY"]).dt.total_seconds() / 60
    sim["PROCESS_TIME_MIN"] = (sim["PROCESS_END_DATE"] - sim["PROCESS_BEGIN_DATE"]).dt.total_seconds() / 60
    return sim