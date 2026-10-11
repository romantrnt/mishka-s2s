# DEPRECATED: local mock-only launcher; never use for production or RunPod.
"""Deprecated local mock launcher; never use this as the RunPod runtime."""

import os

from app.foundation.api import create_app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(include_test_routes=True), host=os.getenv("MISHKA_HOST", "127.0.0.1"), port=int(os.getenv("MISHKA_PORT", "8000")))
