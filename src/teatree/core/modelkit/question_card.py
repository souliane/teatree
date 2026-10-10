"""The owner question as a short card: what is stored, the one text it is shown as, and the checks on both (#4990)."""

import dataclasses
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from teatree.core.modelkit.owner_decision import OwnerDecision, owner_decision

logger = logging.getLogger(__name__)

MAX_WORDS = 120
MAX_QUESTION_CHARS = 160
MAX_SENTENCE_CHARS = 200
MAX_LABEL_CHARS = 40
MAX_CONSEQUENCE_CHARS = 160
MAX_CHECKED_FACTS = 4

REPLY_LINE = "Or reply in this thread."
REPLY_LINE_WITHOUT_OPTIONS = "Reply in this thread."

_REF_LINE = re.compile(r"^ref: question \d+$", re.MULTILINE)
_WORD = re.compile(r"[^\W_]+")
_ABBREVIATION = re.compile(r"\b(?:e\.g|i\.e|etc|vs)\.", re.IGNORECASE)
_SENTENCE_BREAK = re.compile(r"[.!?][\"')\]]*\s+\S")

_LABELLED_LINK = re.compile(r"<https?://[^\s|>]+\|([^>]*)>")
_URL = re.compile(r"<https?://[^\s>]+>|https?://\S+")
_SEGMENT = r"[\w.~-]"
_REFERENCE_WORDS = (
    r"(?:tickets?|issues?|pr|pull request|mr|merge request|tasks?|questions?|rows?|directives?|rules?|todo)"
)

#: Pattern rules, no name lists: each matches a SHAPE an owner cannot read, and a match is blanked before the next rule.
_SHORTHAND_PATTERNS = (
    re.compile(r"`[^`]*`"),
    re.compile(rf"\b{_REFERENCE_WORDS}\s+[#!]?\d+\b", re.IGNORECASE),
    re.compile(r"(?<![\w&])[#!]\d+\b"),
    re.compile(r"(?<![\d,])\d{3,}(?:\s*[,/]\s*\d{3,})+(?!\d)"),
    re.compile(rf"(?<!\w)~/{_SEGMENT}*(?:/{_SEGMENT}+)*"),
    re.compile(rf"(?<![\w/])\./{_SEGMENT}+(?:/{_SEGMENT}+)*"),
    re.compile(rf"\b(?:src|tests)/{_SEGMENT}+(?:/{_SEGMENT}+)*"),
    re.compile(rf"(?<![\w/<>:.~-])/{_SEGMENT}+(?:/{_SEGMENT}+)*"),
    re.compile(rf"(?<![\w/.~-])(?:{_SEGMENT}*[A-Za-z]{_SEGMENT}*/){{2,}}{_SEGMENT}+"),
    re.compile(r"\b[\w-]+\.(?:py|toml|json|ya?ml|md|sh|sqlite3)\b"),
    re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){2,}\b"),
    re.compile(r"\b(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,40}\b"),
    re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"),
    re.compile(r"(?<![\w-])--[A-Za-z][\w-]*"),
    re.compile(r"\b\w+=[^\s,;]+"),
    re.compile(r"\bdeferred\b", re.IGNORECASE),
)


def word_count(text: str) -> int:
    """Runs of letters or digits in *text* as read: a labelled link is its label, a bare URL one word, no footer."""
    read = _URL.sub(" link ", _LABELLED_LINK.sub(r" \1 ", _REF_LINE.sub("", text)))
    return len(_WORD.findall(read))


def _blank_urls(text: str) -> str:
    """*text* with every URL replaced by spaces of the same length, a labelled link keeping its label."""

    def keep_label(match: re.Match[str]) -> str:
        label = match.group(1)
        return " " * (len(match.group(0)) - len(label) - 1) + label + " "

    return _URL.sub(lambda match: " " * len(match.group(0)), _LABELLED_LINK.sub(keep_label, text))


def internal_shorthand(text: str) -> list[str]:
    """The tokens in *text* an owner reads as internal shorthand, in reading order."""
    working = _blank_urls(text)
    found: list[tuple[int, str]] = []
    for pattern in _SHORTHAND_PATTERNS:
        found.extend((match.start(), match.group(0)) for match in pattern.finditer(working))
        working = pattern.sub(lambda match: " " * len(match.group(0)), working)
    return [token for _start, token in sorted(found)]


class OptionDict(TypedDict):
    """An option as stored in ``options_json``: the ``AskUserQuestion`` shape plus the recommended marker."""

    label: str
    description: str
    recommended: NotRequired[bool]


class CardEvidence(TypedDict):
    """What a card stores in a row's ``evidence``."""

    decision: str
    checked: list[str]
    blocker: str
    why: str
    quoted: NotRequired[str]


