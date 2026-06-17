"""
Blue Shield of California — Provider Portal Automation
Handles login + claim status retrieval from the Provider Connection webapp.

Flow:
  1. Navigate to provider login page
  2. Authenticate via PingFederate SSO
  3. Navigate to Claim Status page
  4. Filter by "In Process" + 48h date range
  5. Scrape all claims (with "Show more claims" pagination)
  6. Return structured claim data
"""
import time
import json
from datetime import datetime, timedelta
from playwright.sync_api import Page
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ─── URLs ───
PROVIDER_LOGIN_URL = "https://www.blueshieldca.com/en/provider_authenticate/login"
CLAIM_STATUS_URL = "https://www.blueshieldca.com/providerwebapp/claims/claimStatus"


class BlueShieldPortal:
    def __init__(self, page: Page):
        self.page = page
        self.logged_in = False

    def login(self, credentials: dict) -> bool:
        """
        Log into the Blue Shield Provider Connection portal.
        BS uses PingFederate SSO — the login form appears after redirects.
        """
        logger.info("Navigating to Blue Shield Provider Portal...")
        try:
            self.page.goto(PROVIDER_LOGIN_URL, wait_until="networkidle", timeout=30000)
            time.sleep(2)

            # Click the "Log in" / "Sign in" link to trigger SSO redirect
            login_link = self.page.query_selector(
                'a:has-text("Log in"), a:has-text("Sign in"), a:has-text("Log In"), '
                'a[href*="login"], a[href*="Login"]'
            )
            if login_link:
                logger.info("Found login link, clicking...")
                login_link.click()
                self.page.wait_for_load_state("networkidle", timeout=15000)
                time.sleep(3)
            else:
                # Might already be on the login form
                logger.info("No login link found, checking if already on login form...")

            # Now we should be on the PingFederate login form
            # Try to find and fill the username/email field
            username = credentials.get('username', '')
            password = credentials.get('password', '')

            logger.info(f"Looking for login form fields (user: {username})...")

            # PingFederate typically uses these selectors
            email_selectors = [
                'input[name="pf.username"]',
                'input[name="username"]',
                'input[name="email"]',
                'input[type="email"]',
                'input[id="username"]',
                'input[id="email"]',
                'input[id="pf.username"]',
                '#signInFormUsername',
                'input[placeholder*="email"]',
                'input[placeholder*="user"]',
            ]

            email_field = None
            for selector in email_selectors:
                email_field = self.page.query_selector(selector)
                if email_field:
                    logger.info(f"Found email field: {selector}")
                    break

            if email_field:
                email_field.fill(username)
                logger.info("Username entered.")
                time.sleep(1)

                # Some SSO flows have a "Next" button before password
                next_btn = self.page.query_selector(
                    'button:has-text("Next"), button:has-text("Continue"), '
                    'input[type="submit"][value="Next"]'
                )
                if next_btn:
                    next_btn.click()
                    time.sleep(3)

            # Find and fill password
            pass_selectors = [
                'input[name="pf.pass"]',
                'input[name="password"]',
                'input[type="password"]',
                'input[id="password"]',
                'input[id="pf.pass"]',
                '#signInFormPassword',
            ]

            pass_field = None
            for selector in pass_selectors:
                pass_field = self.page.query_selector(selector)
                if pass_field:
                    logger.info(f"Found password field: {selector}")
                    break

            if pass_field:
                pass_field.fill(password)
                logger.info("Password entered.")
                time.sleep(1)

            # Click Sign In / Submit
            submit_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Sign In")',
                'button:has-text("Log In")',
                'button:has-text("Login")',
                'button:has-text("Submit")',
                '#signInButton',
                '.ping-button',
            ]

            submit_btn = None
            for selector in submit_selectors:
                submit_btn = self.page.query_selector(selector)
                if submit_btn:
                    logger.info(f"Found submit button: {selector}")
                    break

            if submit_btn:
                submit_btn.click()
                logger.info("Login submitted, waiting for redirect...")
                self.page.wait_for_load_state("networkidle", timeout=30000)
                time.sleep(0.5)
            else:
                logger.warning("Could not find submit button.")

            # Verify login success
            current_url = self.page.url
            current_title = self.page.title()
            logger.info(f"Post-login URL: {current_url}")
            logger.info(f"Post-login title: {current_title}")

            # Check for common success indicators
            if 'providerwebapp' in current_url or 'dashboard' in current_url.lower():
                self.logged_in = True
                logger.info("✅ Successfully logged into Blue Shield Provider Portal!")
                return True
            elif 'error' in current_url.lower() or 'invalid' in current_title.lower():
                logger.error("❌ Login failed — invalid credentials or error page.")
                return False
            else:
                # Might have logged in but landed on a different page
                self.logged_in = True
                logger.info(f"Login appears successful (URL: {current_url})")
                return True

        except Exception as e:
            logger.error(f"Blue Shield login failed: {e}")
            return False

    def navigate_to_claim_status(self) -> bool:
        """Navigate to the Claim Status page."""
        logger.info("Navigating to Claim Status page...")
        try:
            self.page.goto(CLAIM_STATUS_URL, wait_until="networkidle", timeout=30000)
            time.sleep(0.5)
            logger.info(f"Claim Status page loaded: {self.page.title()}")
            return True
        except Exception as e:
            logger.error(f"Failed to navigate to Claim Status: {e}")
            return False

    def set_claim_filters(self, status: str = "In Process", hours_back: int = 48) -> bool:
        """
        Apply filters on the Claim Status page:
        - Claim Status: "In Process"
        - Date Range: [now - hours_back] to [now]
        """
        logger.info(f"Setting filters: status='{status}', date range={hours_back}h...")
        try:
            now = datetime.now()
            start_date = (now - timedelta(hours=hours_back)).strftime("%m/%d/%Y")
            end_date = now.strftime("%m/%d/%Y")

            logger.info(f"Date range: {start_date} → {end_date}")

            # Find and set Claim Status dropdown
            status_selector = self.page.query_selector(
                'select[formcontrolname*="status"], select[name*="status"], '
                'select[id*="status"], select[aria-label*="status"]'
            )
            if status_selector:
                status_selector.select_option(label=status)
                logger.info(f"Status filter set to: {status}")
            else:
                # Try clicking a dropdown that might be an Angular/custom component
                status_dropdown = self.page.query_selector(
                    '[class*="status"] .dropdown, [formcontrolname*="status"], '
                    'mat-select[formcontrolname*="status"]'
                )
                if status_dropdown:
                    status_dropdown.click()
                    time.sleep(1)
                    option = self.page.query_selector(f'mat-option:has-text("{status}"), option:has-text("{status}")')
                    if option:
                        option.click()
                        logger.info(f"Status filter set to: {status}")
                else:
                    logger.warning("Could not find status filter dropdown.")

            time.sleep(1)

            # Find and set Start Date
            start_date_input = self.page.query_selector(
                'input[formcontrolname*="start"], input[name*="startDate"], '
                'input[placeholder*="Start"], input[aria-label*="Start"]'
            )
            if start_date_input:
                start_date_input.fill('')
                start_date_input.fill(start_date)
                logger.info(f"Start date set: {start_date}")

            # Find and set End Date
            end_date_input = self.page.query_selector(
                'input[formcontrolname*="end"], input[name*="endDate"], '
                'input[placeholder*="End"], input[aria-label*="End"]'
            )
            if end_date_input:
                end_date_input.fill('')
                end_date_input.fill(end_date)
                logger.info(f"End date set: {end_date}")

            time.sleep(1)

            # Click Search / Apply / Submit button
            search_btn = self.page.query_selector(
                'button:has-text("Search"), button:has-text("Apply"), '
                'button:has-text("Submit"), button[type="submit"]'
            )
            if search_btn:
                search_btn.click()
                logger.info("Search submitted. Waiting for results...")
                self.page.wait_for_load_state("networkidle", timeout=30000)
                time.sleep(0.5)
            else:
                logger.warning("Could not find search/apply button.")

            return True

        except Exception as e:
            logger.error(f"Failed to set claim filters: {e}")
            return False

    def scrape_all_claims(self) -> list:
        """
        Scrape all claims from the Claim Status results table.
        Clicks "Show more claims" button until all are loaded.
        Returns list of claim dicts.
        """
        logger.info("Scraping claims from results...")
        claims = []

        try:
            # Click "Show more claims" until no more
            max_pages = 50  # Safety limit
            for i in range(max_pages):
                show_more = self.page.query_selector('button.button:has-text("Show more claims")')
                if show_more and show_more.is_visible():
                    logger.info(f"Clicking 'Show more claims' (page {i + 1})...")
                    show_more.click()
                    time.sleep(2)
                    self.page.wait_for_load_state("networkidle", timeout=10000)
                else:
                    logger.info("No more 'Show more claims' button — all loaded.")
                    break

            # Now scrape all visible claim rows
            # BS Provider webapp typically uses Angular components
            claim_rows = self.page.query_selector_all(
                'table tbody tr, .claim-row, [class*="claim-item"], '
                '[class*="claim-card"], .claims-table tr'
            )

            logger.info(f"Found {len(claim_rows)} claim rows.")

            for row in claim_rows:
                try:
                    # Extract cells — adjust based on actual table structure
                    cells = row.query_selector_all('td, .cell, [class*="column"]')
                    if len(cells) >= 3:
                        claim_data = {
                            'raw_text': row.inner_text().strip(),
                            'cells': [cell.inner_text().strip() for cell in cells]
                        }
                        claims.append(claim_data)
                except Exception as row_err:
                    logger.warning(f"Error parsing claim row: {row_err}")
                    continue

            # If no table rows found, try extracting all text from the results area
            if not claims:
                logger.info("No table rows found, trying alternative extraction...")
                results_area = self.page.query_selector(
                    '.claims-results, .claim-list, [class*="claim"], main'
                )
                if results_area:
                    full_text = results_area.inner_text()
                    logger.info(f"Results area text ({len(full_text)} chars): {full_text[:500]}...")
                    claims.append({'raw_text': full_text, 'cells': []})

        except Exception as e:
            logger.error(f"Error scraping claims: {e}")

        logger.info(f"Total claims scraped: {len(claims)}")
        return claims

    def capture_claim_number(self, subscriber_id: str, dos_start: str,
                             dos_end: str, patient_name: str) -> str:
        """
        Full flow: login → claim status → filter → find specific claim → return BS claim #.
        """
        logger.info(f"Capturing claim # for {patient_name} (Sub: {subscriber_id}, DOS: {dos_start})")

        if not self.navigate_to_claim_status():
            return None

        if not self.set_claim_filters(status="In Process", hours_back=48):
            return None

        claims = self.scrape_all_claims()

        # Search for matching claim
        for claim in claims:
            raw = claim.get('raw_text', '').lower()
            if (patient_name.lower() in raw or subscriber_id.lower() in raw):
                # Try to extract the BS claim number from cells
                cells = claim.get('cells', [])
                for cell in cells:
                    # BS claim numbers typically start with specific patterns
                    if cell and len(cell) > 5 and any(c.isdigit() for c in cell):
                        if 'claim' not in cell.lower() and 'status' not in cell.lower():
                            logger.info(f"Potential BS Claim #: {cell}")
                            return cell

                logger.info(f"Found matching row but couldn't extract claim #. Raw: {claim['raw_text'][:200]}")
                return None

        logger.warning(f"No matching claim found for {patient_name}")
        return None

    def get_all_in_process_claims(self) -> list:
        """
        Full flow: navigate to claim status → filter "In Process" last 48h → scrape all.
        Used for bulk monitoring.
        """
        if not self.navigate_to_claim_status():
            return []

        if not self.set_claim_filters(status="In Process", hours_back=48):
            return []

        return self.scrape_all_claims()
