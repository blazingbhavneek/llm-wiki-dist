"""Corpus vocabulary: repair a spoken question before it reaches retrieval.

Speech recognition does not know the book. It hears 「エムピーエフ エムエフエス
オープン」 and writes exactly that, while the corpus spells the same thing
``mpf_mfs_open``. No amount of hybrid search recovers an identifier that was
never transcribed, so the question is matched against the vocabulary the corpus
actually contains -- cluster names, node keywords, and identifiers harvested
from titles, claims and bodies -- before any search runs.

Everything here is pure data and string work: no model call, no I/O, and no
dependency on the graph package, so the matcher is testable on its own.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping

# Matching keys are folded to bare alphanumerics: `mpf_mfs_open`, `MPF-MFS-OPEN`
# and a transliterated `mpfmfsopen` all have to collapse onto the same string.
_FOLD_RE = re.compile(r"[^0-9a-z]+")
# Leading underscores are part of the name, not punctuation around it:
# `__syncthreads`, `__managed__` and `_system` are how CUDA spells its
# builtins. Starting the run at [A-Za-z] captured "syncthreads" instead, which
# then failed the decoration check against its own corpus spelling — the
# question had the underscores all along and the tokenizer dropped them.
_LATIN_RUN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
_KANA_RUN_RE = re.compile(r"[ぁ-んァ-ヴー・]+")
_FILENAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,60}\.[A-Za-z][A-Za-z0-9]{0,4}")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,60}")
# Markdown marks identifiers as code. Inside a span, a plain lowercase word is
# an identifier (`filenum`, `bufsize`) — outside one it is just English.
_CODE_SPAN_RE = re.compile(r"`([^`\n]{1,80})`")
_HIRAGANA_START, _HIRAGANA_END = 0x3041, 0x3096
_KANA_SHIFT = 0x60

# Identifier-shaped terms are the ones worth pinning a whole answer to.
PINNABLE_KINDS = ("identifier", "filename")

# Katakana spelling of the *English letter names*. ASR writes an acronym the way
# it was pronounced, so `MPF` arrives as エムピーエフ and only this table gets it
# back. Checked before the syllable table, longest reading first.
_LETTER_READINGS: tuple[tuple[str, str], ...] = (
    ("ダブリュー", "w"),
    ("ダブル", "w"),
    ("エックス", "x"),
    ("エイチ", "h"),
    ("エッチ", "h"),
    ("ジェイ", "j"),
    ("ジェー", "j"),
    ("キュー", "q"),
    ("ゼット", "z"),
    ("アール", "r"),
    ("ディー", "d"),
    ("ティー", "t"),
    ("エム", "m"),
    ("エヌ", "n"),
    ("エフ", "f"),
    ("エル", "l"),
    ("エス", "s"),
    ("ブイ", "v"),
    ("ワイ", "y"),
    ("アイ", "i"),
    ("ケー", "k"),
    ("ビー", "b"),
    ("シー", "c"),
    ("ジー", "g"),
    ("ピー", "p"),
    ("イー", "e"),
    ("エー", "a"),
    ("オー", "o"),
    ("ユー", "u"),
)

# Ordinary katakana readings, digraphs first so ジャ never becomes "jiya".
_SYLLABLE_READINGS: tuple[tuple[str, str], ...] = (
    ("キャ", "kya"), ("キュ", "kyu"), ("キョ", "kyo"),
    ("シャ", "sha"), ("シュ", "shu"), ("ショ", "sho"), ("シェ", "she"),
    ("チャ", "cha"), ("チュ", "chu"), ("チョ", "cho"), ("チェ", "che"),
    ("ニャ", "nya"), ("ニュ", "nyu"), ("ニョ", "nyo"),
    ("ヒャ", "hya"), ("ヒュ", "hyu"), ("ヒョ", "hyo"),
    ("ミャ", "mya"), ("ミュ", "myu"), ("ミョ", "myo"),
    ("リャ", "rya"), ("リュ", "ryu"), ("リョ", "ryo"),
    ("ギャ", "gya"), ("ギュ", "gyu"), ("ギョ", "gyo"),
    ("ジャ", "ja"), ("ジュ", "ju"), ("ジョ", "jo"), ("ジェ", "je"),
    ("ビャ", "bya"), ("ビュ", "byu"), ("ビョ", "byo"),
    ("ピャ", "pya"), ("ピュ", "pyu"), ("ピョ", "pyo"),
    ("ファ", "fa"), ("フィ", "fi"), ("フェ", "fe"), ("フォ", "fo"),
    ("ティ", "ti"), ("トゥ", "tu"), ("ディ", "di"), ("ドゥ", "du"), ("デュ", "du"),
    ("ウィ", "wi"), ("ウェ", "we"), ("ウォ", "wo"),
    ("ヴァ", "va"), ("ヴィ", "vi"), ("ヴェ", "ve"), ("ヴォ", "vo"),
    ("ア", "a"), ("イ", "i"), ("ウ", "u"), ("エ", "e"), ("オ", "o"),
    ("カ", "ka"), ("キ", "ki"), ("ク", "ku"), ("ケ", "ke"), ("コ", "ko"),
    ("サ", "sa"), ("シ", "shi"), ("ス", "su"), ("セ", "se"), ("ソ", "so"),
    ("タ", "ta"), ("チ", "chi"), ("ツ", "tsu"), ("テ", "te"), ("ト", "to"),
    ("ナ", "na"), ("ニ", "ni"), ("ヌ", "nu"), ("ネ", "ne"), ("ノ", "no"),
    ("ハ", "ha"), ("ヒ", "hi"), ("フ", "fu"), ("ヘ", "he"), ("ホ", "ho"),
    ("マ", "ma"), ("ミ", "mi"), ("ム", "mu"), ("メ", "me"), ("モ", "mo"),
    ("ヤ", "ya"), ("ユ", "yu"), ("ヨ", "yo"),
    ("ラ", "ra"), ("リ", "ri"), ("ル", "ru"), ("レ", "re"), ("ロ", "ro"),
    ("ワ", "wa"), ("ヲ", "o"), ("ン", "n"),
    ("ガ", "ga"), ("ギ", "gi"), ("グ", "gu"), ("ゲ", "ge"), ("ゴ", "go"),
    ("ザ", "za"), ("ジ", "ji"), ("ズ", "zu"), ("ゼ", "ze"), ("ゾ", "zo"),
    ("ダ", "da"), ("ヂ", "ji"), ("ヅ", "zu"), ("デ", "de"), ("ド", "do"),
    ("バ", "ba"), ("ビ", "bi"), ("ブ", "bu"), ("ベ", "be"), ("ボ", "bo"),
    ("パ", "pa"), ("ピ", "pi"), ("プ", "pu"), ("ペ", "pe"), ("ポ", "po"),
    ("ヴ", "vu"),
    ("ァ", "a"), ("ィ", "i"), ("ゥ", "u"), ("ェ", "e"), ("ォ", "o"),
    ("ャ", "ya"), ("ュ", "yu"), ("ョ", "yo"),
)


def fold(text: str) -> str:
    """Collapse a term to its bare alphanumeric matching key."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return _FOLD_RE.sub("", normalized)


