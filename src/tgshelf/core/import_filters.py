"""Pure history-import filters: include unions followed by exclusion vetoes."""
from collections.abc import Sequence
from dataclasses import dataclass
import re

_TOKEN = r"[a-z0-9!#$&^_+.-]+"
_MIME = re.compile(rf"{_TOKEN}/(?:{_TOKEN}|\*)\Z")
_EXT = re.compile(r"\.[a-z0-9_+.-]+\Z")


@dataclass(frozen=True)
class ImportFilters:
    include_ext: frozenset[str] = frozenset()
    exclude_ext: frozenset[str] = frozenset()
    include_mime: frozenset[str] = frozenset()
    exclude_mime: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FilterDecision:
    accepted: bool
    reason: str | None = None


def _normalize(values: Sequence[str], *, mime: bool) -> frozenset[str]:
    normalized = set()
    for group in values:
        for raw in group.split(','):
            value = raw.strip().casefold()
            if not value:
                raise ValueError('Import filter values must be non-empty')
            if mime:
                if '/' not in value:
                    value += '/*'
                valid = _MIME.fullmatch(value)
            else:
                value = '.' + value.removeprefix('.')
                valid = _EXT.fullmatch(value) and not value.endswith('.') and '..' not in value
            if not valid:
                raise ValueError(f'Invalid import {"MIME" if mime else "extension"} filter: {raw!r}')
            normalized.add(value)
    return frozenset(normalized)


def parse_filters(*, include_ext: Sequence[str] = (), exclude_ext: Sequence[str] = (),
                  include_mime: Sequence[str] = (), exclude_mime: Sequence[str] = ()) -> ImportFilters:
    return ImportFilters(_normalize(include_ext, mime=False), _normalize(exclude_ext, mime=False),
                         _normalize(include_mime, mime=True), _normalize(exclude_mime, mime=True))


def evaluate_filters(filters: ImportFilters, *, filename: str | None,
                     mime: str | None) -> FilterDecision:
    name = (filename or '').casefold()
    media = (mime or '').split(';', 1)[0].strip().casefold()
    def extension_matches(patterns):
        return bool(name) and any(name.endswith(p) for p in patterns)
    def mime_matches(patterns):
        return bool(media) and any(media == p or (p.endswith('/*') and media.startswith(p[:-1]))
                                   for p in patterns)
    if extension_matches(filters.exclude_ext):
        return FilterDecision(False, 'excluded_extension')
    if mime_matches(filters.exclude_mime):
        return FilterDecision(False, 'excluded_mime')
    if not (filters.include_ext or filters.include_mime):
        return FilterDecision(True)
    if extension_matches(filters.include_ext) or mime_matches(filters.include_mime):
        return FilterDecision(True)
    return FilterDecision(False, 'include_not_matched')
