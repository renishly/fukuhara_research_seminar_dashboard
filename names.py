"""Keeping students' names away from guests.

Guests never see a student's real name. Instead each student has a fixed
placeholder such as "Gen 9 Student 7" (their order of registration in their
gen; the number is never reused). Admins' names are shown unchanged.

Names can also hide inside text: a class description saying "Thanks to Taro
for the slides", or an agent called "Yamada's bot". So, before any text is
shown to a guest, NameMask.text() searches it for every student's
  - full name ("Yamada Taro", also written without the space),
  - name parts of at least 2 characters ("Yamada", "Taro"),
  - "other names" (romaji, nicknames... from the Account page),
and replaces each match with that student's placeholder. Matching ignores
upper/lower case and full-width/half-width differences ("ＴＡＲＯ" = "taro").
Longer matches win, so a full name becomes one placeholder, not two.

This happens only when a page is built for a guest. The stored text never
changes, members always see the original, and renamed or newly registered
students are covered straight away. (Files can't be searched: they're shown
to guests exactly as uploaded.)
"""

import re
import unicodedata
from bisect import bisect_left, bisect_right

from sqlmodel import Session, select

from db import Gen, User
from security import Viewer

MIN_PART_LENGTH = 2  # shorter name parts ("A", "李") would match far too much
MAX_OTHER_NAMES = 10
LATIN_ONLY = re.compile(r"[a-z0-9\s]+")


def placeholder(student: User, gen_number: int | None) -> str:
    """e.g. "Gen 9 Student 7"."""
    gen = f"Gen {gen_number} " if gen_number is not None else ""
    number = student.guest_number if student.guest_number is not None else "?"
    return f"{gen}Student {number}"


def normalize(text: str) -> str:
    """Fold case and full-width/half-width differences: "ＴＡＲＯ" -> "taro"."""
    return unicodedata.normalize("NFKC", text).casefold()


def split_other_names(raw: str | None) -> list[str]:
    """The "other names" field: one name per line, blank lines ignored, at most 10."""
    names = [" ".join(line.split()) for line in (raw or "").splitlines()]
    return [name for name in names if name][:MAX_OTHER_NAMES]


def name_forms(student: User) -> list[str]:
    """Every way of writing this student's name that the search looks for."""
    full = " ".join(student.full_name.split())
    parts = [part for part in full.split(" ") if len(part) >= MIN_PART_LENGTH]
    return [full, *parts, *split_other_names(student.other_names)]


def _pattern(form: str) -> str:
    """A regular expression for one name form.

    Spaces in a name may be any amount of space, or none ("山田 太郎" also
    matches "山田太郎"). Names written only in Latin letters must stand
    alone, so "Ann" doesn't match inside "Annual"; other scripts (like
    Japanese, written without spaces) match anywhere.
    """
    tokens = normalize(form).split()
    pattern = r"\s*".join(re.escape(token) for token in tokens)
    if LATIN_ONLY.fullmatch(normalize(form)):
        pattern = rf"(?<![a-z0-9]){pattern}(?![a-z0-9])"
    return pattern


class NameMask:
    """Built once per page. For guests it hides names; for members it does nothing."""

    def __init__(self, db: Session, viewer: Viewer):
        self.active = viewer.is_guest
        self.placeholders: dict[int, str] = {}
        self._lookup: dict[str, str] = {}  # a name form without spaces -> placeholder
        self._regex = None
        if not self.active:
            return
        gens = {gen.id: gen.number for gen in db.exec(select(Gen)).all()}
        students = db.exec(select(User).where(User.role == "student").order_by(User.id)).all()
        forms: list[str] = []
        for student in students:
            label = placeholder(student, gens.get(student.gen_id))
            self.placeholders[student.id] = label
            for form in name_forms(student):
                key = "".join(normalize(form).split())
                if key and key not in self._lookup:
                    self._lookup[key] = label
                    forms.append(form)
        if forms:
            # Longest first: at any position the regex takes the first
            # alternative that matches, so a full name beats its parts.
            forms.sort(key=lambda form: len(normalize(form)), reverse=True)
            self._regex = re.compile("|".join(_pattern(form) for form in forms))

    def name(self, user: User | None) -> str:
        """The name to show for a person: a placeholder for students when a guest is looking."""
        if user is None:
            return "Deleted account"
        if self.active and user.role == "student":
            return self.placeholders.get(user.id) or "A student"
        return user.full_name

    def text(self, value) -> str:
        """Some text as this viewer may see it (names replaced for guests)."""
        if value is None:
            return ""
        value = str(value)
        if not self.active or self._regex is None or not value:
            return value
        # Search a normalised copy, remembering where each original character
        # starts in it, so matches can be cut out of the original text.
        starts, pieces, offset = [], [], 0
        for char in value:
            starts.append(offset)
            folded = normalize(char)
            pieces.append(folded)
            offset += len(folded)
        searchable = "".join(pieces)

        result, kept_up_to = [], 0
        for match in self._regex.finditer(searchable):
            if match.end() == match.start():
                continue
            first = bisect_right(starts, match.start()) - 1  # the character the match starts in
            after = bisect_left(starts, match.end())  # the first character after it
            if first < kept_up_to:
                continue
            key = "".join(match.group().split())
            result.append(value[kept_up_to:first])
            result.append(self._lookup.get(key, "A student"))
            kept_up_to = after
        result.append(value[kept_up_to:])
        return "".join(result)


def assign_guest_number(db: Session, student: User) -> None:
    """Give a student the next placeholder number in their gen. The caller commits.

    Each gen keeps a counter that only goes up, so numbers are never reused,
    even after an account is deleted.
    """
    gen = db.get(Gen, student.gen_id) if student.gen_id else None
    if gen is None:
        student.guest_number = None
        return
    student.guest_number = gen.next_guest_number
    gen.next_guest_number += 1
    db.add(gen)