@dataclass(frozen=True, slots=True)
class CardOption:
    """One answer the owner can pick: its label, what happens when it is picked, and whether it is the advice."""

    label: str
    description: str
    recommended: bool = dataclasses.field(default=False, kw_only=True)

    def as_dict(self) -> OptionDict:
        option = OptionDict(label=self.label, description=self.description)
        if self.recommended:
            option["recommended"] = True
        return option

    @classmethod
    def from_raw(cls, raw: object) -> "CardOption | None":
        if isinstance(raw, str):
            return cls(raw, "")
        if isinstance(raw, Mapping):
            return cls(
                str(raw.get("label", "")), str(raw.get("description", "")), recommended=raw.get("recommended") is True
            )
        return None

    @classmethod
    def parse_all(cls, options_json: str) -> tuple["CardOption", ...]:
        try:
            raw = json.loads(options_json) if options_json else []
        except ValueError:
            return ()
        return tuple(option for item in raw if (option := cls.from_raw(item))) if isinstance(raw, list) else ()


@dataclass(frozen=True, slots=True)
class CardView:
    """What the owner is shown; ``ref`` is the row id the reply and the footer name."""

    question: str
    why: str
    options: tuple[CardOption, ...]
    quoted: str
    ref: int


def option_row(option: CardOption) -> str:
    """The line one option renders as, in the text and (split at the button) in the blocks."""
    row = f"[{option.label}]"
    if option.recommended:
        row += " (recommended)"
    return f"{row} - {option.description}" if option.description else row


def layout(view: CardView, *, closing: str = "") -> list[list[str]]:
    """The blocks of lines a card is shown as; *closing* replaces the options and the reply line once answered."""
    blocks = [[view.question, view.why] if view.why else [view.question]]
    if view.quoted:
        blocks.append([f"> {view.quoted}"])
    ref = f"ref: question {view.ref}"
    if closing:
        return [*blocks, [closing, ref]]
    if view.options:
        blocks.append([option_row(option) for option in view.options])
    return [*blocks, [REPLY_LINE if view.options else REPLY_LINE_WITHOUT_OPTIONS, ref]]


def plain_text(view: CardView, *, closing: str = "") -> str:
    """THE text of a card: record time and send time both measure this string."""
    return "\n\n".join("\n".join(lines) for lines in layout(view, closing=closing))


def _shorthand_problems(where: str, text: str) -> list[str]:
    tokens = internal_shorthand(text)
    return [f"{where} carries internal shorthand ({', '.join(tokens)}); say it in plain words"] if tokens else []


def _option_problems(options: tuple[CardOption, ...]) -> list[str]:
    problems: list[str] = []
    if len(options) not in {0, 2, 3, 4}:
        problems.append(f"{len(options)} option; a card has none, or 2 to 4 options")
    labels = [option.label.strip().casefold() for option in options]
    if "" in labels:
        problems.append("an option has a blank label")
    if len(set(labels)) != len(labels):
        problems.append("two options share a label (duplicate label)")
    for option in options:
        if len(option.label) > MAX_LABEL_CHARS:
            problems.append(f"option label {option.label[:20]!r}... is over {MAX_LABEL_CHARS} characters")
        if len(option.description) > MAX_CONSEQUENCE_CHARS:
            problems.append(f"the consequence of option {option.label!r} is over {MAX_CONSEQUENCE_CHARS} characters")
    marked = [option for option in options if option.recommended]
    if len(marked) > 1:
        problems.append("more than one option is marked recommended")
    elif marked and not options[0].recommended:
        problems.append("the recommended option must come first")
    return problems


def shown_problems(view: CardView) -> list[str]:
    """What is wrong with *view* as the owner will read it: shorthand anywhere, too many words, a bad option set."""
    shown = [("the question", view.question), ("the why sentence", view.why), ("the quoted text", view.quoted)]
    for option in view.options:
        shown += [(f"option {option.label!r} label", option.label), (f"option {option.label!r}", option.description)]
    problems = [problem for where, text in shown for problem in _shorthand_problems(where, text)]
    if (words := word_count(plain_text(view))) > MAX_WORDS:
        problems.append(f"the card is {words} words; at most {MAX_WORDS}")
    return [*problems, *_option_problems(view.options)]


def _sentence_problems(name: str, text: str, *, limit: int, ends_with: str = "") -> list[str]:
    clean = text.strip()
    if not clean:
        return [f"{name} is required"]
    problems = []
    if len(clean) > limit:
        problems.append(f"{name} is {len(clean)} characters; at most {limit}")
    if ends_with and not clean.endswith(ends_with):
        problems.append(f'{name} must end in "{ends_with}"')
    if "\n" in clean or _SENTENCE_BREAK.search(_ABBREVIATION.sub("x", clean)):
        problems.append(f"{name} must be one sentence on one line")
    return problems


