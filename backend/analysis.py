"""Local, dependency-free text analysis: keywords, extractive summary and sentiment."""
import math
import re
from collections import Counter

STOP_WORDS = set(
    "a an the and or but if then so of to in on at by for with from as is are was were be been it its "
    "this that these those i you he she we they my your our their me us them not no yes do does did have "
    "has had just very really can could would should will about into than too also like get got one how "
    "what why when where who which anything something everything thanks thank tell joining honestly kept "
    "ended up".split()
)
POSITIVE = set("love great easy helpful good happy clear fast useful intuitive enjoy nice smooth".split())
NEGATIVE = set(
    "hate slow confusing frustrating hard difficult bug broken expensive annoying painful bad stuck "
    "problem issue complicated".split()
)

_SPEAKER_LABEL = re.compile(r"^\s*[A-Z][\w .'-]{0,30}:\s*", re.MULTILINE)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_WORD = re.compile(r"[a-z']{3,}")
_TOKEN = re.compile(r"[a-z']+")


def _content_words(text):
    return [w for w in _WORD.findall(text.lower()) if w not in STOP_WORDS]


def analyze(raw_text):
    """Return keywords, a 3-sentence summary and a sentiment label for a transcript."""
    text = _SPEAKER_LABEL.sub("", raw_text)
    freq = Counter(_content_words(text))
    keywords = [word for word, _ in freq.most_common(8)]

    scored = []
    for index, match in enumerate(_SENTENCE.findall(text)):
        sentence = match.strip()
        if len(sentence) <= 25 or sentence.endswith("?"):
            continue
        words = _content_words(sentence)
        score = sum(freq[w] for w in words) / math.sqrt(len(words) or 1)
        scored.append((score, index, sentence))
    top = sorted(scored, key=lambda item: item[0], reverse=True)[:3]
    summary = [sentence for _, _, sentence in sorted(top, key=lambda item: item[1])]

    tokens = _TOKEN.findall(text.lower())
    positive = sum(1 for t in tokens if t in POSITIVE)
    negative = sum(1 for t in tokens if t in NEGATIVE)
    if positive == negative:
        sentiment = "neutral"
    else:
        sentiment = "positive" if positive > negative else "negative"
    return {
        "keywords": keywords,
        "summary": summary,
        "sentiment": sentiment,
        "positive": positive,
        "negative": negative,
    }
