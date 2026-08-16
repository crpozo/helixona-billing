from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    aws_region: str = "us-west-2"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    sqs_queue_url: str = ""
    sqs_queue_url_resub: str = ""
    sqs_queue_url_iv: str = ""
    s3_bucket_name: str = ""
    ecw_login_url: str = "https://eclinicalworks.com/login"
    secrets_manager_prefix: str = "prod/helixona/"

    # Bot role: which task family this process consumes. Each role is a
    # separate systemd unit with its own X display, noVNC port and browser
    # profile, so the three never share a session:
    #
    #   role             unit                   display  noVNC  queue
    #   submissions      helixona-agent         :99      6080   sqs_queue_url
    #   resubmissions    helixona-agent-resub   :100     6081   sqs_queue_url_resub
    #   iv_corrections   helixona-agent-iv      :101     6082   sqs_queue_url_iv
    bot_role: str = "submissions"

    # Browser config
    headless_browser: bool = True
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
