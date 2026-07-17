"""AppliedIn apply worker + career-site crawler (ECS Fargate task).

One container image, two entrypoints selected by ``APPLIEDIN_TASK_MODE``:
``apply`` (one job per task, per-task public IP = the IP rotation) and
``crawl`` (careers-page crawler for watchlist companies with no feed).
"""

__version__ = "0.1.0"
