import os

# main.py reads these via os.environ[...] at import time, and load_dotenv()
# never overrides variables that are already set — so setting them here first
# keeps tests isolated from any real .env file in the working directory.
os.environ.setdefault("SUPABASE_URL", "https://test-project.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-service-role-key")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("EVOLUTION_API_URL", "https://test-evolution.example.com")
os.environ.setdefault("EVOLUTION_API_KEY", "test-evolution-key")
os.environ.setdefault("EVOLUTION_INSTANCE_NAME", "test-instance")
