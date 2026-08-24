import re
import time

from .base import Plugin
from core.types import ChapterInfo
import config


class ChaptersPlugin(Plugin):
    """Plugin for fetching book chapters and their content."""

    def fetch_list(self, book_id: str) -> list[ChapterInfo]:
        """Fetch list of chapters for a book."""
        url = f"{config.API_V2}/epub-chapters/?epub_identifier=urn:orm:book:{book_id}"
        chapters: list[ChapterInfo] = []

        while url:
            data = self.http.get_json(url)
            for ch in data.get("results", []):
                chapters.append(ChapterInfo(
                    ourn=ch.get("ourn", ""),
                    title=ch.get("title", ""),
                    filename=self._extract_filename(ch.get("reference_id", "")),
                    content_url=ch.get("content_url", ""),
                    images=(ch.get("related_assets") or {}).get("images", []),
                    stylesheets=(ch.get("related_assets") or {}).get("stylesheets", []),
                    virtual_pages=ch.get("virtual_pages"),
                    minutes_required=ch.get("minutes_required"),
                ))
            url = data.get("next")

        return chapters

    def fetch_toc(self, book_id: str) -> list[dict]:
        url = f"{config.API_V2}/epubs/urn:orm:book:{book_id}/table-of-contents/"
        data = self.http.get_json(url)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("results", [])
        return []

    _FRONT_MATTER_KEYWORDS = ("cover", "halftitle", "titlepage", "title-page", "contents", "toc")

    def reorder_by_toc(self, chapters: list[ChapterInfo], toc: list[dict]) -> list[ChapterInfo]:
        """Reorder chapters to match the TOC reading order."""
        ordered_filenames = self._flatten_toc_filenames(toc)
        if not ordered_filenames:
            return chapters

        chapter_map = {ch["filename"]: ch for ch in chapters}

        ordered = []
        for filename in ordered_filenames:
            if filename in chapter_map:
                ordered.append(chapter_map.pop(filename))

        # Split remaining into front matter (before TOC content) and back matter (after)
        front = []
        back = []
        for ch in chapters:
            if ch["filename"] not in chapter_map:
                continue
            if self._is_front_matter(ch):
                front.append(ch)
            else:
                back.append(ch)

        return front + ordered + back

    def _is_front_matter(self, ch: ChapterInfo) -> bool:
        filename_lower = ch["filename"].lower()
        title_lower = ch["title"].lower()
        for keyword in self._FRONT_MATTER_KEYWORDS:
            if keyword in filename_lower or keyword in title_lower:
                return True
        return False

    # A chapter served without a valid session comes back as HTTP 200 with a
    # short "abstract" preview whose text is cut off with an ellipsis, instead
    # of a 403. Silently accepting it produces a book with only the first
    # paragraph of each chapter, so detect it and retry/fail loudly.
    _PREVIEW_MAX_BYTES = 6000
    _PREVIEW_RETRIES = 3

    def fetch_content(self, content_url: str) -> str:
        """Fetch chapter HTML, retrying if the server returns a preview stub."""
        last = ""
        for attempt in range(self._PREVIEW_RETRIES):
            last = self.http.get_text(content_url)
            if not self._looks_truncated(last):
                return last
            if attempt < self._PREVIEW_RETRIES - 1:
                # The session may have gone stale mid-download; reload cookies
                # from disk (the user may have refreshed them) and back off.
                reload_cookies = getattr(self.http, "reload_cookies", None)
                if reload_cookies:
                    reload_cookies()
                time.sleep(config.RETRY_BACKOFF * (attempt + 1))

        raise RuntimeError(
            f"O'Reilly returned only a preview stub for {content_url} "
            f"({len(last)} bytes). Your session most likely expired mid-download "
            "(long translations can take hours). Copy fresh cookies from your "
            "browser, POST them to /api/cookies, and download again."
        )

    def _looks_truncated(self, html: str) -> bool:
        """True if the HTML looks like O'Reilly's cut-off abstract preview."""
        if len(html) > self._PREVIEW_MAX_BYTES:
            return False
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        # Preview bodies end mid-sentence with "..." or the ellipsis character.
        return text.endswith("...") or text.endswith("…")

    def _extract_filename(self, reference_id: str) -> str:
        if "-/" in reference_id:
            return reference_id.split("-/")[1]
        return reference_id

    def _flatten_toc_filenames(self, toc_items: list[dict]) -> list[str]:
        """Extract ordered, deduplicated filenames from the TOC tree."""
        result: list[str] = []
        seen: set[str] = set()

        def walk(items: list[dict]):
            for item in items:
                ref_id = item.get("reference_id", "")
                if ref_id:
                    filename = self._extract_filename(ref_id)
                    if filename and filename not in seen:
                        result.append(filename)
                        seen.add(filename)
                children = item.get("children", [])
                if children:
                    walk(children)

        walk(toc_items)
        return result