# Sounds Japanese does not distinguish from the English spelling. カ行 always
# transliterates to `k`, so 「クーダ」 can never reach `cuda` by spelling alone;
# `l`/`r` and `v`/`b` collapse the same way.
_PHONETIC_MAP = str.maketrans({"c": "k", "l": "r", "v": "b", "q": "k"})


def phonetic(key: str) -> str:
    """Folded key reduced to the distinctions katakana can actually carry."""
    text = key.replace("x", "ks").translate(_PHONETIC_MAP)
    # Doubled letters are a spelling convention, not a heard difference.
    return re.sub(r"(.)\1+", r"\1", text)


def to_katakana(text: str) -> str:
    """Hiragana in, katakana out. ASR output mixes the two for the same sound."""
    return "".join(
        chr(ord(ch) + _KANA_SHIFT)
        if _HIRAGANA_START <= ord(ch) <= _HIRAGANA_END
        else ch
        for ch in str(text or "")
    )


def _transliterate(kana: str, *, letters_first: bool) -> str:
    """One reading of a katakana run.

    Two readings are possible for the same characters and neither is reliably
    correct: ケー is the letter ``k`` inside an acronym and ``ke`` inside a
    loanword. Both are produced and the caller keeps whichever matches the
    corpus, which is cheaper and more accurate than guessing.
    """
    tables = (
        (_LETTER_READINGS, _SYLLABLE_READINGS)
        if letters_first
        else (_SYLLABLE_READINGS,)
    )
    out: list[str] = []
    index = 0
    while index < len(kana):
        char = kana[index]
        if char in "ー・":
            index += 1
            continue
        if char == "ッ":
            # Doubles the next consonant; the following iteration writes it.
            index += 1
            continue
        matched = False
        for table in tables:
            for reading, latin in table:
                if kana.startswith(reading, index):
                    out.append(latin)
                    index += len(reading)
                    matched = True
                    break
            if matched:
                break
        if not matched:
            index += 1
    return "".join(out)


