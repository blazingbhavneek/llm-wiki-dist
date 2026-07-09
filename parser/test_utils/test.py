import re


_IMAGE_UNIT_RE = re.compile(
    r"<image-unit\b[^>]*>(?P<body>.*?)</image-unit>",
    re.IGNORECASE | re.DOTALL,
)

_IMAGE_DESCRIPTION_RE = re.compile(
    r"<image-description\b[^>]*>(?P<description>.*?)</image-description>",
    re.IGNORECASE | re.DOTALL,
)

_IMAGE_MEDIA_RE = re.compile(
    r"<image-media\b[^>]*>.*?</image-media>",
    re.IGNORECASE | re.DOTALL,
)

_IMAGE_SRC_RE = re.compile(
    r"""<img\b[^>]*\bsrc=["'](?P<src>data:image/[^"']+)["'][^>]*>""",
    re.IGNORECASE | re.DOTALL,
)


def has_image_units(text: str) -> bool:
    """Return True if the text contains at least one image-unit block."""
    if not isinstance(text, str) or not text:
        return False

    return _IMAGE_UNIT_RE.search(text) is not None


def count_image_units(text: str) -> int:
    """Return the number of image-unit blocks."""
    if not isinstance(text, str) or not text:
        return 0

    return len(list(_IMAGE_UNIT_RE.finditer(text)))


def strip_image_media(text: str) -> str:
    """
    Remove embedded image media payloads from image-unit blocks.

    If an image-description exists, keep only the description.
    If no description exists, remove only the image-media block and keep
    any remaining non-media content inside the image-unit.
    """
    if not isinstance(text, str) or not text:
        return text

    def replace_image_unit(match: re.Match[str]) -> str:
        body = match.group("body")

        description = _IMAGE_DESCRIPTION_RE.search(body)
        if description:
            return description.group("description").strip()

        return _IMAGE_MEDIA_RE.sub("", body).strip()

    return _IMAGE_UNIT_RE.sub(replace_image_unit, text).strip()


def replace_image_media_with_marker(
    text: str,
    marker: str = "[image omitted]",
) -> str:
    """
    Replace image-media blocks with a marker while preserving image-unit blocks.

    This keeps the image-unit structure visible but removes the heavy base64
    image payload.
    """
    if not isinstance(text, str) or not text:
        return text

    return _IMAGE_MEDIA_RE.sub(marker, text).strip()


def extract_image_descriptions(text: str) -> list[str]:
    """Return non-empty image-description values from image-unit blocks."""
    if not isinstance(text, str) or not text:
        return []

    descriptions: list[str] = []

    for unit_match in _IMAGE_UNIT_RE.finditer(text):
        body = unit_match.group("body")
        description = _IMAGE_DESCRIPTION_RE.search(body)

        if description:
            value = description.group("description").strip()
            if value:
                descriptions.append(value)

    return descriptions


def extract_image_data_urls(text: str) -> list[str]:
    """Return data:image/... URLs from image-media blocks."""
    if not isinstance(text, str) or not text:
        return []

    urls: list[str] = []

    for unit_match in _IMAGE_UNIT_RE.finditer(text):
        body = unit_match.group("body")
        media = _IMAGE_MEDIA_RE.search(body)

        if not media:
            continue

        src = _IMAGE_SRC_RE.search(media.group(0))

        if src:
            urls.append(src.group("src").strip())

    return urls


def extract_image_base64(text: str) -> list[str]:
    """Return raw base64 payloads from image data URLs."""
    values: list[str] = []

    for data_url in extract_image_data_urls(text):
        if "," in data_url:
            values.append(data_url.split(",", 1)[1])

    return values


def check(name: str, actual, expected) -> None:
    """Tiny inline test helper. No pytest needed."""
    passed = actual == expected
    status = "PASS" if passed else "FAIL"

    print(f"{status}: {name}")

    if not passed:
        print("  Expected:", repr(expected))
        print("  Actual:  ", repr(actual))


