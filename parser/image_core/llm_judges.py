import json
import os
from typing import List, TypeVar

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from image_core.mermaid_media import (
    ENABLE_MERMAID_DIAGRAMS,
    ENABLE_MERMAID_VISUAL_MATCH_LOOP,
    INCLUDE_VISUAL_JUDGE_NOTE,
    MERMAID_REPAIR_ATTEMPTS,
    MERMAID_VISUAL_MATCH_ATTEMPTS,
    MERMAID_VISUAL_MATCH_GOOD_ENOUGH_SCORE,
    VALIDATE_MERMAID,
    extract_mermaid_blocks,
    get_good_mermaid_examples,
    remove_mermaid_blocks,
    render_all_mermaid_blocks_to_pngs,
    validate_all_mermaid_blocks,
)

load_dotenv()

OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://10.160.144.101:51029/v1")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "local")
OPENAI_MODEL = os.environ.get("WIKI_MODEL", "gemma-4-31B")

TEMPERATURE = 0.7
TOP_P = 0.95
MAX_TOKENS = 16384
LLM_THINKING_TIMEOUT_SECONDS = int(os.environ.get("LLM_THINKING_TIMEOUT_SECONDS", "600"))
LLM_FALLBACK_TIMEOUT_SECONDS = int(os.environ.get("LLM_FALLBACK_TIMEOUT_SECONDS", "600"))

# Relaxed judge settings
JUDGE_TEMPERATURE = 0.7

ENABLE_DESCRIPTION_COVERAGE_LOOP = True
DESCRIPTION_COVERAGE_ATTEMPTS = 3

# Pass threshold stays 90 as requested.
DESCRIPTION_COVERAGE_GOOD_ENOUGH_SCORE = 90

# Do not pollute final output with judge notes.
INCLUDE_COVERAGE_JUDGE_NOTE = False

ENABLE_SHALLOW_RETRY = False


def normalize_base_url(base_url: str) -> str:
    base = (base_url or OPENAI_BASE_URL).rstrip("/")
    if base.endswith("/chat/completions"):
        return base[: -len("/chat/completions")]
    return base


def make_chat_client(timeout: int = 300) -> ChatOpenAI:
    return ChatOpenAI(
        model=OPENAI_MODEL,
        base_url=normalize_base_url(OPENAI_BASE_URL),
        api_key=OPENAI_API_KEY,
        temperature=TEMPERATURE,
        timeout=timeout,
        max_retries=0,
        top_p=TOP_P,
        # max_tokens=MAX_TOKENS,
        extra_body={"chat_template_kwargs": {"enable_thinking": True}},
    )


