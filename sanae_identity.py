"""One configured Sanae QQ account; old bot messages remain bot evidence."""
import os

BOT_NAME = "八千代"
# Display/interaction aliases only; QQ identity and permissions stay unchanged.
BOT_NAME_PATTERN = r"(?:月[见見](?:八千代|ヤチヨ)|八千代|ヤチヨ|Yachiyo(?:8000)?(?![A-Za-z0-9_])|(?:東風谷|东风谷)\s*早苗|早苗)"


# SANAE_BOT_QQ remains compatible with existing public deployments.
SANAE_QQ = (os.environ.get("SANAE_QQ") or os.environ.get("SANAE_BOT_QQ") or "0").strip()
if not SANAE_QQ.isascii() or not SANAE_QQ.isdigit():
    raise ValueError("SANAE_QQ must be a numeric QQ account")

# Configure retired bot accounts only when their old messages need attribution.
_historical = [value.strip() for value in os.environ.get("SANAE_HISTORICAL_BOT_QQ", "").split(",") if value.strip()]
if any(not value.isascii() or not value.isdigit() or int(value) <= 0 for value in _historical):
    raise ValueError("SANAE_HISTORICAL_BOT_QQ must contain comma-separated numeric QQ accounts")
HISTORICAL_BOT_QQ = frozenset(_historical + ([SANAE_QQ] if int(SANAE_QQ) > 0 else []))
