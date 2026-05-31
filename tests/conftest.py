# conftest.py — shared pytest fixtures
import os
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
