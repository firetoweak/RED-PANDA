import asyncio, os, time
from pathlib import Path
from konden.cli.models import CliSourceRef
from konden.cli.provision import provision_github_release

async def main():
    t = time.time()
    try:
        out = await provision_github_release(
            "zellij",
            CliSourceRef("github-release", "zellij-org/zellij", "v0.45.1"),
            install_root=Path(os.environ["TEMP"]) / "konden_zellij",
            timeout_seconds=180,
        )
        print("OK", round(time.time()-t,1), "s")
        print("PATH", out.resolved_path)
        print("VER", out.version)
    except Exception as e:
        print("FAIL", type(e).__name__, e)

asyncio.run(main())