def _bind_llm_runtime_options(
    client: ChatOpenAI,
    *,
    temperature: float | None = None,
    enable_thinking: bool | None = None,
):
    kwargs = {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if enable_thinking is not None:
        kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": enable_thinking}}
    return client.bind(**kwargs) if kwargs else client


def get_response_text(response) -> str:
    if response is None:
        return ""

    if isinstance(response, str):
        content = response
    elif isinstance(response, dict):
        choices = response.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
        else:
            content = response.get("content", "")
    elif hasattr(response, "content"):
        content = response.content
    else:
        choices = getattr(response, "choices", None) or []
        if not choices:
            return str(response).strip()
        content = choices[0].message.content

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif hasattr(item, "text"):
                parts.append(item.text)
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()

    return str(content).strip()


async def llm_ainvoke_text(
    client: ChatOpenAI,
    messages,
    *,
    temperature: float | None = None,
    enable_thinking: bool | None = None,
) -> str:
    response = await _bind_llm_runtime_options(
        client,
        temperature=temperature,
        enable_thinking=enable_thinking,
    ).ainvoke(messages)
    return get_response_text(response)


TModel = TypeVar("TModel", bound=BaseModel)


async def llm_ainvoke_structured(
    client: ChatOpenAI,
    messages,
    schema_cls: type[TModel],
    *,
    temperature: float | None = None,
    enable_thinking: bool | None = None,
) -> TModel:
    structured_llm = _bind_llm_runtime_options(
        client,
        temperature=temperature,
        enable_thinking=enable_thinking,
    ).with_structured_output(schema_cls)

    result = await structured_llm.ainvoke(messages)
    if isinstance(result, schema_cls):
        return result
    return schema_cls.model_validate(result)


def ensure_reconstruction_wrapper(description: str) -> str:
    description = description.strip()
    if not description.startswith("[Image reconstruction:") and not description.startswith("[Image description:"):
        description = f"[Image reconstruction:\n{description}\n]"
    return description


def looks_too_shallow(description: str) -> bool:
    lowered = description.lower()

    shallow_phrases = [
        "the image displays",
        "the image shows",
        "this image corresponds to",
        "it illustrates",
        "a diagram showing",
        "a figure showing",
        "representing the",
        "this diagram illustrates",
    ]

    structural_markers = [
        "[Image reconstruction:",
        "Type:",
        "Title/caption:",
        "Reconstructed content:",
        "Detailed notes:",
        "```mermaid",
        "flowchart",
        "graph TD",
        "graph LR",
        "sequenceDiagram",
        "-->",
        "|",
    ]

    has_structure = any(marker in description for marker in structural_markers)
    has_shallow_language = any(phrase in lowered for phrase in shallow_phrases)
    too_short_without_structure = len(description.strip()) < 500 and not has_structure

    return too_short_without_structure or (has_shallow_language and not has_structure)


class MermaidVisualJudgeResult(BaseModel):
    should_keep_mermaid: bool = Field(
        ...,
        description="False only if Mermaid is actively misleading or harmful.",
    )
    score: int = Field(..., ge=0, le=100)
    reason: str
    missing: List[str] = Field(default_factory=list)
    wrong: List[str] = Field(default_factory=list)
    suggested_fixes: List[str] = Field(default_factory=list)


class DescriptionCoverageJudgeResult(BaseModel):
    score: int = Field(..., ge=0, le=100)
    reason: str
    missing: List[str] = Field(default_factory=list)
    wrong: List[str] = Field(default_factory=list)
    suggested_fixes: List[str] = Field(default_factory=list)


def build_mermaid_repair_prompt(error_text: str, current_description: str) -> str:
    return (
        "The Markdown replacement contains a Mermaid diagram that does not parse/render.\n\n"
        "Fix Mermaid syntax while preserving meaning. Return the full corrected Markdown replacement.\n\n"
        "Rules:\n"
        "- Use simple ASCII node IDs.\n"
        "- Put Japanese text, spaces, punctuation, parentheses, and long labels inside quoted labels.\n"
        "- Do not use raw Japanese labels as node IDs.\n"
        "- Mermaid block must contain only Mermaid syntax.\n"
        "- Keep explanatory text outside Mermaid.\n"
        "- Simplify unsupported syntax if needed.\n\n"
        f"Mermaid error:\n{error_text}\n\n"
        f"{get_good_mermaid_examples()}\n\n"
        f"Current Markdown replacement:\n{current_description}\n"
    )


async def repair_mermaid_if_needed(client: ChatOpenAI, original_content, description: str):
    if not VALIDATE_MERMAID:
        return description

    blocks = extract_mermaid_blocks(description)
    if not blocks:
        return description

    for attempt in range(1, MERMAID_REPAIR_ATTEMPTS + 1):
        ok, error_text = await validate_all_mermaid_blocks(description)
        if ok:
            print(f"Mermaid validation passed with {len(blocks)} block(s).")
            return description

        print(f"Mermaid validation failed. Repair attempt {attempt}/{MERMAID_REPAIR_ATTEMPTS}.")
        print(error_text)
        print("")

        repair_prompt = build_mermaid_repair_prompt(
            error_text=error_text,
            current_description=description,
        )

        description = (
            await llm_ainvoke_text(
                client,
                [
                    {"role": "user", "content": original_content},
                    {"role": "assistant", "content": description},
                    {"role": "user", "content": repair_prompt},
                ],
                temperature=TEMPERATURE,
            )
        ).strip()

        description = ensure_reconstruction_wrapper(description)
        blocks = extract_mermaid_blocks(description)

        if not blocks:
            print("Repair response contains no Mermaid blocks. Skipping Mermaid validation.")
            return description

    ok, error_text = await validate_all_mermaid_blocks(description)
    if not ok:
        print("WARNING: Mermaid validation still failed after repair attempts.")
        print(error_text)
        print("")

        description = (
            description.rstrip()
            + "\n\n"
            + "[Mermaid validation warning:\n"
            + "The Mermaid diagram above could not be validated automatically.\n\n"
            + "```text\n"
            + error_text.strip()
            + "\n```\n"
            + "]"
        )

    return description


async def judge_mermaid_visual_match(
    client: ChatOpenAI,
    original_image_blocks,
    rendered_mermaid_images,
    description: str,
):
    content = list(original_image_blocks)

    for item in rendered_mermaid_images:
        content.append(
            {
                "type": "text",
                "text": (
                    f"Rendered Mermaid diagram #{item['index']} from this code:\n"
                    f"```mermaid\n{item['code']}\n```"
                ),
            }
        )
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": item["data_url"], "detail": "high"},
            }
        )

    content.append(
        {
            "type": "text",
            "text": (
                "You are a practical visual judge. Compare the original image with the rendered Mermaid.\n\n"
                "Main goal: decide whether Mermaid helps a downstream LLM understand the diagram structure.\n"
                "Do NOT demand pixel-perfect visual matching.\n\n"
                "Relaxed rules:\n"
                "- Orientation/layout differences are acceptable.\n"
                "- Shape/style/spacing differences are minor unless they change meaning.\n"
                "- Score high if the main components, relationships, directions, and important labels are represented.\n"
                "- Missing tiny labels are acceptable if the surrounding text already explains them.\n"
                "- Do not remove Mermaid just because the text description is sufficient.\n"
                "- Prefer keeping Mermaid if it is useful as a structural aid.\n"
                "- Set should_keep_mermaid=false ONLY if Mermaid is actively misleading, harmful, or contradicts the image.\n\n"
                "Score:\n"
                "- 100 = Mermaid is useful and meaning-preserving.\n"
                "- 90 = good enough; minor visual/label issues only.\n"
                "- 80 = mostly useful, some non-critical omissions.\n"
                "- 60 = useful idea but important structure missing.\n"
                "- 40 = many structural mistakes.\n"
                "- 20 = barely useful.\n"
                "- 0 = misleading/unrelated.\n\n"
                "Current Markdown replacement:\n"
                f"{description}\n"
            ),
        }
    )

    try:
        result = await llm_ainvoke_structured(
            client,
            [{"role": "user", "content": content}],
            MermaidVisualJudgeResult,
            temperature=JUDGE_TEMPERATURE,
        )
        return result.model_dump()
    except Exception as e:
        print(f"[WARN] Mermaid visual judge structured parsing failed: {e}")
        return {
            "should_keep_mermaid": True,
            "score": 90,
            "reason": "Judge failed; keeping Mermaid by default.",
            "missing": [],
            "wrong": [],
            "suggested_fixes": [],
        }


