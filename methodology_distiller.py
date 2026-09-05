# engine/methodology_distiller.py
# AURA QUANT-X — Methodology Distiller (XGBoost Surrogate + SHAP)
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd
import xgboost as xgb
from sklearn.metrics import accuracy_score, classification_report
from sklearn.model_selection import train_test_split

try:
    import shap
    SHAP_OK = True
except ImportError:
    SHAP_OK = False


class MethodologyDistiller:
    def __init__(self, db_path: str = "aura_quant_local.db", model_path: str = "surrogate_model.json"):
        self.db_path = db_path
        self.model_path = model_path
        self.model: Optional[xgb.XGBClassifier] = None
        self.explainer = None

    def _fetch_and_preprocess(self):
        conn = sqlite3.connect(self.db_path)
        df = pd.read_sql_query(
            "SELECT page_state, signal_detected, corner_happened_next_10m "
            "FROM external_ai_surrogate WHERE corner_happened_next_10m IS NOT NULL",
            conn,
        )
        conn.close()
        if df.empty:
            return pd.DataFrame(), pd.Series(dtype=float)

        df["odds_mentioned"] = (
            df["page_state"].str.extract(r"(\d+\.\d+)", expand=False).astype(float).fillna(0.0)
        )
        df["stats_count"] = df["page_state"].apply(lambda x: len(str(x).split("|")))
        df["signal_detected"] = df["signal_detected"].astype(int)
        features = df[["odds_mentioned", "stats_count", "signal_detected"]]
        return features, df["corner_happened_next_10m"].astype(int)

    def train_surrogate_model(self) -> None:
        print("[Distiller] Preparando dados para destilacao de metodologia...")
        X, y = self._fetch_and_preprocess()
        if len(X) < 50:
            print("[Distiller] Dados insuficientes para treino. Continue capturando.")
            return

        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
        self.model = xgb.XGBClassifier(
            objective="binary:logistic", eval_metric="logloss", max_depth=4
        )
        self.model.fit(X_train, y_train)
        preds = self.model.predict(X_test)
        print(f"[Distiller] Acuracia do Surrogate: {accuracy_score(y_test, preds):.4f}")
        print(classification_report(y_test, preds))
        self.model.save_model(self.model_path)
        self._extract_methodology(X_train)

    def _extract_methodology(self, X_train) -> None:
        if not SHAP_OK or self.model is None:
            print("[Distiller] SHAP indisponivel ou modelo nulo.")
            return
        print("[Distiller] Executando SHAP para extrair logica oculta...")
        self.explainer = shap.TreeExplainer(self.model)
        shap_values = self.explainer.shap_values(X_train)
        methodology_report = []
        for i, col in enumerate(X_train.columns):
            mean_abs_shap = abs(shap_values[:, i]).mean()
            methodology_report.append(
                {"feature": col, "importancia_metodologia": float(mean_abs_shap)}
            )
        methodology_report.sort(key=lambda x: x["importancia_metodologia"], reverse=True)
        print("\n=== METODOLOGIA EXTRAIDA DA IA ALVO ===")
        for item in methodology_report:
            print(f"Feature: {item['feature']} | Impacto: {item['importancia_metodologia']:.4f}")
        Path("methodology_report.json").write_text(json.dumps(methodology_report, indent=2))

    def predict_cross_market(self, current_page_state: str, has_signal: bool) -> float:
        if self.model is None:
            self.model = xgb.XGBClassifier()
            if Path(self.model_path).exists():
                self.model.load_model(self.model_path)
            else:
                return 0.0
        try:
            first = current_page_state.split("|")[0] if current_page_state else ""
            digits = "".join(c for c in first if c.isdigit() or c == ".")
            odds = float(digits) if digits else 0.0
        except Exception:
            odds = 0.0
        stats_count = len(current_page_state.split("|")) if current_page_state else 0
        df_input = pd.DataFrame(
            [[odds, stats_count, int(has_signal)]],
            columns=["odds_mentioned", "stats_count", "signal_detected"],
        )
        return float(self.model.predict_proba(df_input)[0][1])


if __name__ == "__main__":
    distiller = MethodologyDistiller()
    distiller.train_surrogate_model()
