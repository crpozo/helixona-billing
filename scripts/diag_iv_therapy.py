"""Diagnostic: dump buttons near IV Therapy in the open PHM Hub Care Plan."""
import json
from playwright.sync_api import sync_playwright

pw = sync_playwright().start()
browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
context = browser.contexts[0]
page = context.pages[0]

page.screenshot(path='/tmp/diag_state.png')
print("Screenshot saved to /tmp/diag_state.png")

# Scan ALL frames for buttons near IV Therapy text
for idx, frame in enumerate(page.frames):
    try:
        result = frame.evaluate("""() => {
            const results = [];
            const allBtns = document.querySelectorAll('button');
            for (let i = 0; i < allBtns.length; i++) {
                const btn = allBtns[i];
                if (btn.offsetWidth === 0) continue;
                // Walk up 8 levels
                let el = btn;
                let text = '';
                for (let j = 0; j < 8; j++) {
                    el = el.parentElement;
                    if (!el) break;
                    text = (el.textContent || '').substring(0, 300);
                    if (text.includes('IV Therapy')) break;
                }
                if (text.includes('IV Therapy')) {
                    results.push({
                        index: i,
                        btnClass: btn.className || '',
                        btnId: btn.id || '',
                        innerHTML: btn.innerHTML.substring(0, 100),
                        hasMoreIcon: !!btn.querySelector('.icon-more-black, i[class*="more"]'),
                        isDropdown: btn.classList.contains('dropdown-toggle'),
                        text: btn.textContent.trim().substring(0, 30),
                        parentText: text.substring(0, 100)
                    });
                }
            }
            return results;
        }""")
        
        if result and len(result) > 0:
            print(f"\n=== Frame {idx}: Buttons near IV Therapy ({len(result)} found) ===")
            for r in result:
                print(json.dumps(r, indent=2))
    except Exception as e:
        continue

# Also check: is "IV Therapy" text actually present in any frame?
for idx, frame in enumerate(page.frames):
    try:
        found = frame.evaluate("""() => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            const matches = [];
            while (walker.nextNode()) {
                const text = (walker.currentNode.textContent || '').trim();
                if (text.includes('IV Therapy')) {
                    const parent = walker.currentNode.parentElement;
                    matches.push({
                        text: text.substring(0, 60),
                        parentTag: parent ? parent.tagName : '?',
                        parentClass: parent ? (parent.className || '').substring(0, 80) : '',
                        parentId: parent ? (parent.id || '') : ''
                    });
                }
            }
            return matches;
        }""")
        if found and len(found) > 0:
            print(f"\n=== Frame {idx}: IV Therapy text nodes ({len(found)} found) ===")
            for f in found:
                print(json.dumps(f, indent=2))
    except:
        continue

browser.close()
pw.stop()
