from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    aws_region: str = "us-west-2"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    sqs_queue_url: str = ""
    sqs_queue_url_iv: str = ""
    s3_bucket_name: str = ""
    ecw_login_url: str = "https://eclinicalworks.com/login"
    secrets_manager_prefix: str = "prod/helixona/"

    # Bot role: which task family this process consumes.
    # "submissions"    → Blue Shield new-submission pipeline (default queue)
    # "iv_corrections" → IV fix-coding pipeline (sqs_queue_url_iv)
    bot_role: str = "submissions"

    # Browser config
    headless_browser: bool = True
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
