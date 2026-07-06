"""Phase 21 — Interpretable Rule Reconstruction.

Fits candidate rules that explain wallet fills using only information
available *before* each fill. Rules are evaluated with mandatory temporal
validation (train/validation/test split) and registered in
``strategy_candidates``. Future labels (markout, PnL, close path) are used
only for evaluation/reporting, never for rule matching.
"""
