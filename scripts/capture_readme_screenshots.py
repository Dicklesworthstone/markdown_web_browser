#!/usr/bin/env python3
"""
Capture screenshots for README.md demonstrating the finviz.com example.

This script captures:
1. BEFORE: Original finviz.com website
2. AFTER (Raw): Browser UI showing raw markdown with syntax highlighting
3. AFTER (Rendered): Browser UI showing rendered markdown
"""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright


async def capture_screenshots():
    """Capture all required screenshots for README."""

    output_dir = Path("docs/images")
    output_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # Launch browser with same stealth settings as our system
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--headless=new",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--window-size=1920,1080",
            ]
        )

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()

        # Add stealth masking
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });
            delete navigator.__proto__.webdriver;
            Object.defineProperty(navigator, 'plugins', {
                get: () => [
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: 'Portable Document Format' },
                    { name: 'Native Client', filename: 'internal-nacl-plugin', description: 'Native Client Executable' }
                ]
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en']
            });
            if (!window.chrome) {
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };
            }
        """)

        print("📸 Capturing BEFORE screenshot: finviz.com original...")

        # 1. Capture original finviz.com (BEFORE)
        try:
            await page.goto("https://finviz.com", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)  # Wait for dynamic content
            await page.screenshot(path=str(output_dir / "finviz_before.png"), full_page=False)
            print("✅ Saved: docs/images/finviz_before.png")
        except Exception as e:
            print(f"⚠️  Warning: Could not capture finviz.com directly: {e}")

        print("\n📸 Capturing AFTER screenshots: Browser UI with markdown...")

        # 2. Load the browser UI
        await page.goto("http://localhost:8000/browser", wait_until="load", timeout=30000)
        print("   Browser UI loaded")
        
        # 3. Enter the URL in the input field and press Enter
        print("   Entering finviz.com URL and pressing Enter...")
        await page.fill("#url-input", "https://finviz.com")
        await page.press("#url-input", "Enter")
        
        # 4. Wait for the welcome message to be REPLACED with actual content
        print("   Waiting for finviz content to load (this may take 30-60 seconds)...")
        try:
            await page.wait_for_function(
                """() => {
                    const content = document.querySelector('#rendered-content');
                    if (!content) return false;
                    const text = content.textContent || '';
                    // Wait until the welcome message is gone and replaced with real content
                    const hasWelcome = text.includes('Welcome to Markdown Web Browser');
                    const hasFinviz = text.includes('Finviz') || text.includes('DOW') || text.includes('NASDAQ') || text.includes('S&P 500');
                    return !hasWelcome && hasFinviz;
                }""",
                timeout=180000  # 3 minutes for finviz to be captured and processed
            )
            print("   ✓ Content loaded successfully - finviz data detected!")
        except Exception as e:
            print(f"   ⚠️  Warning: Timeout waiting for content: {e}")
            
            # Debug: Check what's actually in the content
            content_text = await page.evaluate("""() => {
                const content = document.querySelector('#rendered-content');
                return content ? content.textContent.substring(0, 300) : 'NO CONTENT';
            }""")
            print(f"   Content preview: {content_text[:150]}...")
        
        # Extra wait to ensure rendering is complete
        await page.wait_for_timeout(2000)

        # 5. Capture RENDERED view (should be active by default)
        await page.screenshot(path=str(output_dir / "finviz_after_rendered.png"), full_page=True)
        print("✅ Saved: docs/images/finviz_after_rendered.png")

        # 6. Click "Raw" button to show raw markdown
        print("   Switching to raw markdown view...")
        try:
            await page.click("#raw-btn", force=True)
            await page.wait_for_timeout(1500)
            
            # Wait for raw content to be visible and have content
            await page.wait_for_function(
                """() => {
                    const rawView = document.querySelector('#raw-view');
                    const rawContent = document.querySelector('#raw-content');
                    if (!rawView || !rawContent) return false;
                    
                    // Check if raw view is active (has 'active' class)
                    const isActive = rawView.classList.contains('active');
                    
                    // Check if raw content has actual markdown text
                    const hasContent = rawContent.textContent.length > 100;
                    
                    return isActive && hasContent;
                }""",
                timeout=5000
            )
            print("   ✓ Switched to raw view successfully")
        except Exception as e:
            print(f"   ⚠️  Warning: Could not switch to raw view properly: {e}")

        await page.screenshot(path=str(output_dir / "finviz_after_raw.png"), full_page=True)
        print("✅ Saved: docs/images/finviz_after_raw.png")

        await browser.close()

    print("\n✨ All screenshots captured successfully!")
    print(f"   Location: {output_dir.absolute()}")


if __name__ == "__main__":
    asyncio.run(capture_screenshots())