def kana_readings(text: str) -> list[str]:
    """Every plausible latin reading of a kana run, best-guess order."""
    kana = to_katakana(text)
    if not kana:
        return []
    readings = [
        _transliterate(kana, letters_first=True),
        _transliterate(kana, letters_first=False),
    ]
    return [reading for reading in dict.fromkeys(readings) if reading]


@dataclass(frozen=True)
class VocabTerm:
    """One thing the corpus can actually be asked about."""

    surface: str
    kind: str  # identifier | filename | cluster | keyword
    key: str = ""

    def with_key(self) -> "VocabTerm":
        return self if self.key else VocabTerm(self.surface, self.kind, fold(self.surface))


@dataclass(frozen=True)
class VocabHit:
    surface: str
    kind: str
    score: float
    heard: str


@dataclass(frozen=True)
class VocabularyMatch:
    """Result of repairing one question."""

    query: str
    identifiers: list[str] = field(default_factory=list)
    clusters: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    hits: list[VocabHit] = field(default_factory=list)

    @property
    def pinned(self) -> str:
        return self.identifiers[0] if self.identifiers else ""

    def public(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "identifiers": list(self.identifiers),
            "clusters": list(self.clusters),
            "keywords": list(self.keywords),
            "repairs": [
                {
                    "heard": hit.heard,
                    "matched": hit.surface,
                    "kind": hit.kind,
                    "score": round(hit.score, 3),
                }
                for hit in self.hits
                if fold(hit.heard) != fold(hit.surface)
            ],
        }


# Terms shorter than this match everything and mean nothing.
_MIN_KEY_LEN = 3
# A fuzzy candidate has to be within this length band of the heard span before
# an O(n*m) sequence comparison is worth running at all.
_LENGTH_BAND = 0.45
_DEFAULT_THRESHOLD = 0.76
# Below this, a similarity ratio says nothing: 「において」 reads as "nioite" and
# scores 0.80 against the keyword "Note". Short terms have to match exactly.
_MIN_FUZZY_LEN = 5
# Kana is a guess about spelling and 0.76 is the price of repairing it. Latin
# was transcribed, so an approximate hit there is near-certainly a different
# word than the one written: "capability" scores 0.78 against "compatibility".
_LATIN_THRESHOLD = 0.88


