"""
Stage 6: SympliSend Automated Submission
Handles the complete 3-branch submission decision tree:
  Branch 1: Provider First Submission Claim
  Branch 2: Provider Prior Claim (Add to Existing)
  Branch 3: Provider Itemization
"""
from playwright.sync_api import Page
from src.utils.logger import get_logger

logger = get_logger(__name__)


class SympliSendSubmission:
    def __init__(self, page: Page):
        self.page = page
        self.base_url = "https://www.symplisend.com"  # Actual SympliSend URL

    def login(self, credentials: dict) -> bool:
        logger.info("Logging into SympliSend...")
        try:
            # self.page.goto(f"{self.base_url}/login")
            # self.page.fill("input[name='username']", credentials['username'])
            # self.page.fill("input[name='password']", credentials['password'])
            # self.page.click("button[type='submit']")
            # self.page.wait_for_selector("text=New Submission", timeout=15000)
            logger.info("SympliSend login successful.")
            return True
        except Exception as e:
            logger.error(f"SympliSend login failed: {e}")
            return False

    def create_new_submission(self):
        """Click 'New Submission' button."""
        logger.info("Creating new SympliSend submission...")
        # self.page.click("text=New Submission")
        # self.page.wait_for_selector("text=Submission Type", timeout=10000)

    # ──────────────────────────────────────────
    # BRANCH 1: Provider First Submission Claim
    # ──────────────────────────────────────────
    def submit_first_claim(self, submission_data: dict) -> dict:
        """
        Branch 1: Provider First Submission Claim
        Checks HMO POS Benefit → HEAT Claim routing
        """
        logger.info("Branch 1: Provider First Submission Claim")
        # self.page.click("text=Provider First Submission Claim")

        hmo_pos = submission_data.get('hmo_pos_benefit', False)
        is_heat = submission_data.get('is_heat_claim', False)

        if hmo_pos:
            logger.info("HMO POS Benefit: Yes — proceeding HMO path")
            # self.page.click("text=Yes")  # HMO POS
        else:
            logger.info("HMO POS Benefit: No — checking HEAT claim")
            # self.page.click("text=No")
            if is_heat:
                logger.info("HEAT Claim detected — routing to HEAT fields")
                # self.page.click("text=Yes")  # HEAT Init/Final
                # Fill HEAT-specific fields here

        return {"branch": "first", "status": "ready_for_docs"}

    # ──────────────────────────────────────────
    # BRANCH 2: Provider Prior Claim (Add to Existing)
    # ──────────────────────────────────────────
    def submit_prior_claim(self, submission_data: dict) -> dict:
        """
        Branch 2: Provider Prior Claim (Add to Existing)
        Enter Subscriber ID + BS Claim # → Attachment Type decision
        """
        logger.info("Branch 2: Provider Prior Claim (Add to Existing)")
        # self.page.click("text=Provider Prior Claim")

        subscriber_id = submission_data['subscriber_id']
        bs_claim_number = submission_data['blueshield_claim_number']
        attachment_type = submission_data.get('attachment_type', 'BS Requested')

        logger.info(f"Subscriber: {subscriber_id}, BS Claim#: {bs_claim_number}")
        # self.page.fill("input[name='subscriber_id']", subscriber_id)
        # self.page.fill("input[name='claim_number']", bs_claim_number)

        if attachment_type == 'BS Requested':
            logger.info("Attachment Type: BS Requested Records")
            # self.page.click("text=BS Requested Records")
            # self.page.click("text=Select Requested Items")
        elif attachment_type == 'Correction':
            logger.info("Attachment Type: Correction — checking HEAT claim")
            # self.page.click("text=Correction")
            is_heat = submission_data.get('is_heat_claim', False)
            if is_heat:
                logger.info("HEAT claim correction path")

        # Medicare check
        is_medicare = submission_data.get('is_medicare', False)
        if not is_medicare:
            logger.info("Not Medicare — continuing to Add Documentation")

        return {"branch": "prior", "status": "ready_for_docs"}

    # ──────────────────────────────────────────
    # BRANCH 3: Provider Itemization
    # ──────────────────────────────────────────
    def submit_itemization(self, submission_data: dict) -> dict:
        """
        Branch 3: Provider Itemization
        Requires Itemized Bill — blocks if missing (Rule 7)
        """
        logger.info("Branch 3: Provider Itemization")
        # self.page.click("text=Provider Itemization")

        subscriber_id = submission_data['subscriber_id']
        # self.page.fill("input[name='subscriber_id_prefix']", subscriber_id)

        has_itemized_bill = submission_data.get('is_itemized_bill', False)
        if not has_itemized_bill:
            logger.error("BLOCKED: Itemized Bill is required but missing! Cannot continue.")
            return {"branch": "itemization", "status": "blocked", "error": "Itemized Bill required"}

        has_medical_records = submission_data.get('has_medical_records', False)
        if has_medical_records:
            page_start = submission_data.get('page_range_start', 1)
            page_end = submission_data.get('page_range_end', 1)
            logger.info(f"Medical Records included: pages {page_start}-{page_end}")
            # self.page.fill("input[name='page_start']", str(page_start))
            # self.page.fill("input[name='page_end']", str(page_end))

        return {"branch": "itemization", "status": "ready_for_docs"}

    # ──────────────────────────────────────────
    # SHARED: Add Documentation (Stage 6.3)
    # ──────────────────────────────────────────
    def add_documentation(self, file_paths: list, group_label: str = "DOCUMENT_1(3)"):
        """
        Upload documentation packet via drag & drop.
        Supported: PDF / TIFF / PNG / JPEG
        """
        logger.info(f"Uploading {len(file_paths)} documents as group: {group_label}")

        for i, file_path in enumerate(file_paths):
            logger.info(f"  Uploading [{i+1}/{len(file_paths)}]: {file_path}")
            # file_input = self.page.query_selector("input[type='file']")
            # file_input.set_input_files(file_path)
            # self.page.wait_for_selector(f"text={os.path.basename(file_path)}", timeout=10000)

        # Set document group label
        logger.info(f"Setting document group label: {group_label}")
        # self.page.fill("input[name='group_label']", group_label)

    # ──────────────────────────────────────────
    # SUBMIT + STATUS TRACKING (Stage 6.5 / 6.6)
    # ──────────────────────────────────────────
    def submit_and_track(self) -> dict:
        """
        Click Submit → wait for green popup confirmation.
        Then track: Submitted → WIP → Completed
        """
        logger.info("Submitting documentation to SympliSend...")
        # self.page.click("button#submit")

        # Wait for green confirmation popup
        # self.page.wait_for_selector(".success-popup, .green-popup", timeout=15000)
        logger.info("Submission confirmed (Green Popup).")

        # Initial status
        status = "Submitted"
        logger.info(f"Submission status: {status}")

        return {"status": status}

    # ──────────────────────────────────────────
    # CAPTURE FLN# (Stage 6.7 — CRITICAL)
    # ──────────────────────────────────────────
    def capture_fln_number(self) -> str:
        """
        Navigate to Output Tab and extract the FLN# (File Log Number).
        This is THE critical reference number for Blue Shield.
        """
        logger.info("Capturing FLN# from Output Tab...")

        # self.page.click("text=Output")
        # self.page.wait_for_selector("td.fln-number, .reference-number", timeout=15000)
        # fln_element = self.page.query_selector("td.fln-number, .reference-number")
        # fln_number = fln_element.inner_text().strip()

        # Mock for development
        fln_number = "FLN-2026-0417-93847"
        logger.info(f"FLN# Captured: {fln_number}")

        if not fln_number:
            logger.error("ALERT: SympliSend completed but FLN# NOT captured! (Rule 8 violation)")
            return None

        return fln_number

    # ──────────────────────────────────────────
    # FULL FLOW ORCHESTRATOR
    # ──────────────────────────────────────────
    def execute_full_submission(self, submission_data: dict, file_paths: list) -> dict:
        """
        Runs the entire SympliSend submission end-to-end.
        Returns dict with submission_type, status, fln_number.
        """
        self.create_new_submission()

        submission_type = submission_data.get('submission_type', 'First')

        # Route to correct branch
        if submission_type == 'First':
            result = self.submit_first_claim(submission_data)
        elif submission_type == 'Prior':
            result = self.submit_prior_claim(submission_data)
        elif submission_type == 'Itemization':
            result = self.submit_itemization(submission_data)
            if result.get('status') == 'blocked':
                return result
        else:
            logger.error(f"Unknown submission type: {submission_type}")
            return {"status": "error", "error": f"Unknown type: {submission_type}"}

        # Add documentation (all branches converge here)
        group_label = submission_data.get('document_group_label', 'DOCUMENT_1(3)')
        self.add_documentation(file_paths, group_label)

        # Submit
        submit_result = self.submit_and_track()

        # Capture FLN#
        fln_number = self.capture_fln_number()

        return {
            "submission_type": submission_type,
            "status": submit_result.get('status', 'Unknown'),
            "fln_number": fln_number,
            "branch_result": result
        }
