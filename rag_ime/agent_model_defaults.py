"""Current model defaults for new PAW work; recorded runs keep their identity."""

DEFAULT_AGENT_MODEL_PROVIDER = "openai-codex"
DEFAULT_AGENT_MODEL_ID = "gpt-6.1-sol"
DEFAULT_AGENT_MODEL_PROFILE = f"{DEFAULT_AGENT_MODEL_PROVIDER}/{DEFAULT_AGENT_MODEL_ID}"

PREVIOUS_PRODUCT_MODEL_PROFILES = frozenset({
    "openai-codex/gpt-5.6-luna",
    "openai-codex/gpt-5.6-terra",
    "openai-codex/gpt-5.6-sol",
})