class Vocabulary:
    """Folded lookup over corpus terms, with a bounded fuzzy fallback."""

    def __init__(self, terms: Iterable[VocabTerm], *, max_terms: int = 60_000) -> None:
        by_key: dict[str, VocabTerm] = {}
        japanese: list[VocabTerm] = []
        # Kinds are indexed by surface, never by folded key. Folding exists so
        # a mishearing can reach a spelling it does not share characters with,
        # and it strips exactly the punctuation that distinguishes a name from
        # a word: `__managed__`, `_system` and `_ALLOCATION` fold onto
        # "managed", "system" and "allocation". Answering "is this token a
        # name?" from a folded hit reports a *different* term's kind, which is
        # how an English word inherits an identifier's status.
        kind_by_surface: dict[str, str] = {}
        for term in terms:
            resolved = term.with_key()
            surface = resolved.surface.strip()
            if not surface:
                continue
            # Case-sensitive, for the same reason the key is not used: `DEVICE`
            # is a macro and `device` is the English word, `_system` is a name
            # and `System` is a noun. Every fold or case merge here hands a
            # common word an identifier's status. Identifiers are yielded
            # before keywords, so they still win a true surface collision.
            kind_by_surface.setdefault(surface, resolved.kind)
            if len(resolved.key) >= _MIN_KEY_LEN:
                # First writer wins: identifiers are inserted before keywords,
                # so `mpf_mfs_open` is not shadowed by a keyword spelled the same.
                by_key.setdefault(resolved.key, resolved)
            elif _has_japanese(surface) and len(surface) >= 2:
                japanese.append(resolved)
            if len(by_key) >= max_terms:
                break
        # Japanese cluster/keyword surfaces are matched by substring, so index
        # them by character bigram instead of scanning every term per question.
        for term in list(by_key.values()):
            if _has_japanese(term.surface) and len(term.surface) >= 2:
                japanese.append(term)

        self._by_key = by_key
        self._kind_by_surface = kind_by_surface
        self._latin_keys = [key for key in by_key if key.isascii()]
        self._by_phonetic: dict[str, VocabTerm] = {}
        for key in self._latin_keys:
            self._by_phonetic.setdefault(phonetic(key), by_key[key])
        self._by_bigram: dict[str, list[VocabTerm]] = {}
        for term in japanese:
            for bigram in _bigrams(term.surface):
                bucket = self._by_bigram.setdefault(bigram, [])
                if len(bucket) < 64:
                    bucket.append(term)

    # region construction

    @classmethod
    def empty(cls) -> "Vocabulary":
        return cls([])

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]], **kwargs: Any) -> "Vocabulary":
        """Build from node-shaped mappings.

        ``keywords_json`` is not reliably populated in every corpus, so
        identifiers are also harvested straight out of titles, claims, source
        paths and bodies. A vocabulary that depends on one enrichment pass
        having run is a vocabulary that is empty exactly when it is needed.
        """
        return cls(_terms_from_rows(rows), **kwargs)

    def knows(self, term: str) -> bool:
        """Exact membership, folded. Used to decide whether a term is a thing."""
        key = fold(term)
        return bool(key) and key in self._by_key

    def kind_of(self, term: str) -> str:
        """The kind this exact spelling was harvested as, or "".

        Two things make membership the wrong test. Enrichment keywords put
        ordinary English nouns ("System", "Memory") in the same table as
        ``mpf_mfs_open``; and the folded key that lets a mishearing find a
        spelling also erases the punctuation that made it a name, so "System"
        would otherwise answer with ``_system``'s kind. Matching is deliberately
        loose and classification has to be exact, so this compares surfaces
        exactly, case included.
        """
        return self._kind_by_surface.get(str(term or "").strip(), "")

    def __len__(self) -> int:
        return len(self._by_key)

    def __bool__(self) -> bool:
        return bool(self._by_key) or bool(self._by_bigram)

    # endregion construction

    def match(
        self,
        question: str,
        *,
        limit: int = 6,
        threshold: float = _DEFAULT_THRESHOLD,
    ) -> VocabularyMatch:
        """Repair one question. Never raises; an empty vocabulary is a no-op."""
        question = str(question or "").strip()
        if not question or not self:
            return VocabularyMatch(query=question)

        best: dict[str, VocabHit] = {}

        def offer(hit: VocabHit) -> None:
            current = best.get(hit.surface)
            if current is None or hit.score > current.score:
                best[hit.surface] = hit

        for span, keys, heard_as_kana in _question_candidates(question):
            for key in keys:
                if len(key) < _MIN_KEY_LEN:
                    continue
                exact = self._by_key.get(key)
                if exact is not None:
                    if _decoration_differs(span, exact.surface, heard_as_kana):
                        # A single latin word arrived spelled correctly, so the
                        # punctuation it does *not* have is meaningful: "Managed"
                        # is not `__managed__`, "Memory" is not `_MEMORY`, "API"
                        # is not `_API`. Folding exists to rescue a mishearing,
                        # and letting it rewrite correctly transcribed English
                        # into decorated identifiers pins the whole answer to
                        # the wrong subject.
                        continue
                    offer(VocabHit(exact.surface, exact.kind, 1.0, span))
                    continue
                near = (
                    self._closest(
                        key, threshold if heard_as_kana else _LATIN_THRESHOLD
                    )
                    if _fuzzy_allowed(span, heard_as_kana)
                    else None
                )
                if near is not None:
                    term, score = near
                    offer(VocabHit(term.surface, term.kind, score, span))
                    continue
                if heard_as_kana:
                    # Last resort, and only for something that was spoken:
                    # compare what the two spellings would have sounded like.
                    sounded = self._by_phonetic.get(phonetic(key))
                    if sounded is not None:
                        offer(VocabHit(sounded.surface, sounded.kind, 0.9, span))

        for term in self._japanese_candidates(question):
            offer(VocabHit(term.surface, term.kind, 0.95, term.surface))

        ranked = sorted(
            best.values(),
            key=lambda hit: (hit.score, len(hit.surface)),
            reverse=True,
        )[:limit]

        identifiers = [hit.surface for hit in ranked if hit.kind in PINNABLE_KINDS]
        clusters = [hit.surface for hit in ranked if hit.kind == "cluster"]
        keywords = [hit.surface for hit in ranked if hit.kind == "keyword"]
        return VocabularyMatch(
            query=_rewrite(question, [hit.surface for hit in ranked]),
            identifiers=identifiers,
            clusters=clusters,
            keywords=keywords,
            hits=ranked,
        )

    def _closest(self, key: str, threshold: float) -> tuple[VocabTerm, float] | None:
        """Best fuzzy latin match, or nothing.

        ASR drops and invents syllables, so an exact fold rarely survives a
        long acronym. Candidates are pruned by first character and length band
        before the quadratic comparison, which keeps this well under a
        millisecond even on a large corpus.
        """
        if not key.isascii() or len(key) < _MIN_FUZZY_LEN:
            return None
        low = max(_MIN_FUZZY_LEN, int(len(key) * (1 - _LENGTH_BAND)))
        high = int(len(key) * (1 + _LENGTH_BAND)) + 1
        head = key[0]
        matcher = SequenceMatcher()
        matcher.set_seq2(key)
        best: tuple[VocabTerm, float] | None = None
        for candidate in self._latin_keys:
            if not (low <= len(candidate) <= high) or candidate[0] != head:
                continue
            matcher.set_seq1(candidate)
            # Cheap upper bounds first; .ratio() is the expensive call.
            if matcher.real_quick_ratio() < threshold:
                continue
            if matcher.quick_ratio() < threshold:
                continue
            score = matcher.ratio()
            if score >= threshold and (best is None or score > best[1]):
                best = (self._by_key[candidate], score)
        return best

    def _japanese_candidates(self, question: str) -> list[VocabTerm]:
        seen: set[str] = set()
        found: list[VocabTerm] = []
        for bigram in dict.fromkeys(_bigrams(question)):
            for term in self._by_bigram.get(bigram, ()):
                if term.surface in seen or term.surface not in question:
                    continue
                seen.add(term.surface)
                found.append(term)
        return found


