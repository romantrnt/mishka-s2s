import json
from pathlib import Path
from app.foundation.content import FileScenarioRepository

root = Path(__file__).resolve().parents[1]
report = FileScenarioRepository(root / "content").report()
target = root / "reports" / "content-report.json"; target.parent.mkdir(exist_ok=True)
target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(target)
