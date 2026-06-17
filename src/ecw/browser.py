try:
    from patchright.sync_api import sync_playwright, Browser, Page, BrowserContext
except ImportError:
    from playwright.sync_api import sync_playwright, Browser, Page, BrowserContext
from src.utils.logger import get_logger
import os

logger = get_logger(__name__)

# Persistent profile directory — cookies & session survive between runs
PROFILE_DIR = "/opt/helixona-agent/browser-profile"

# Stealth JS scripts to inject
STEALTH_SCRIPTS = """
    // Override webdriver
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    
    // Override plugins
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const plugins = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
            ];
            plugins.length = 3;
            return plugins;
        }
    });

    // Override languages
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

    // Chrome runtime
    window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {} };

    // Permissions
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);

    // WebGL vendor/renderer
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Intel Inc.';
        if (parameter === 37446) return 'Intel Iris OpenGL Engine';
        return getParameter.call(this, parameter);
    };

    // Fix for iframe contentWindow
    const originalCreateElement = document.createElement;
    document.createElement = function(...args) {
        const element = originalCreateElement.apply(this, args);
        if (element.tagName === 'IFRAME') {
            const origContentWindow = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
            if (origContentWindow) {
                Object.defineProperty(element, 'contentWindow', {
                    get: function() {
                        const win = origContentWindow.get.call(this);
                        if (win) {
                            Object.defineProperty(win.navigator, 'webdriver', { get: () => undefined });
                        }
                        return win;
                    }
                });
            }
        }
        return element;
    };
"""


class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None

    def start(self, proxy_config=None):
        """Start browser with optional residential proxy.
        
        proxy_config: dict with keys 'server', 'username', 'password'
            Example: {'server': 'http://geo.iproyal.com:12321',
                      'username': 'user', 'password': 'pass_country-us'}
        """
        logger.info("Starting Playwright browser (persistent profile + stealth)...")
        self.playwright = sync_playwright().start()

        # Ensure profile directory exists
        os.makedirs(PROFILE_DIR, exist_ok=True)

        # Build launch options
        launch_opts = dict(
            user_data_dir=PROFILE_DIR,
            headless=False,  # HEADED: visible on noVNC display :99
            channel="chrome",  # Use real Google Chrome
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--disable-session-crashed-bubble",
                "--disable-features=TranslateUI",
                "--hide-crash-restore-bubble",
                # ─── PERFORMANCE FLAGS ───
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-sync",
                "--disable-translate",
                "--disable-default-apps",
                "--disable-hang-monitor",
                "--disable-popup-blocking",
                "--disable-prompt-on-repost",
                "--disable-client-side-phishing-detection",
                "--disable-component-update",
                "--no-first-run",
                "--no-default-browser-check",
                "--metrics-recording-only",
                "--disable-breakpad",
                "--disable-ipc-flooding-protection",
                "--enable-features=NetworkService,NetworkServiceInProcess",
            ],
            ignore_default_args=["--enable-automation"],
        )

        # Add residential proxy if configured
        if proxy_config:
            launch_opts["proxy"] = {
                "server": proxy_config["server"],
                "username": proxy_config.get("username", ""),
                "password": proxy_config.get("password", ""),
            }
            logger.info(f"🌐 Using residential proxy: {proxy_config['server']}")
        else:
            logger.info("⚠️ No proxy configured — using direct connection (datacenter IP)")

        self.context = self.playwright.chromium.launch_persistent_context(**launch_opts)

        # Inject stealth scripts into all pages
        self.context.add_init_script(STEALTH_SCRIPTS)
        for p in self.context.pages:
            p.add_init_script(STEALTH_SCRIPTS)

        return self

    def new_page(self) -> Page:
        if not self.context:
            raise Exception("Browser not started.")
        # Use existing page if available, otherwise open new one
        if self.context.pages:
            page = self.context.pages[0]
        else:
            page = self.context.new_page()
        return page

    def stop(self):
        logger.info("Stopping Playwright browser instance...")
        if self.context:
            self.context.close()
        if self.playwright:
            self.playwright.stop()