@dataclass(frozen=True, slots=True)
class QuestionCard:
    """An owner question: the decision it asks for, what was checked, what blocks, and what the owner sees besides it.

    ``decision``, ``checked`` and ``blocker`` are stored and read by operators; they are never part of the text.
    """

    decision: OwnerDecision
    checked: tuple[str, ...]
    blocker: str
    why: str
    options: tuple[CardOption, ...] = ()
    quoted: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "checked", tuple(self.checked))
        object.__setattr__(self, "options", tuple(self.options))

    def view(self, question: str, *, ref: int = 0) -> CardView:
        return CardView(question.strip(), self.why.strip(), self.options, self.quoted.strip(), ref)

    def problems(self, question: str) -> list[str]:
        """Every reason this card may not be sent as an owner question, all at once."""
        problems = shown_problems(self.view(question))
        problems += _sentence_problems("the question", question, limit=MAX_QUESTION_CHARS, ends_with="?")
        problems += _sentence_problems("the why sentence", self.why, limit=MAX_SENTENCE_CHARS)
        if not 1 <= len(self.checked) <= MAX_CHECKED_FACTS:
            problems.append(f"{len(self.checked)} checked facts; give 1 to {MAX_CHECKED_FACTS}")
        for fact in self.checked:
            problems += _sentence_problems("a checked fact", fact, limit=MAX_SENTENCE_CHARS)
            problems += _shorthand_problems("a checked fact", fact)
        problems += _sentence_problems("the blocker", self.blocker, limit=MAX_SENTENCE_CHARS)
        problems += _shorthand_problems("the blocker", self.blocker)
        if "\n" in self.quoted.strip():
            problems.append("the quoted text must be one line")
        if self.options and not any(option.recommended for option in self.options):
            problems.append("mark exactly one option as recommended, and list it first")
        problems += [f"option {o.label!r} needs a consequence" for o in self.options if not o.description.strip()]
        return problems

    def quote_problems(self, question: str, text: str) -> list[str]:
        """What would be wrong with this card if *text* were quoted on it."""
        return dataclasses.replace(self, quoted=" ".join(text.split())).problems(question)

    def with_quote(self, question: str, text: str) -> "QuestionCard":
        """This card with *text* quoted on one line, or unchanged (and logged) when the quote would break a check."""
        if not text.strip():
            return self
        if problems := self.quote_problems(question, text):
            logger.warning("owner question %r: quoted text left off: %s", question[:60], "; ".join(problems))
            return self
        return dataclasses.replace(self, quoted=" ".join(text.split()))

    def to_evidence(self) -> CardEvidence:
        evidence = CardEvidence(
            decision=self.decision.value, checked=list(self.checked), blocker=self.blocker, why=self.why
        )
        if self.quoted:
            evidence["quoted"] = self.quoted
        return evidence

    def option_dicts(self) -> list[OptionDict]:
        return [option.as_dict() for option in self.options]

    def options_json(self) -> str:
        return json.dumps(self.option_dicts()) if self.options else ""

    @classmethod
    def from_row(cls, evidence: Mapping[str, object], options_json: str) -> "QuestionCard | None":
        """The card a row was recorded with, or ``None`` for a row recorded before cards (no why sentence)."""
        decision = owner_decision(evidence.get("decision"))
        checked = evidence.get("checked")
        if decision is None or "why" not in evidence or not isinstance(checked, list):
            return None
        return cls(
            decision=decision,
            checked=tuple(map(str, checked)),
            blocker=str(evidence.get("blocker", "")),
            why=str(evidence["why"]),
            options=CardOption.parse_all(options_json),
            quoted=str(evidence.get("quoted", "")),
        )

    @classmethod
    def from_envelope(cls, result: Mapping[str, object]) -> "QuestionCard | None":
        """The card an agent's owner-stop envelope carries — ``None`` unless it names an owner decision.

        A missing or mistyped key leaves an empty field, so :meth:`problems` names the gap and the stop is not lost.
        """
        decision = owner_decision(result.get("user_input_kind"))
        if decision is None:
            return None
        checked, card = result.get("user_input_checked"), result.get("user_input_card")
        fields = card if isinstance(card, Mapping) else {}
        options = fields.get("options")
        return cls(
            decision=decision,
            checked=tuple(map(str, checked)) if isinstance(checked, list) else (),
            blocker=str(fields.get("blocker", "")),
            why=str(fields.get("why", "")),
            options=tuple(option for item in options if (option := CardOption.from_raw(item)))
            if isinstance(options, list)
            else (),
        )
