"""Central configuration, loaded from environment / .env."""

from functools import lru_cache

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Provider keys named in config/models.yaml are read from the environment.
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Public base URL of this server as Twilio sees it, e.g. https://abc.ngrok.app
    public_url: str = "http://localhost:8000"
    host: str = "0.0.0.0"
    port: int = 8000

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""  # enables webhook signature validation + call transfer
    transfer_number: str = ""  # E.164 number for human hand-off

    # Asterisk AudioSocket (TCP); 0 disables
    audiosocket_port: int = 9092
    asterisk_hosts: str = ""  # comma-separated IPs allowed to call /asterisk/register

    # LLM pool: providers/models in this file, keys in .env (GROQ_API_KEY, GLM_API_KEY, ...)
    llm_models_file: str = "config/models.yaml"
    llm_first_token_timeout: float = 4.0  # seconds; slower models are skipped for the next one
    llm_max_tokens: int = 1024

    # Deepgram (STT + TTS)
    deepgram_api_key: str = ""
    stt_model: str = "nova-3"
    stt_language: str = "en-US"
    stt_endpointing_ms: int = 300
    tts_voice: str = "aura-2-thalia-en"

    # VAD / barge-in
    vad_aggressiveness: int = 2  # 0-3
    barge_in_ms: int = 240  # continuous speech needed to interrupt the bot

    # Data
    database_url: str = "postgresql://ringmind:ringmind@localhost:5433/ringmind"
    seed_demo_data: bool = True  # load the demo company into an empty database at startup
    kb_path: str = "data/chroma"
    kb_collection: str = "company_kb"
    kb_docs_path: str = "data/kb"  # ingested at startup if the vector store is empty

    # Operations
    max_concurrent_calls: int = 10  # extra callers hear a "lines are busy" message
    max_call_minutes: int = 20
    admin_user: str = "admin"
    admin_password: str = ""  # enables /admin (call history, tickets, callbacks); unset = disabled
    timezone: str = "America/New_York"  # business hours, appointment slots, "today"

    company_name: str = "Nimbus Mobile"
    bot_name: str = "Maya"


@lru_cache
def get_settings() -> Settings:
    return Settings()