def _fuzzy_allowed(span: str, heard_as_kana: bool) -> bool:
    """Whether an approximate match may be attempted for this span.

    Kana is always a guess about spelling, so it always earns one. Latin does
    not: it arrived transcribed, and the only reason to approximate is a
    genuine mis-transcription of a single word. A *joined* latin span is
    already a guess — the tokens were glued together on the chance they form
    one name — and approximating on top of that guess is what turns "API x"
    into ``_API`` and "Memory API" into "memory mapping".
    """
    return heard_as_kana or " " not in span.strip()


def _decoration_differs(span: str, surface: str, heard_as_kana: bool) -> bool:
    """Whether a folded hit joined two spellings that are not the same token.

    Only latin spans are judged. Kana carries no punctuation at all, so a kana
    reading is *expected* to reach ``mpf_mfs_open`` from 「エムピーエフ エムエフ
    エス オープン」 and must keep the loose comparison. A multi-token latin span
    is dictated one piece at a time and its separators are guesses too, so it
    stays loose as well; a single latin word is not, and for it the underscores
    and dots of the corpus spelling have to be present.
    """
    if heard_as_kana or " " in span.strip():
        return False
    return span.strip().casefold() != surface.strip().casefold()


def _has_japanese(text: str) -> bool:
    return bool(re.search(r"[぀-ヿ㐀-鿿]", text))