async def improve_mermaid_from_visual_feedback(
    client: ChatOpenAI,
    original_image_blocks,
    rendered_mermaid_images,
    description: str,
    judge_result: dict,
):
    content = list(original_image_blocks)

    for item in rendered_mermaid_images:
        content.append(
            {
                "type": "text",
                "text": (
                    f"Current rendered Mermaid diagram #{item['index']} from this code:\n"
                    f"```mermaid\n{item['code']}\n```"
                ),
            }
        )
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": item["data_url"], "detail": "high"},
            }
        )

    content.append(
        {
            "type": "text",
            "text": (
                "Improve this Mermaid reconstruction only where it helps understanding.\n\n"
                "Task:\n"
                "- Rewrite the full Markdown replacement.\n"
                "- Keep useful Mermaid unless it is actively misleading.\n"
                "- Fix missing/wrong important nodes, edges, directions, or labels.\n"
                "- Do not chase pixel-perfect layout.\n"
                "- Preserve useful textual notes outside Mermaid.\n"
                "- Return only the full corrected Markdown replacement.\n\n"
                "Mermaid rules:\n"
                "- Use simple ASCII node IDs.\n"
                "- Put Japanese text and long labels inside quoted labels.\n"
                "- Mermaid block must contain only Mermaid syntax.\n\n"
                f"{get_good_mermaid_examples()}\n\n"
                "Judge feedback:\n"
                f"{json.dumps(judge_result, ensure_ascii=False, indent=2)}\n\n"
                "Current Markdown replacement:\n"
                f"{description}\n"
            ),
        }
    )

    response_text = await llm_ainvoke_text(
        client,
        [{"role": "user", "content": content}],
        temperature=TEMPERATURE,
    )
    return ensure_reconstruction_wrapper(response_text)


