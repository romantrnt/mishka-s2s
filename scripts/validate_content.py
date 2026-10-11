import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.foundation.content import FileScenarioRepository

repo = FileScenarioRepository(Path(__file__).resolve().parents[1] / "content")
print(json.dumps(repo.report(), ensure_ascii=False, indent=2))
raise SystemExit(0 if not repo.issues else 1)
