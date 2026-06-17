"""
Stage 5: Cover Letter PDF Generation
Generates the Helixona OON cover sheet with all 5 required sections.
"""
import os
import tempfile
from datetime import datetime
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Company constants
COMPANY_NAME = "Helixona, Inc."
COMPANY_ADDRESS = "114 Pacifica, Suite 130, Irvine, CA 92618"
COMPANY_TIN = "33-4243090"
COMPANY_NPI = "1407646755"
COMPANY_CONTACT = "Tom Bakman"
COMPANY_EMAIL = "tom@helixona.com"

NOVOLOGIX_DISCLOSURE = (
    "If Blue Shield applies a reduced allowable using NovoLogix or any other "
    "third-party pricing methodology, please disclose in writing the pricing "
    "source/database used, the geographic mapping applied (zip/region), the "
    "percentile level applied, and any edits, bundling, reductions, or modifiers "
    "used, sufficient to reproduce the final allowed amount per code billed."
)


def generate_cover_letter_html(claim_data: dict) -> str:
    """
    Generates cover letter as HTML string.
    claim_data keys:
        patient_name, dob, member_id, group_number, dos,
        blueshield_claim_number, rendering_provider,
        ppo_classification (Self-Funded/ERISA or Fully Insured/Commercial)
    """
    patient_name = claim_data.get('patient_name', 'N/A')
    dob = claim_data.get('dob', 'N/A')
    member_id = claim_data.get('member_id', 'N/A')
    group_number = claim_data.get('group_number', 'N/A')
    dos = claim_data.get('dos', 'N/A')
    bs_claim_number = claim_data.get('blueshield_claim_number', 'PENDING')
    rendering_provider = claim_data.get('rendering_provider', 'N/A')
    ppo_class = claim_data.get('ppo_classification', 'To Be Determined')
    generated_date = datetime.now().strftime('%B %d, %Y')

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: Arial, sans-serif; font-size: 11pt; margin: 40px; color: #222; }}
            .header {{ text-align: center; border-bottom: 3px solid #003366; padding-bottom: 15px; margin-bottom: 25px; }}
            .header h1 {{ color: #003366; margin: 0; font-size: 18pt; }}
            .header p {{ margin: 2px 0; font-size: 9pt; color: #555; }}
            .section {{ margin-bottom: 20px; }}
            .section-title {{ background-color: #003366; color: white; padding: 6px 12px; font-size: 11pt; font-weight: bold; margin-bottom: 10px; }}
            table {{ width: 100%; border-collapse: collapse; margin-bottom: 10px; }}
            td {{ padding: 4px 8px; border: 1px solid #ccc; font-size: 10pt; }}
            td.label {{ background-color: #f0f0f0; font-weight: bold; width: 35%; }}
            .checklist li {{ margin: 4px 0; font-size: 10pt; }}
            .disclosure {{ background-color: #fff8e1; border: 1px solid #ffc107; padding: 12px; font-size: 9.5pt; font-style: italic; }}
            .footer {{ margin-top: 30px; border-top: 1px solid #ccc; padding-top: 10px; font-size: 9pt; color: #666; }}
            .claim-ref {{ background-color: #e8f5e9; border: 2px solid #4caf50; padding: 10px; text-align: center; font-size: 13pt; font-weight: bold; margin-bottom: 20px; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>{COMPANY_NAME}</h1>
            <p>{COMPANY_ADDRESS}</p>
            <p>TIN: {COMPANY_TIN} | Group NPI: {COMPANY_NPI}</p>
            <p>Out-of-Network Documentation Submission Cover Sheet</p>
        </div>

        <div class="claim-ref">
            Blue Shield Claim #: {bs_claim_number}
        </div>

        <!-- SECTION 1: Claim Identifiers -->
        <div class="section">
            <div class="section-title">Section 1 — Claim Identifiers</div>
            <table>
                <tr><td class="label">Patient Name</td><td>{patient_name}</td></tr>
                <tr><td class="label">Date of Birth</td><td>{dob}</td></tr>
                <tr><td class="label">Member ID</td><td>{member_id}</td></tr>
                <tr><td class="label">Group #</td><td>{group_number}</td></tr>
                <tr><td class="label">Date of Service</td><td>{dos}</td></tr>
                <tr><td class="label">Blue Shield Claim #</td><td><strong>{bs_claim_number}</strong></td></tr>
                <tr><td class="label">Rendering Provider</td><td>{rendering_provider}</td></tr>
            </table>
        </div>

        <!-- SECTION 2: PPO Governance Classification -->
        <div class="section">
            <div class="section-title">Section 2 — PPO Governance Classification</div>
            <table>
                <tr><td class="label">Plan Type</td><td>{ppo_class}</td></tr>
            </table>
            <p style="font-size: 9pt; color: #555;">
                Self-Funded / ERISA-Governed: Plan sponsor governs benefit determination.<br>
                Fully Insured / Commercial: State-regulated benefit determination applies.
            </p>
        </div>

        <!-- SECTION 3: Documentation Checklist -->
        <div class="section">
            <div class="section-title">Section 3 — Enclosed Documentation</div>
            <ul class="checklist">
                <li>☑ 837 Claim Print / CMS-1500 Claim Detail</li>
                <li>☑ Verification of Benefits / Eligibility (coverage active on DOS)</li>
                <li>☑ W-9 ({COMPANY_NAME})</li>
                <li>☑ Clinical Note(s) / Medical Necessity Documentation</li>
                <li>☑ Infusion Record (start/stop times, route, dosage, supplies)</li>
                <li>☑ FAIR Health Benchmark Summary</li>
            </ul>
        </div>

        <!-- SECTION 4: Pricing Methodology Disclosure Request -->
        <div class="section">
            <div class="section-title">Section 4 — Pricing Methodology Disclosure Request</div>
            <div class="disclosure">
                {NOVOLOGIX_DISCLOSURE}
            </div>
        </div>

        <!-- SECTION 5: Contact Information -->
        <div class="section">
            <div class="section-title">Section 5 — Provider Contact Information</div>
            <table>
                <tr><td class="label">Provider</td><td>{COMPANY_NAME}</td></tr>
                <tr><td class="label">Address</td><td>{COMPANY_ADDRESS}</td></tr>
                <tr><td class="label">TIN</td><td>{COMPANY_TIN}</td></tr>
                <tr><td class="label">Group NPI</td><td>{COMPANY_NPI}</td></tr>
                <tr><td class="label">Contact</td><td>{COMPANY_CONTACT}</td></tr>
                <tr><td class="label">Email</td><td>{COMPANY_EMAIL}</td></tr>
            </table>
        </div>

        <div class="footer">
            Generated: {generated_date} | {COMPANY_NAME} — Confidential
        </div>
    </body>
    </html>
    """
    return html


def generate_cover_letter_pdf(claim_data: dict, output_dir: str = None) -> str:
    """
    Generates the cover letter as a PDF file using Playwright's PDF rendering.
    Returns the local file path of the generated PDF.
    """
    from playwright.sync_api import sync_playwright

    if output_dir is None:
        output_dir = tempfile.mkdtemp()

    claim_id = claim_data.get('claim_id', 'unknown')
    bs_claim = claim_data.get('blueshield_claim_number', 'PENDING')
    pdf_filename = f"cover_letter_{claim_id}_{bs_claim}.pdf"
    pdf_path = os.path.join(output_dir, pdf_filename)

    html_content = generate_cover_letter_html(claim_data)

    logger.info(f"Generating cover letter PDF for claim {claim_id}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html_content, wait_until="networkidle")
        page.pdf(
            path=pdf_path,
            format="Letter",
            margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"},
            print_background=True
        )
        browser.close()

    logger.info(f"Cover letter PDF saved: {pdf_path}")
    return pdf_path