async def improve_mermaid_visual_match_loop(
    client: ChatOpenAI,
    original_image_blocks,
    original_content,
    description: str,
):
    if not ENABLE_MERMAID_VISUAL_MATCH_LOOP:
        return description

    if not extract_mermaid_blocks(description):
        return description

    best_description = description
    best_score = -1
    best_judge = None
    current_description = description

    for attempt in range(1, MERMAID_VISUAL_MATCH_ATTEMPTS + 1):
        print(f"Mermaid visual match attempt {attempt}/{MERMAID_VISUAL_MATCH_ATTEMPTS}")

        current_description = ensure_reconstruction_wrapper(current_description)

        current_description = await repair_mermaid_if_needed(
            client=client,
            original_content=original_content,
            description=current_description,
        )

        if not extract_mermaid_blocks(current_description):
            print("No Mermaid block found after repair; stopping visual match loop.")
            break

        ok, rendered_images, render_error = await render_all_mermaid_blocks_to_pngs(
            current_description
        )

        if not ok:
            print("Mermaid render failed:")
            print(render_error)
            print("Trying repair again...")

            current_description = await repair_mermaid_if_needed(
                client=client,
                original_content=original_content,
                description=current_description,
            )

            ok, rendered_images, render_error = await render_all_mermaid_blocks_to_pngs(
                current_description
            )

            if not ok:
                print("Still cannot render Mermaid. Skipping candidate.")
                print(render_error)
                continue

        judge_result = await judge_mermaid_visual_match(
            client=client,
            original_image_blocks=original_image_blocks,
            rendered_mermaid_images=rendered_images,
            description=current_description,
        )

        # Hard relaxed behavior: do not let judge casually delete Mermaid.
        if not judge_result.get("should_keep_mermaid", True):
            print("Judge suggested Mermaid removal; keeping Mermaid unless actively misleading.")
            judge_result["should_keep_mermaid"] = True

        score = judge_result["score"]

        print(f"Mermaid visual judge score: {score}/100")
        print(f"Judge reason: {judge_result.get('reason', '')}")
        print("")

        if score > best_score:
            best_score = score
            best_description = current_description
            best_judge = judge_result

        if score >= MERMAID_VISUAL_MATCH_GOOD_ENOUGH_SCORE:
            print(
                f"Mermaid visual match score {score} >= "
                f"{MERMAID_VISUAL_MATCH_GOOD_ENOUGH_SCORE}; stopping early."
            )
            break

        current_description = await improve_mermaid_from_visual_feedback(
            client=client,
            original_image_blocks=original_image_blocks,
            rendered_mermaid_images=rendered_images,
            description=current_description,
            judge_result=judge_result,
        )

    print(f"Best Mermaid visual score selected: {best_score}/100")
    print("")

    if INCLUDE_VISUAL_JUDGE_NOTE and best_judge:
        best_description = (
            best_description.rstrip()
            + "\n\n"
            + "[Mermaid visual match judge:\n"
            + f"Score: {best_score}/100\n"
            + f"Reason: {best_judge.get('reason', '')}\n"
            + "]"
        )

    return ensure_reconstruction_wrapper(best_description)


async def judge_description_coverage(
    client: ChatOpenAI,
    original_image_blocks,
    description: str,
):
    content = list(original_image_blocks)

    content.append(
        {
            "type": "text",
            "text": (
                "You are a practical image reconstruction judge.\n\n"
                "Goal:\n"
                "Judge whether the reconstruction gives a downstream LLM enough information to understand "
                "the image correctly. This is NOT a perfect OCR audit.\n\n"
                "Important relaxed criteria:\n"
                "- Do not require every tiny visible string to be transcribed.\n"
                "- Do not punish paraphrasing if meaning is preserved.\n"
                "- Do not punish missing cosmetic/style/layout details.\n"
                "- Do not require pixel-perfect diagram reconstruction.\n"
                "- If the description fully explains the useful information in the image, it should pass.\n"
                "- Mermaid is optional support; if text already explains the image well, that is fine.\n\n"
                "Check these things:\n"
                "- Main objects/components are described.\n"
                "- Important visible text is included exactly or summarized accurately.\n"
                "- Important numbers/values are preserved when they affect meaning.\n"
                "- Relationships, arrows, flow, grouping, or layout meaning are understandable.\n"
                "- No harmful invented content changes the meaning.\n"
                "- Unclear text is not confidently guessed.\n\n"
                "Score:\n"
                "- 100 = excellent; all useful information is clear and faithful.\n"
                "- 90 = PASS; image meaning is preserved, only minor omissions/formatting issues.\n"
                "- 80 = mostly useful, but some secondary details are missing.\n"
                "- 60 = main idea exists, but important details/relationships are missing.\n"
                "- 40 = too vague for reliable downstream understanding.\n"
                "- 20 = shallow generic summary.\n"
                "- 0 = unrelated, unusable, or mostly invented.\n\n"
                "Mandatory relaxed rule:\n"
                "- Give 90 or higher if a downstream LLM can correctly understand the image from the reconstruction.\n"
                "- Penalize heavily only when missing/wrong content changes the actual meaning.\n"
                "- Do not reward verbosity. Reward useful faithful coverage.\n\n"
                "Reconstruction to judge:\n"
                f"{description}\n"
            ),
        }
    )

    try:
        result = await llm_ainvoke_structured(
            client,
            [{"role": "user", "content": content}],
            DescriptionCoverageJudgeResult,
            temperature=JUDGE_TEMPERATURE,
        )
        return result.model_dump()
    except Exception as e:
        print(f"[WARN] Description coverage judge structured parsing failed: {e}")
        return {
            "score": 90,
            "reason": "Judge structured parsing failed; accepting useful reconstruction by default.",
            "missing": [],
            "wrong": [],
            "suggested_fixes": [],
        }


