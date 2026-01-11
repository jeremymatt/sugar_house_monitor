#!/usr/bin/env python3
"""Train isolation-forest models per tank and save them for plotting."""

import argparse
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import numpy as np
from joblib import dump as joblib_dump
from sklearn.ensemble import IsolationForest

import explore_data as ed


def parse_date(date_str: str) -> date:
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def prepare_features(df, feature_cols):
    features = df[feature_cols].replace([np.inf, -np.inf], np.nan).dropna()
    return features


def train_and_save_models(
    dataset_dir: Path,
    model_dir: Path,
    train_date: date,
    test_date: Optional[date] = None,
) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)

    data = ed.load_all_data(dataset_dir)
    data = ed.compute_depth_volume(data)
    data, _ = ed.compute_flow_and_diffs(data)

    data["date"] = data["timestamp"].dt.date
    feature_cols = ["calc_depth_in", "calc_gallons", "calc_flow_gph"]

    for tank_name, tank_df in data.groupby("tank"):
        train_df = tank_df[tank_df["date"] == train_date]
        if train_df.empty:
            print(f"[{tank_name}] no training rows for {train_date}, skipping.")
            continue
        X_train = prepare_features(train_df, feature_cols)
        if X_train.empty:
            print(f"[{tank_name}] training features empty after dropna, skipping.")
            continue

        model = IsolationForest(
            n_estimators=200,
            contamination=0.05,
            random_state=42,
        )
        model.fit(X_train)

        model_path = model_dir / f"isolation_forest_{tank_name}.joblib"
        joblib_dump(model, model_path)
        print(f"[{tank_name}] saved model -> {model_path}")

        if test_date:
            test_df = tank_df[tank_df["date"] == test_date]
            X_test = prepare_features(test_df, feature_cols)
            if X_test.empty:
                print(f"[{tank_name}] test features empty for {test_date}")
            else:
                preds = model.predict(X_test)
                outliers = int((preds == -1).sum())
                print(f"[{tank_name}] test rows: {len(X_test)} | outliers flagged: {outliers}")


def main():
    parser = argparse.ArgumentParser(description="Train isolation-forest models for tank data.")
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "dataset",
        help="Path to dataset directory containing CSVs.",
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "models",
        help="Directory to save joblib models.",
    )
    parser.add_argument(
        "--train-date",
        type=str,
        default="2025-04-03",
        help="Training date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--test-date",
        type=str,
        default="2025-04-04",
        help="Optional test date (YYYY-MM-DD).",
    )
    args = parser.parse_args()

    train_date = parse_date(args.train_date)
    test_date = parse_date(args.test_date) if args.test_date else None

    train_and_save_models(args.dataset_dir, args.model_dir, train_date, test_date)


if __name__ == "__main__":
    main()
