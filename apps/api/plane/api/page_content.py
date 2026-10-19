# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Public Page read modes, preserving source slices rather than reserializing HTML."""

from dataclasses import dataclass
from html.parser import HTMLParser
import re

from drf_spectacular.utils import OpenApiParameter
from rest_framework.exceptions import ValidationError

from plane.utils.html_processor import strip_tags

CONTENT_PARAMETERS = [
    OpenApiParameter("with_content", bool, description="Explicit whole-body opt-in; attachments default to metadata."),
    OpenApiParameter("sections", bool, description="Return the document outline without body content."),
    OpenApiParameter("section", str, many=True, description="Heading data-id or preamble; repeat at most 10 times."),
    OpenApiParameter("body_format", str, enum=["html", "text"]),
    OpenApiParameter("limit_chars", int, description="Body character cap, 1 to 100000, per returned body."),
]
CONTENT_NAMES = {parameter.name for parameter in CONTENT_PARAMETERS}


@dataclass(frozen=True)
class PageContentOptions:
    mode: str = "metadata"
    section_ids: tuple[str, ...] = ()
    body_format: str = "html"
    limit_chars: int | None = None

    @property
    def legacy(self):
        return self.mode == "full" and self.body_format == "html" and self.limit_chars is None

    @classmethod
    def from_query(cls, query, *, default_full=False):
        if not CONTENT_NAMES.intersection(query):
            return cls(mode="full" if default_full else "metadata")
        for name in CONTENT_NAMES - {"section"}:
            if len(query.getlist(name)) > 1:
                raise ValidationError({name: "Supply this parameter only once."})
        for name in ("with_content", "sections"):
            if name in query and query[name] not in ("true", "false"):
                raise ValidationError({name: "Expected true or false."})
        body_format = query.get("body_format", "html")
        if body_format not in ("html", "text"):
            raise ValidationError({"body_format": "Expected html or text."})
        limit = query.get("limit_chars")
        if limit is not None:
            try:
                if not re.fullmatch(r"[0-9]+", limit) or not 1 <= int(limit) <= 100000:
                    raise ValueError()
                limit = int(limit)
            except ValueError:
                raise ValidationError({"limit_chars": "Expected an integer from 1 to 100000."})
        selected = query.getlist("section")
        if "section" in query and (not selected or any(not value for value in selected)):
            raise ValidationError({"section": "Section ids must not be empty."})
        if len(selected) > 10:
            raise ValidationError({"section": "Request at most 10 section ids."})
        if query.get("with_content") == "true":
            for name in ("sections", "section"):
                if name in query:
                    raise ValidationError({name: "Cannot combine with with_content=true."})
            mode = "full"
        elif query.get("sections") == "true":
            for name in ("section", "body_format", "limit_chars"):
                if name in query:
                    raise ValidationError({name: "Cannot combine with sections=true."})
            mode = "outline"
        elif selected:
            mode = "section"
        else:
            for name in ("body_format", "limit_chars"):
                if name in query:
                    raise ValidationError({name: "Requires with_content=true or section."})
            mode = "metadata"
        return cls(mode, tuple(dict.fromkeys(selected)), body_format, limit)


def defer_page_bodies(queryset, options, prefix=""):
    fields = ["description_binary"]
    if not options.legacy:
        fields.append("description_json")
    if options.mode == "metadata":
        fields += ["description_html", "description_stripped"]
    elif options.mode in ("outline", "section"):
        fields.append("description_stripped")
    return queryset.defer(*(prefix + name for name in fields))


class HeadingPositions(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.lines = [0] + [match.end() for match in re.finditer("\n", source)]
        self.headings = []
        self.current = None
        self.feed(source)
        self.close()

    def source_offset(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag not in ("h1", "h2", "h3", "h4", "h5", "h6"):
            return
        start = self.source_offset()
        heading = {
            "id": dict(attrs).get("data-id") or f"s{len(self.headings) + 1}",
            "level": int(tag[1]),
            "start": start,
            "title_start": start + len(self.get_starttag_text()),
            "title": "",
        }
        self.headings.append(heading)
        self.current = heading

    def handle_endtag(self, tag):
        if self.current is not None and tag == f"h{self.current['level']}":
            self.current["title"] = strip_tags(self.source[self.current["title_start"] : self.source_offset()])
            self.current = None


def document_sections(source):
    headings = HeadingPositions(source).headings
    result = []
    if not headings or headings[0]["start"] > 0:
        end = headings[0]["start"] if headings else len(source)
        result.append({"id": "preamble", "level": 0, "title": "", "body": source[:end]})
    for index, heading in enumerate(headings):
        end = next(
            (later["start"] for later in headings[index + 1 :] if later["level"] <= heading["level"]),
            len(source),
        )
        result.append(
            {key: heading[key] for key in ("id", "level", "title")} | {"body": source[heading["start"] : end]}
        )
    return result


def body_payload(source, options, *, text=None):
    text = strip_tags(source) if text is None else text
    body = source if options.body_format == "html" else text
    total = len(body)
    # Python slices Unicode code points, so truncation cannot split a UTF-8 character.
    body = body[: options.limit_chars] if options.limit_chars is not None else body
    return {
        "body": body,
        "body_format": options.body_format,
        "text_chars": len(text),
        "total_chars": total,
        "truncated": len(body) < total,
    }


def render_page_content(page, options):
    source = page.description_html or ""
    if options.mode == "full":
        return body_payload(source, options, text=page.description_stripped)
    entries = document_sections(source)
    if options.mode == "outline":
        outline = [
            {key: entry[key] for key in ("id", "level", "title")} | {"text_chars": len(strip_tags(entry["body"]))}
            for entry in entries
        ]
        return {"outline": outline, "section_count": len(outline), "total_chars": len(strip_tags(source))}
    by_id = {entry["id"]: entry for entry in entries}
    unknown = [key for key in options.section_ids if key not in by_id]
    if unknown:
        raise ValidationError({"section": f"Unknown section ids: {', '.join(unknown)}"})
    return {
        "sections": [
            {key: by_id[section_id][key] for key in ("id", "level", "title")}
            | body_payload(by_id[section_id]["body"], options)
            for section_id in options.section_ids
        ]
    }