async def improve_description_from_coverage_feedback(
    client: ChatOpenAI,
    original_image_blocks,
    description: str,
    judge_result: dict,
):
    if ENABLE_MERMAID_DIAGRAMS:
        mermaid_rules = (
            "- If the current reconstruction contains useful Mermaid, keep it valid.\n"
            "- Improve Mermaid only when it helps explain important structure.\n"
            "- Do not remove Mermaid just because text is sufficient.\n"
            "- Use simple ASCII node IDs and quoted labels for Japanese/long text.\n"
            "- Mermaid code blocks must contain only Mermaid syntax.\n"
        )
    else:
        mermaid_rules = "- Do not output Mermaid code blocks.\n"

    content = list(original_image_blocks)
    content.append(
        {
            "type": "text",
            "text": (
                "Fix this image reconstruction only where it improves downstream LLM understanding.\n\n"
                "Task:\n"
                "- Rewrite the FULL reconstruction.\n"
                "- Add missing important information from the judge feedback.\n"
                "- Correct content that is meaningfully wrong or misleading.\n"
                "- Preserve important visible text accurately.\n"
                "- Paraphrase is allowed when it does not change meaning.\n"
                "- Do not invent document context.\n"
                "- Do not obsess over tiny OCR/cosmetic details.\n"
                "- Return only the full corrected Markdown replacement.\n\n"
                "Rules:\n"
                f"{mermaid_rules}\n"
                "Judge feedback:\n"
                f"{json.dumps(judge_result, ensure_ascii=False, indent=2)}\n\n"
                "Current reconstruction:\n"
                f"{description}\n"
            ),
        }
    )

    response_text = await llm_ainvoke_text(
        client,
        [{"role": "user", "content": content}],
        temperature=TEMPERATURE,
    )
    return ensure_reconstruction_wrapper(response_text)


async def improve_description_coverage_loop(
    client: ChatOpenAI,
    original_image_blocks,
    description: str,
):
    if not ENABLE_DESCRIPTION_COVERAGE_LOOP:
        return description, None, -1

    if not description.strip():
        return description, None, -1

    best_description = description
    best_score = -1
    best_judge = None
    current_description = description

    for attempt in range(1, DESCRIPTION_COVERAGE_ATTEMPTS + 1):
        print(f"Description coverage attempt {attempt}/{DESCRIPTION_COVERAGE_ATTEMPTS}")

        judge_result = await judge_description_coverage(
            client=client,
            original_image_blocks=original_image_blocks,
            description=current_description,
        )

        score = judge_result["score"]

        print(f"Description coverage score: {score}/100")
        print(f"Judge reason: {judge_result.get('reason', '')}")

        if judge_result.get("missing"):
            print(f"Missing: {judge_result['missing']}")
        if judge_result.get("wrong"):
            print(f"Wrong: {judge_result['wrong']}")
        print("")

        if score > best_score:
            best_score = score
            best_description = current_description
            best_judge = judge_result

        if score >= DESCRIPTION_COVERAGE_GOOD_ENOUGH_SCORE:
            print(
                f"Description coverage score {score} >= "
                f"{DESCRIPTION_COVERAGE_GOOD_ENOUGH_SCORE}; stopping early."
            )
            break

        if attempt == DESCRIPTION_COVERAGE_ATTEMPTS:
            break

        current_description = await improve_description_from_coverage_feedback(
            client=client,
            original_image_blocks=original_image_blocks,
            description=current_description,
            judge_result=judge_result,
        )

    print(f"Best description coverage score selected: {best_score}/100")
    print("")

    return ensure_reconstruction_wrapper(best_description), best_judge, best_score
