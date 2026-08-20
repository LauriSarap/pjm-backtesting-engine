"""pjm_eval: read engine parquet output, score and compare strategies.

Eval reads parquet only — no engine imports. Product strings and
formula_version are opaque; the parquet schema is the contract.
"""

import matplotlib

matplotlib.use("Agg")  # headless HTML report generation