def main() -> None:
    sample_md = """
# Example Document

Some text before the image.

<image-unit>
  <image-media>
    <img src="data:image/jpeg;base64,AAA111" alt="">
  </image-media>
  <image-description>
    A chart showing quarterly revenue growth.
  </image-description>
</image-unit>

Some text between images.

<image-unit>
  <image-media>
    <img src="data:image/png;base64,BBB222" alt="">
  </image-media>
  <image-description>
    A screenshot of the application dashboard.
  </image-description>
</image-unit>

Some text after the images.
""".strip()

    image_without_description_md = """
Before.

<image-unit>
  <image-media>
    <img src="data:image/jpeg;base64,CCC333" alt="">
  </image-media>
  <image-description>
    
  </image-description>
</image-unit>

After.
""".strip()

    image_with_extra_text_md = """
Before.

<image-unit>
  Caption-like text before media.
  <image-media>
    <img src="data:image/jpeg;base64,DDD444" alt="">
  </image-media>
  Caption-like text after media.
</image-unit>

After.
""".strip()

    plain_md = """
# Plain Markdown

This document has no image units.
""".strip()

    malformed_md = """
Before.

<image-unit>
  <image-media>
    <img src="data:image/jpeg;base64,EEE555" alt="">
  </image-media>
  <image-description>
    This block is missing the closing image-unit tag.
  </image-description>

After.
""".strip()

    print("\n--- Running inline tests ---\n")

    check(
        "has_image_units detects image-unit blocks",
        has_image_units(sample_md),
        True,
    )

    check(
        "has_image_units returns False for plain Markdown",
        has_image_units(plain_md),
        False,
    )

    check(
        "count_image_units counts multiple image-unit blocks",
        count_image_units(sample_md),
        2,
    )

    check(
        "count_image_units returns 0 for plain Markdown",
        count_image_units(plain_md),
        0,
    )

    check(
        "extract_image_descriptions returns non-empty descriptions",
        extract_image_descriptions(sample_md),
        [
            "A chart showing quarterly revenue growth.",
            "A screenshot of the application dashboard.",
        ],
    )

    check(
        "extract_image_descriptions ignores empty descriptions",
        extract_image_descriptions(image_without_description_md),
        [],
    )

    check(
        "extract_image_data_urls returns data:image URLs",
        extract_image_data_urls(sample_md),
        [
            "data:image/jpeg;base64,AAA111",
            "data:image/png;base64,BBB222",
        ],
    )

    check(
        "extract_image_base64 returns only base64 payloads",
        extract_image_base64(sample_md),
        [
            "AAA111",
            "BBB222",
        ],
    )

    check(
        "strip_image_media replaces image-unit with description when available",
        strip_image_media(sample_md),
        """
# Example Document

Some text before the image.

A chart showing quarterly revenue growth.

Some text between images.

A screenshot of the application dashboard.

Some text after the images.
""".strip(),
    )

    check(
        "strip_image_media removes media and keeps remaining body when no description exists",
        strip_image_media(image_with_extra_text_md),
        """
Before.

Caption-like text before media.
  
  Caption-like text after media.

After.
""".strip(),
    )

    check(
        "replace_image_media_with_marker replaces image-media blocks only",
        replace_image_media_with_marker(
            image_without_description_md,
            marker="[image removed]",
        ),
        """
Before.

<image-unit>
  [image removed]
  <image-description>
    
  </image-description>
</image-unit>

After.
""".strip(),
    )

    check(
        "plain Markdown remains unchanged when stripping image media",
        strip_image_media(plain_md),
        plain_md,
    )

    check(
        "malformed image-unit does not crash and remains unchanged",
        strip_image_media(malformed_md),
        malformed_md,
    )

    check(
        "non-string None input for has_image_units",
        has_image_units(None),
        False,
    )

    check(
        "non-string None input for count_image_units",
        count_image_units(None),
        0,
    )

    check(
        "non-string None input for extract_image_descriptions",
        extract_image_descriptions(None),
        [],
    )

    print("\n--- Done ---\n")


if __name__ == "__main__":
    main()
