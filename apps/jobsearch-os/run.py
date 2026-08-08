"""
Bootstrap script: checks dependencies, installs Playwright Chromium if needed,
then launches the app. Run with: uv run python run.py
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent


def run(*args, check=True, **kwargs):
    return subprocess.run(args, check=check, **kwargs)


def chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
        return True
    except Exception:
        return False


def main():
    print("==> uv sync")
    run("uv", "sync", cwd=ROOT)

    print("==> checking Playwright Chromium...")
    if not chromium_installed():
        print("==> installing Playwright Chromium")
        run("uv", "run", "playwright", "install", "chromium", cwd=ROOT)
    else:
        print("==> Playwright Chromium already installed")

    print("==> launching app")
    run("uv", "run", "python", str(ROOT / "main.py"), cwd=ROOT)


if __name__ == "__main__":
    main()
