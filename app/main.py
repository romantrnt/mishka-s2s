from app.foundation.config import Settings
from app.foundation.stand import create_stand_app

# Deprecated legacy ASGI target. Canonical RunPod production starts
# scripts/runpod_start.sh -> scripts/run_stand.py on port 8000. This module is
# retained only for the historical 8015 harness and fails fast on errors.
app = create_stand_app(Settings.from_env())
