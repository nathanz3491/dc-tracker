"""US data center construction project tracker.

Deliberately free of heavy imports: `import tracker` must stay cheap so the CLI
starts fast and so tests can import submodules without pulling in SQLAlchemy,
httpx or crawl4ai.
"""

__version__ = "1.9.4"

__all__ = ["__version__"]
