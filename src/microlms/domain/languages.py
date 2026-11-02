"""Language tags shared by the content that exists in several languages:
activity variants and course and activity translations."""

import re

__all__: list[str] = []

# BCP 47-shaped language tags: a 2-3 letter primary subtag plus optional
# alphanumeric subtags ("en", "fr-BE", "es-419")
_LANG_PATTERN = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


class InvalidLanguageTagError(Exception):
    """Raised when a language is not a valid BCP 47-shaped tag."""


def ensure_language_tag(lang: str) -> None:
    """Raise InvalidLanguageTagError unless ``lang`` is a BCP 47-shaped tag."""
    if _LANG_PATTERN.match(lang) is None:
        raise InvalidLanguageTagError(
            f"Invalid language tag {lang!r}: expected a BCP 47 tag such as 'en' or 'fr-BE'"
        )
