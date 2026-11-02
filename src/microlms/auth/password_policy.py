"""Password strength rules for accounts that log in with a password.

A :class:`PasswordPolicy` is passed to the backend that sets passwords
(``JwtAuthBackend``), which refuses any new password breaking one of its
rules. This module is pure and free of the optional hashing dependencies.
"""

import enum
from dataclasses import dataclass

__all__ = [
    "PasswordPolicy",
    "PasswordRule",
    "unmet_password_rules",
]


class PasswordRule(enum.StrEnum):
    """A rule a password can break; the values are part of the HTTP contract,
    so API clients can map each one to their own message."""

    MIN_LENGTH = "min_length"
    UPPERCASE = "uppercase"
    LOWERCASE = "lowercase"
    DIGIT = "digit"
    SPECIAL = "special"


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """The rules every new password must meet."""

    min_length: int
    require_uppercase: bool
    require_lowercase: bool
    require_digit: bool
    require_special: bool


def unmet_password_rules(policy: PasswordPolicy, password: str) -> list[PasswordRule]:
    """Return the rules ``password`` breaks, in declaration order (empty = accepted).

    Character classes are Unicode-aware: an accented capital counts as an
    uppercase letter, and a special character is anything that is neither a
    letter nor a digit (spaces included).
    """
    checks = [
        (PasswordRule.MIN_LENGTH, len(password) >= policy.min_length),
        (
            PasswordRule.UPPERCASE,
            not policy.require_uppercase or any(char.isupper() for char in password),
        ),
        (
            PasswordRule.LOWERCASE,
            not policy.require_lowercase or any(char.islower() for char in password),
        ),
        (
            PasswordRule.DIGIT,
            not policy.require_digit or any(char.isdigit() for char in password),
        ),
        (
            PasswordRule.SPECIAL,
            not policy.require_special or any(not char.isalnum() for char in password),
        ),
    ]
    return [rule for rule, is_met in checks if not is_met]