def _bigrams(text: str) -> list[str]:
    compact = "".join(str(text or "").split())
    return [compact[i : i + 2] for i in range(len(compact) - 1)]


def _question_candidates(question: str) -> list[tuple[str, list[str], bool]]:
    """Every span of the question that could be a mangled corpus term.

    Adjacent runs are joined as well as taken alone: an identifier is dictated
    one piece at a time (「エムピーエフ エムエフエス オープン」) and only the
    joined reading resolves to ``mpf_mfs_open``. The flag records whether the
    span was heard in kana, where the spelling is a guess.
    """
    latin: list[tuple[str, list[str], bool]] = [
        (match.group(0), [fold(match.group(0))], False)
        for match in _LATIN_RUN_RE.finditer(question)
    ]
    kana: list[tuple[str, list[str], bool]] = [
        (
            match.group(0),
            [fold(reading) for reading in kana_readings(match.group(0))],
            True,
        )
        for match in _KANA_RUN_RE.finditer(question)
    ]

    candidates: list[tuple[str, list[str], bool]] = latin + kana
    # Join only within one script. An identifier dictated in pieces arrives as
    # consecutive kana runs, and a latin name spelled with spaces as consecutive
    # latin runs; a window mixing the two is a name plus the grammar around it.
    # 「cudaFree は をする」 is not a term, but glue it together and it scores
    # 0.82 against ``cudaFreeHost`` — a different function, pinned into the
    # scope line as though the user had asked for it.
    for tokens in (latin, kana):
        for width in (2, 3, 4):
            for start in range(len(tokens) - width + 1):
                window = tokens[start : start + width]
                span = " ".join(token for token, _keys, _kana in window)
                # One joined key per position choice would explode
                # combinatorially; take the primary reading of each token plus
                # the all-syllable one.
                primary = "".join(keys[0] for _t, keys, _k in window if keys)
                secondary = "".join(keys[-1] for _t, keys, _k in window if keys)
                candidates.append(
                    (
                        span,
                        list(dict.fromkeys([primary, secondary])),
                        window[0][2],
                    )
                )
    return candidates


def _rewrite(question: str, surfaces: list[str]) -> str:
    """Append matched corpus spellings; never remove what the user said."""
    additions = [
        surface
        for surface in dict.fromkeys(surfaces)
        if surface and surface.casefold() not in question.casefold()
    ]
    if not additions:
        return question
    return f"{question} {' '.join(additions)}"


