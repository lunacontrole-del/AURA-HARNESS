"""Model promoter advisory-only; nenhuma promoção automática ou escrita de config."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class PromotionRecommendation:
    candidate: str
    recommended: bool
    reason: str
    paper_only: bool = True
    execution_allowed: bool = False


class ModelPromoter:
    def recommend(self, candidate: str, *, current_score: float,
                  candidate_score: float, min_improvement: float = 0.05) -> PromotionRecommendation:
        improvement = float(candidate_score) - float(current_score)
        if improvement >= float(min_improvement):
            return PromotionRecommendation(str(candidate), True, "candidate_outperforms_baseline")
        return PromotionRecommendation(str(candidate), False, "insufficient_improvement")

    def promote(self, candidate: str) -> PromotionRecommendation:
        return PromotionRecommendation(str(candidate), False, "automatic_promotion_disabled")


MODEL_PROMOTER = ModelPromoter()
__all__ = ["ModelPromoter", "PromotionRecommendation", "MODEL_PROMOTER"]
