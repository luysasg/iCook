import os

# Must be set before app.config is imported; real values in .env are not overridden by load_dotenv.
os.environ["IFOOD_CLIENT_ID"] = "test-client"
os.environ["OPENROUTER_API_KEY"] = "test-key"
