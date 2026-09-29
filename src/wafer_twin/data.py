from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

DATE_COLS = {
    "lot_arrival_table": ["ARRIVAL_DATE", "DUE_DATE"],
    "lot_process_table": ["PROCESS_BEGIN_DATE", "PROCESS_END_DATE"],
    "machine_breakdown_table": ["BREAKDOWN_BEGIN_DATE", "BREAKDOWN_END_DATE"],
}


def read_table(path):
    with open(path) as fh:
        header = fh.readline()
    sep = ";" if header.count(";") > header.count(",") else ","
    df = pd.read_csv(path, sep=sep)
    df.columns = df.columns.str.strip()
    return df


def load_tables(data_dir=DATA_DIR):
    tables = {f.stem: read_table(f) for f in sorted(Path(data_dir).glob("*.csv"))}

    if "lot_process_table" not in tables:
        raise FileNotFoundError(
            "lot_process_table.csv not found, unzip data/lot_process_table.zip first"
        )

    # timestamps are in ms
    for name, cols in DATE_COLS.items():
        for c in cols:
            tables[name][c] = pd.to_datetime(tables[name][c], unit="ms")

    return tables


def build_process(tables):
    # workshop + recipe come from the product routes
    process = tables["lot_process_table"].merge(
        tables["product_route_table2"], on=["PRODUCT", "OPERATION"], how="left"
    )
    process["PROCESS_TIME_MIN"] = (
        process["PROCESS_END_DATE"] - process["PROCESS_BEGIN_DATE"]
    ).dt.total_seconds() / 60

    process = process.sort_values(["LOT", "OPERATION"]).reset_index(drop=True)

    # next queue when the previous op ends, first queue when the lot arrives
    process["QUEUE_ENTRY"] = process.groupby("LOT")["PROCESS_END_DATE"].shift(1)
    arrivals = tables["lot_arrival_table"][["LOT", "ARRIVAL_DATE"]]
    process = process.merge(arrivals, on="LOT", how="left")
    process["QUEUE_ENTRY"] = process["QUEUE_ENTRY"].fillna(process["ARRIVAL_DATE"])

    process["WAIT_MIN"] = (
        process["PROCESS_BEGIN_DATE"] - process["QUEUE_ENTRY"]
    ).dt.total_seconds() / 60

    return process