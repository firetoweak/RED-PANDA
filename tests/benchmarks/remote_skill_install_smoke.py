from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile

from redpanda.paths import RedPandaHome
from redpanda.skills.application import SkillApplicationService
from redpanda.skills.models import SkillSourceRef


SOURCE = SkillSourceRef(
    "github",
    "https://github.com/openai/skills/tree/main/skills/.curated/pdf",
)


async def run() -> dict:
    temporary = Path(tempfile.mkdtemp(prefix="redpanda-remote-skill-"))
    try:
        workspace = RedPandaHome(temporary / ".redpanda")
        workspace.initialize()
        service = SkillApplicationService(workspace)
        record = await service.install_source(SOURCE)
        inspection = await service.inspect(record.name)
        checks = {
            "installed": record.name == "pdf",
            "default_disabled": record.enabled is False,
            "resolved_to_commit": "/tree/main/" not in record.resolved_ref,
            "hash_matches_registry": (
                inspection.record.content_hash == record.content_hash
            ),
            "supporting_files_present": len(inspection.files) > 1,
        }
        return {
            "passed": all(checks.values()),
            "checks": checks,
            "record": record.to_dict(),
            "files": [path for path, _ in inspection.files],
        }
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    result = asyncio.run(run())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
