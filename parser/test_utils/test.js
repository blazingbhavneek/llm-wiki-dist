function imageUnitRegex() {
  return /<image-unit\b[^>]*>(?<body>[\s\S]*?)<\/image-unit>/gi
}

function imageMediaRegex() {
  return /<image-media\b[^>]*>[\s\S]*?<\/image-media>/gi
}

function imageMediaSingleRegex() {
  return /<image-media\b[^>]*>[\s\S]*?<\/image-media>/i
}

const IMAGE_DESCRIPTION_RE =
  /<image-description\b[^>]*>(?<description>[\s\S]*?)<\/image-description>/i

const IMAGE_SRC_RE =
  /<img\b[^>]*\bsrc=["'](?<src>data:image\/[^"']+)["'][^>]*>/i

function hasImageUnits(text) {
  if (typeof text !== 'string' || !text) return false

  return imageUnitRegex().test(text)
}

function countImageUnits(text) {
  if (typeof text !== 'string' || !text) return 0

  return Array.from(text.matchAll(imageUnitRegex())).length
}

function stripImageMedia(text) {
  /**
   * Remove embedded image media payloads from image-unit blocks.
   *
   * If an image-description exists, keep only the description.
   * If no description exists, remove only the image-media block and keep
   * any remaining non-media content inside the image-unit.
   */
  if (typeof text !== 'string' || !text) return text

  return text
    .replace(imageUnitRegex(), (...args) => {
      const groups = args[args.length - 1]
      const body = groups?.body || ''

      const description = IMAGE_DESCRIPTION_RE.exec(body)

      if (description) {
        return description.groups?.description?.trim() || ''
      }

      return body.replace(imageMediaRegex(), '').trim()
    })
    .trim()
}

function replaceImageMediaWithMarker(text, marker = '[image omitted]') {
  /**
   * Replace image-media blocks with a marker while preserving image-unit blocks.
   *
   * This keeps the image-unit structure visible but removes the heavy base64
   * image payload.
   */
  if (typeof text !== 'string' || !text) return text

  return text.replace(imageMediaRegex(), marker).trim()
}

function extractImageDescriptions(text) {
  /**
   * Return non-empty image-description values from image-unit blocks.
   */
  if (typeof text !== 'string' || !text) return []

  const descriptions = []

  for (const match of text.matchAll(imageUnitRegex())) {
    const body = match.groups?.body || ''
    const description = IMAGE_DESCRIPTION_RE.exec(body)
    const value = description?.groups?.description?.trim()

    if (value) descriptions.push(value)
  }

  return descriptions
}

function extractImageDataUrls(text) {
  /**
   * Return data:image/... URLs from image-media blocks.
   */
  if (typeof text !== 'string' || !text) return []

  const urls = []

  for (const match of text.matchAll(imageUnitRegex())) {
    const body = match.groups?.body || ''
    const media = imageMediaSingleRegex().exec(body)?.[0] || ''
    const src = IMAGE_SRC_RE.exec(media)?.groups?.src

    if (src) urls.push(src.trim())
  }

  return urls
}

function extractImageBase64(text) {
  /**
   * Return raw base64 payloads from image data URLs.
   */
  return extractImageDataUrls(text)
    .map((url) => {
      const commaIndex = url.indexOf(',')
      return commaIndex >= 0 ? url.slice(commaIndex + 1) : ''
    })
    .filter(Boolean)
}

function isEqual(actual, expected) {
  return JSON.stringify(actual) === JSON.stringify(expected)
}

function check(name, actual, expected) {
  /**
   * Tiny inline test helper. No Jest/Vitest needed.
   */
  const passed = isEqual(actual, expected)
  const status = passed ? 'PASS' : 'FAIL'

  console.log(`${status}: ${name}`)

  if (!passed) {
    console.log('  Expected:', expected)
    console.log('  Actual:  ', actual)
  }
}

function main() {
  const sampleMd = `
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
`.trim()

  const imageWithoutDescriptionMd = `
Before.

<image-unit>
  <image-media>
    <img src="data:image/jpeg;base64,CCC333" alt="">
  </image-media>
  <image-description>
    
  </image-description>
</image-unit>

After.
`.trim()

  const imageWithExtraTextMd = `
Before.

<image-unit>
  Caption-like text before media.
  <image-media>
    <img src="data:image/jpeg;base64,DDD444" alt="">
  </image-media>
  Caption-like text after media.
</image-unit>

After.
`.trim()

  const plainMd = `
# Plain Markdown

This document has no image units.
`.trim()

  const malformedMd = `
Before.

<image-unit>
  <image-media>
    <img src="data:image/jpeg;base64,EEE555" alt="">
  </image-media>
  <image-description>
    This block is missing the closing image-unit tag.
  </image-description>

After.
`.trim()

  console.log('\n--- Running inline tests ---\n')

  check(
    'hasImageUnits detects image-unit blocks',
    hasImageUnits(sampleMd),
    true,
  )

  check(
    'hasImageUnits returns false for plain Markdown',
    hasImageUnits(plainMd),
    false,
  )

  check(
    'countImageUnits counts multiple image-unit blocks',
    countImageUnits(sampleMd),
    2,
  )

  check(
    'countImageUnits returns 0 for plain Markdown',
    countImageUnits(plainMd),
    0,
  )

  check(
    'extractImageDescriptions returns non-empty descriptions',
    extractImageDescriptions(sampleMd),
    [
      'A chart showing quarterly revenue growth.',
      'A screenshot of the application dashboard.',
    ],
  )

  check(
    'extractImageDescriptions ignores empty descriptions',
    extractImageDescriptions(imageWithoutDescriptionMd),
    [],
  )

  check(
    'extractImageDataUrls returns data:image URLs',
    extractImageDataUrls(sampleMd),
    [
      'data:image/jpeg;base64,AAA111',
      'data:image/png;base64,BBB222',
    ],
  )

  check(
    'extractImageBase64 returns only base64 payloads',
    extractImageBase64(sampleMd),
    [
      'AAA111',
      'BBB222',
    ],
  )

  check(
    'stripImageMedia replaces image-unit with description when available',
    stripImageMedia(sampleMd),
    `
# Example Document

Some text before the image.

A chart showing quarterly revenue growth.

Some text between images.

A screenshot of the application dashboard.

Some text after the images.
`.trim(),
  )

  check(
    'stripImageMedia removes media and keeps remaining body when no description exists',
    stripImageMedia(imageWithExtraTextMd),
    `
Before.

Caption-like text before media.
  
  Caption-like text after media.

After.
`.trim(),
  )

  check(
    'replaceImageMediaWithMarker replaces image-media blocks only',
    replaceImageMediaWithMarker(
      imageWithoutDescriptionMd,
      '[image removed]',
    ),
    `
Before.

<image-unit>
  [image removed]
  <image-description>
    
  </image-description>
</image-unit>

After.
`.trim(),
  )

  check(
    'plain Markdown remains unchanged when stripping image media',
    stripImageMedia(plainMd),
    plainMd,
  )

  check(
    'malformed image-unit does not crash and remains unchanged',
    stripImageMedia(malformedMd),
    malformedMd,
  )

  check(
    'non-string null input for hasImageUnits',
    hasImageUnits(null),
    false,
  )

  check(
    'non-string null input for countImageUnits',
    countImageUnits(null),
    0,
  )

  check(
    'non-string null input for extractImageDescriptions',
    extractImageDescriptions(null),
    [],
  )

  console.log('\n--- Done ---\n')
}

main()