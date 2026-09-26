import importlib

MODULES = [
    "sqlalchemy",
    "pydantic",
    "pydantic_settings",
    "fastapi",
    "redis",
    "httpx",
    "tenacity",
    "arq",
    "langgraph",
    "alembic",
    "asyncmy",
]

for name in MODULES:
    try:
        mod = importlib.import_module(name)
        ver = getattr(mod, "__version__", "-")
        print("OK   %-18s %s" % (name, ver))
    except Exception as exc:
        print("MISS %-18s %s" % (name, type(exc).__name__))
