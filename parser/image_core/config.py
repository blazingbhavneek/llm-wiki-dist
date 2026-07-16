from dataclasses import dataclass


@dataclass
class ImageConfig:
    """All knobs for one pipeline run. Built by image.py from its module globals
    (which parser/server.py mutates directly), then passed explicitly everywhere."""

    base_url: str
    api_key: str
    model: str
    temperature: float = 0.7
    top_p: float = 0.95
    thinking_timeout: int = 600
    fallback_timeout: int = 600

    mermaid_enabled: bool = True
    validate_mermaid: bool = True
    mmdc_bin: str = "mmdc"
    puppeteer_config: str = "./puppeteer-config.json"
    mermaid_timeout: int = 30

    # Preceding-document context sent with each image.
    # context_max_tokens = 0 disables context entirely (image only).
    context_blocks: int = 100
    context_max_tokens: int = 32000
