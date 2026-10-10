"""Browser smoke test with a mocked media stack; never starts real downloads."""
import os
import json
import tempfile
import threading
import time
from pathlib import Path
import uvicorn
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright
from portal.main import create_app
from portal.settings import Settings
from portal_tests.test_portal import Fake


def run():
    with tempfile.TemporaryDirectory(prefix="media-search-browser-") as temp:
        settings = Settings("http://radarr", "http://sonarr", "http://emby", "fake", "fake",
                            Fernet.generate_key().decode(), Path(temp),
                            origins=("http://127.0.0.1:18081",), secure_cookie=False, poll_seconds=1)
        fake = Fake()
        fake.queue_records = [{"movieId": 9, "status": "downloading", "size": 100, "sizeleft": 40}]
        server = uvicorn.Server(uvicorn.Config(create_app(settings, fake.transport), host="127.0.0.1", port=18081, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(200):
            if server.started:
                break
            time.sleep(.05)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"), headless=True, args=["--no-sandbox"])
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto("http://127.0.0.1:18081")
                page.locator("#login-name").fill("User")
                page.locator("#login-password").fill("correct")
                page.get_by_role("button", name="Sign in", exact=True).click()
                page.locator("#query").fill("A movie")
                page.get_by_role("button", name="Search", exact=True).click()
                page.get_by_role("button", name="Add to Emby").wait_for()
                page.screenshot(path="/tmp/media-search-results.png")
                page.get_by_role("button", name="Add to Emby").click()
                page.locator("#status .state.downloading").wait_for(timeout=40000)
                assert page.locator("#query").input_value() == ""
                assert page.locator("#results").inner_text() == ""
                assert "60%" in page.locator("#status").inner_text()
                page.screenshot(path="/tmp/media-search-status.png")
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path="/tmp/media-search-mobile.png")
                page.get_by_role("button", name="Sign out").click()
                page.locator("#login-section").wait_for(state="visible")
                assert not errors, errors
                print(json.dumps({"browser_check": "passed", "javascript_errors": errors,
                                  "screenshots": ["/tmp/media-search-results.png", "/tmp/media-search-status.png", "/tmp/media-search-mobile.png"]}))
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)


if __name__ == "__main__":
    run()
