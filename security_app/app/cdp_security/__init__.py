"""Security Access Workflow - shared package used by the Databricks App and the jobs.

Nothing in this package imports Streamlit or PySpark at module import time, so the
same code runs inside the App container, serverless jobs and unit tests.
"""
__version__ = "1.0.0"
