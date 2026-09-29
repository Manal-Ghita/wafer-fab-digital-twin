import pandas as pd


def occupancy(start, end, freq="h"):
    # +1 when a lot comes in, -1 when it leaves, running sum = lots inside
    events = pd.concat([
        pd.Series(1, index=start.values),
        pd.Series(-1, index=end.values),
    ])
    level = events.groupby(level=0).sum().sort_index().cumsum()

    # sample on a regular grid
    grid = pd.date_range(level.index.min().floor(freq), level.index.max().ceil(freq), freq=freq)
    return level.reindex(level.index.union(grid)).ffill().reindex(grid).fillna(0)


def workshop_occupancy(process, freq="h"):
    wip = {}
    for ws, g in process.groupby("WORKSHOP"):
        wip[ws] = pd.DataFrame({
            "L_workshop": occupancy(g["QUEUE_ENTRY"], g["PROCESS_END_DATE"], freq),
            "L_machine": occupancy(g["PROCESS_BEGIN_DATE"], g["PROCESS_END_DATE"], freq),
        })
    return wip