from src.utils.logger import get_logger

logger = get_logger(__name__)

class IVVerificationService:
    def __init__(self, aws_client):
        self.aws = aws_client

    def verify_iv_prescription(self, medical_record_text: str, dos: str) -> bool:
        """
        Uses Claude via AWS Bedrock to verify if the IV prescription is documented
        in the retrieved medical record for the given DOS.
        """
        logger.info(f"Verifying IV prescription in medical record for DOS {dos}")
        
        system_prompt = (
            "You are a medical billing auditor. Your job is to read clinical documentation "
            "and verify if an Intravenous (IV) Therapy or Infusion was explicitly prescribed or "
            "administered on the provided Date of Service. Respond with ONLY 'YES' or 'NO'."
        )
        
        prompt = f"Date of Service: {dos}\n\nClinical Note:\n{medical_record_text}"
        
        try:
            response_text = self.aws.invoke_bedrock_model(prompt=prompt, system=system_prompt)
            result = response_text.strip().upper()
            
            if "YES" in result:
                logger.info("AI Verification Passed: IV prescription found in medical record.")
                return True
            else:
                logger.warning("AI Verification Failed: IV prescription NOT found explicitly.")
                return False
                
        except Exception as e:
            logger.error(f"Error during AI verification: {e}")
            return False