def _terms_from_rows(rows: Iterable[Mapping[str, Any]]) -> Iterable[VocabTerm]:
    """Harvest terms in priority order: identifiers win key collisions."""
    identifiers: dict[str, VocabTerm] = {}
    others: list[VocabTerm] = []
    # An all-lowercase word in a code span is the one ambiguous case: `filenum`
    # and `bufsize` are names, `compute` and `device` are English that happened
    # to be marked up. Nothing about the spelling separates them, but their
    # distribution does — a name is written as code nearly every time it is
    # written at all, while a word is mostly prose. Both counts are collected
    # over the whole corpus and the decision is made once at the end.
    span_hits: dict[str, int] = {}
    prose_hits: dict[str, int] = {}
    for row in rows:
        title = str(row.get("title") or "")
        cluster = str(row.get("cluster") or "").strip()
        source_path = str(row.get("source_path") or "")
        document = str(row.get("original_document_name") or "")
        keywords = _as_list(row.get("keywords"))
        claims = _as_list(row.get("claims"))
        body = str(row.get("body") or "")

        if cluster:
            others.append(VocabTerm(cluster, "cluster"))
        for keyword in keywords:
            text = str(keyword or "").strip()
            if text:
                others.append(VocabTerm(text, "keyword"))

        # Identifier-shaped text is what a speaker garbles and what an answer
        # has to be scoped to, so it is harvested from every field including
        # the body, not only from enrichment output.
        scanned = " ".join([title, source_path, document, *claims, *keywords])
        for name in _FILENAME_RE.findall(scanned) + _FILENAME_RE.findall(body):
            if not _is_hashish(name):
                identifiers.setdefault(name, VocabTerm(name, "filename"))
        for name in _IDENTIFIER_RE.findall(scanned):
            if _is_identifier_like(name):
                identifiers.setdefault(name, VocabTerm(name, "identifier"))
        for name in _IDENTIFIER_RE.findall(body):
            if "_" in name and _is_identifier_like(name):
                identifiers.setdefault(name, VocabTerm(name, "identifier"))
        marked_up = f"{scanned}\n{body}"
        for span in _CODE_SPAN_RE.findall(marked_up):
            for name in _IDENTIFIER_RE.findall(span):
                if _is_code_identifier(name):
                    identifiers.setdefault(name, VocabTerm(name, "identifier"))
                elif _is_lower_candidate(name):
                    span_hits[name] = span_hits.get(name, 0) + 1
        # Prose is the body with its code spans removed, and nothing else.
        # Titles, paths and claims are metadata: a name appears bare in them
        # routinely, so counting those as prose would argue that every title is
        # evidence against the thing it names.
        for name in _IDENTIFIER_RE.findall(_CODE_SPAN_RE.sub(" ", body)):
            folded = name.casefold()
            prose_hits[folded] = prose_hits.get(folded, 0) + 1

    for name, hits in span_hits.items():
        # Ties go to prose: a word that is as often plain text as it is code is
        # not a name worth pinning an answer or a research stage to.
        if hits > prose_hits.get(name, 0):
            identifiers.setdefault(name, VocabTerm(name, "identifier"))

    yield from identifiers.values()
    yield from others


def _is_identifier_like(name: str) -> bool:
    if len(name) < 4 or _is_hashish(name):
        return False
    # An underscore, a digit or an all-caps spelling is what separates a real
    # identifier from an ordinary English word appearing in a title.
    if "_" in name or any(ch.isdigit() for ch in name) or name.isupper():
        return True
    # A second capital does too. English capitalises the first letter of a word
    # and no more, so `NVLink`, `PCIe` and `GPUDirect` are names while `System`,
    # `Device` and `Allocated` are sentences starting. These are precisely the
    # terms a reader asks a follow-up question about.
    return sum(ch.isupper() for ch in name) >= 2 and any(ch.islower() for ch in name)


# Lowercase immediately followed by uppercase: `cudaMallocManaged` matches,
# `PCIe` does not — its capitals run the other way.
_CAMEL_RE = re.compile(r"[a-z][A-Z]")


def _is_code_identifier(name: str) -> bool:
    """Whether a term inside a code span is unambiguously a name.

    Documentation marks up ordinary prose constantly, so a span alone proves
    nothing. Camel case and the underscore/digit/shouted shapes do prove it:
    no English sentence contains `cudaMallocManaged` or `MPF_MODE_SYNC`. A
    capitalised word (`System`, `Allocated`, `PCIe`) proves the opposite, and
    an all-lowercase one proves nothing either way — see `_is_lower_candidate`.
    """
    if len(name) < 4 or _is_hashish(name):
        return False
    return _is_identifier_like(name) or bool(_CAMEL_RE.search(name))


def _is_lower_candidate(name: str) -> bool:
    """An all-lowercase code-span word, to be judged on distribution instead."""
    return len(name) >= 4 and name.islower() and not _is_hashish(name)


_HEX_RE = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)


def _is_hashish(name: str) -> bool:
    """Content hashes are in every corpus and nobody has ever asked about one.

    They are long, unpronounceable, and numerous enough to slow the fuzzy
    candidate scan measurably.
    """
    stem = name.rsplit(".", 1)[0]
    return len(stem) >= 16 and bool(_HEX_RE.match(stem))


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return []
