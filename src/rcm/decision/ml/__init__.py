"""The ML subsystem (M6, SRS section 9, ADR-0026).

Models advise; rules decide. A model never overrides a deterministic rule
(PD-5, BR-HITL-01), never runs on features it cannot have (PD-11), and when it
cannot answer, the work goes on without it (FR-ML-06).

Training-time modules (`pipeline`, `evaluate`, `tracking`) import scikit-learn
and MLflow, which are in the `ml` extra only. Nothing a request reaches may
import them.
"""
