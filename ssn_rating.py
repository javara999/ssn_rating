# -*- coding: utf-8 -*-
import html
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from plugins.metadata.base import BaseMetadataProvider


PLUGIN_VERSION = "1.0.5"
BASE_URL = "https://ssn.so"
USER_AGENT = "BookOasis-SsnRatingPlugin/1.0"
BLOCK_START = "<!-- BOOKOASIS_SSN_RATING_START -->"
BLOCK_END = "<!-- BOOKOASIS_SSN_RATING_END -->"


class _SearchResultsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.current = None
        self.in_h3 = False
        self.capture = None

    @staticmethod
    def _attrs(attrs):
        return dict(attrs)

    def handle_starttag(self, tag, attrs):
        attrs = self._attrs(attrs)
        classes = set((attrs.get("class") or "").split())

        if tag == "div" and "product" in classes:
            self._finish_current()
            self.current = {"title": "", "author": "", "cover": "", "rating": "", "reviews": ""}
            return

        if self.current is None:
            return

        if tag == "div":
            if "product-reviews" in classes:
                self.capture = "reviews"
        elif tag == "h3":
            self.in_h3 = True
        elif tag == "a":
            href = attrs.get("href") or ""
            if re.fullmatch(r"/series/(\d+)/", href):
                self.current["id"] = re.search(r"\d+", href).group(0)
                self.current["link"] = urllib.parse.urljoin(BASE_URL, href)
                if self.in_h3:
                    self.capture = "title"
            elif "/profile/author/" in href and not self.current["author"]:
                self.capture = "author"
        elif tag == "img" and not self.current["cover"]:
            self.current["cover"] = attrs.get("data-orig") or attrs.get("src") or ""
            self.current["image_title"] = attrs.get("alt") or ""
        elif tag == "span" and "rateit" in classes:
            self.current["rating"] = attrs.get("data-rateit-value") or ""

    def handle_endtag(self, tag):
        if self.current is None:
            return
        if tag == "a" and self.capture in ("title", "author"):
            self.capture = None
        elif tag == "h3":
            self.in_h3 = False
        elif tag == "div":
            if self.capture == "reviews":
                self.capture = None

    def handle_data(self, data):
        if self.current is not None and self.capture:
            self.current[self.capture] += data

    def close(self):
        super().close()
        self._finish_current()

    def _finish_current(self):
        if self.current is None:
            return
        if not self.current["title"]:
            self.current["title"] = self.current.get("image_title", "")
        if self.current.get("id") and self.current["title"]:
            self.items.append(self.current)
        self.current = None


class _JsonLdParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_json_ld = False
        self.parts = []
        self.documents = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and (attrs.get("type") or "").lower() == "application/ld+json":
            self.in_json_ld = True
            self.parts = []

    def handle_endtag(self, tag):
        if tag == "script" and self.in_json_ld:
            self.in_json_ld = False
            raw = "".join(self.parts).strip()
            try:
                self.documents.append(json.loads(raw))
            except (TypeError, ValueError):
                pass

    def handle_data(self, data):
        if self.in_json_ld:
            self.parts.append(data)


class SsnRatingMetadataProvider(BaseMetadataProvider):
    id = "ssn_rating"
    name = "소설넷 평점"
    version = PLUGIN_VERSION
    is_searchable = True
    update_manifest = {
        "enabled": True,
        "provider": "github-raw",
        "raw_base_url": "https://raw.githubusercontent.com/javara999/ssn_rating/main",
        "files": ["ssn_rating.py", "__init__.py", "VERSION", "settings.html", "settings.css", "README.md"],
        "version_file": "VERSION",
        "version_key": "plugin version",
        "show_sample_update_button": True,
    }
    config_schema = [
        {
            "key": "MAX_RESULTS",
            "label": "최대 검색 결과",
            "type": "number",
            "default": 10,
            "required": False,
            "description": "검색 결과에 표시할 최대 작품 수입니다.",
        },
        {
            "key": "REVIEW_MAX_LENGTH",
            "label": "리뷰 최대 글자 수",
            "type": "number",
            "default": 1000,
            "required": False,
            "description": "최고 평점 리뷰를 저장할 최대 글자 수입니다.",
        },
        {
            "key": "PRESERVE_SUMMARY",
            "label": "기존 작품 설명 보존",
            "type": "checkbox",
            "default": True,
            "required": False,
            "description": "기존 설명 뒤에 소설넷 평점 블록을 추가합니다.",
        },
        {
            "key": "TIMEOUT",
            "label": "요청 제한 시간(초)",
            "type": "number",
            "default": 15,
            "required": False,
        },
        {
            "key": "CACHE_TTL",
            "label": "캐시 유지 시간(초)",
            "type": "number",
            "default": 600,
            "required": False,
        },
    ]

    _cache = {}
    _request_lock = threading.Lock()
    _last_request_at = 0.0
    _minimum_request_interval = 10.0

    def search(self, db_type, query):
        query = self._clean_text(query)
        if not query:
            return []

        cfg = self._config(db_type)
        url = f"{BASE_URL}/series/?" + urllib.parse.urlencode({"keyword": query})
        try:
            page = self._request_text(url, cfg)
            parser = _SearchResultsParser()
            parser.feed(page)
            parser.close()
        except Exception as exc:
            print(f"[SsnRatingMetadataProvider] search failed: {exc}")
            return []

        max_results = self._int(cfg.get("MAX_RESULTS"), 10, 1, 50)
        results = []
        seen = set()
        for item in parser.items:
            series_id = item.get("id")
            if series_id in seen:
                continue
            seen.add(series_id)
            rating = self._float(item.get("rating"), 0.0)
            count = self._review_count(item.get("reviews"))
            rating_text = f"{rating:g} / 5.0" if rating else "평점 없음"
            count_text = f" ({count:,}명)" if count else ""
            results.append(
                {
                    "title": self._clean_text(item.get("title")),
                    "author": self._clean_text(item.get("author")),
                    "publisher": "소설넷",
                    "pubDate": "",
                    "cover": item.get("cover") or "",
                    "description": f"소설넷 평점 {rating_text}{count_text} · 적용 시 최고 평점 리뷰를 함께 저장합니다.",
                    "link": item.get("link") or f"{BASE_URL}/series/{series_id}/",
                    "score": self._score_100(rating),
                    "ssn_id": series_id,
                    "ssn_rating": rating,
                    "ssn_rating_count": count,
                }
            )
            if len(results) >= max_results:
                break
        return results

    def apply(self, db_type, book_id, item_data):
        series_id = str((item_data or {}).get("ssn_id") or "").strip()
        if not series_id.isdigit():
            return False, "유효한 소설넷 작품 ID가 없습니다. 다시 검색해 주세요."

        gateway = self.get_db_gateway(db_type)
        book = gateway.fetch_one(
            "SELECT id, summary FROM books WHERE id = ? AND COALESCE(is_deleted, 0) = 0",
            (book_id,),
        )
        if not book:
            return False, "대상 도서를 찾을 수 없습니다."

        cfg = self._config(db_type)
        try:
            highest = self._fetch_detail(series_id, "-rating", cfg)
        except Exception as exc:
            print(f"[SsnRatingMetadataProvider] detail fetch failed: {exc}")
            return False, f"소설넷 상세정보 조회 실패: {exc}"

        aggregate = highest.get("aggregate") or {}
        rating = self._float(aggregate.get("ratingValue"), self._float(item_data.get("ssn_rating"), 0.0))
        rating_count = self._int_value(aggregate.get("ratingCount"), self._int_value(item_data.get("ssn_rating_count"), 0))
        if rating <= 0:
            return False, "소설넷 평균 평점을 확인할 수 없습니다."

        review_limit = self._int(cfg.get("REVIEW_MAX_LENGTH"), 1000, 100, 5000)
        block = self._build_summary_block(
            series_id,
            rating,
            rating_count,
            highest.get("review"),
            review_limit,
        )
        old_summary = self._remove_existing_block(book["summary"] or "")
        preserve = self._truthy(cfg.get("PRESERVE_SUMMARY", True))
        summary = f"{old_summary.rstrip()}\n\n{block}" if preserve and old_summary.strip() else block
        score = self._score_100(rating)

        try:
            count = gateway.execute(
                """
                UPDATE books
                SET score = ?, summary = ?, metadata_locked = 1
                WHERE id = ? AND COALESCE(is_deleted, 0) = 0
                """,
                (score, summary, book_id),
            )
        except Exception as exc:
            return False, f"DB 업데이트 오류: {exc}"
        if count != 1:
            return False, "평점 적용 중 대상 도서가 변경되었습니다."
        return True, f"소설넷 평점 {rating:g}/5.0과 최고 평점 리뷰를 적용했습니다."

    def _fetch_detail(self, series_id, order, cfg):
        query = urllib.parse.urlencode({"filter": "rating", "order_by": order})
        url = f"{BASE_URL}/series/{series_id}/?{query}"
        page = self._request_text(url, cfg)
        parser = _JsonLdParser()
        parser.feed(page)
        book = self._find_book_json_ld(parser.documents)
        if not book:
            raise RuntimeError("작품 구조화 데이터를 찾을 수 없습니다.")
        reviews = book.get("review") or []
        if isinstance(reviews, dict):
            reviews = [reviews]
        review = next((value for value in reviews if self._clean_text(value.get("reviewBody"))), None)
        return {"aggregate": book.get("aggregateRating") or {}, "review": review}

    @classmethod
    def _request_text(cls, url, cfg):
        ttl = cls._int(cfg.get("CACHE_TTL"), 600, 60, 86400)
        now = time.time()
        with cls._request_lock:
            cached = cls._cache.get(url)
            if cached and now - cached[0] < ttl:
                return cached[1]

            wait = cls._minimum_request_interval - (now - cls._last_request_at)
            if wait > 0:
                time.sleep(wait)
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ko-KR,ko;q=0.9",
                },
            )
            timeout = cls._int(cfg.get("TIMEOUT"), 15, 5, 60)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    text = response.read().decode(charset, errors="replace")
            finally:
                cls._last_request_at = time.time()
            cls._cache[url] = (time.time(), text)
            return text

    @staticmethod
    def _find_book_json_ld(documents):
        def visit(value):
            if isinstance(value, dict):
                kind = value.get("@type")
                if kind == "Book" or isinstance(kind, list) and "Book" in kind:
                    return value
                graph = value.get("@graph")
                if graph:
                    found = visit(graph)
                    if found:
                        return found
                for child in value.values():
                    found = visit(child)
                    if found:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = visit(child)
                    if found:
                        return found
            return None

        return visit(documents)

    @classmethod
    def _build_summary_block(cls, series_id, rating, rating_count, highest, limit):
        lines = [BLOCK_START, "[소설넷 평점]"]
        count_text = f" ({rating_count:,}명)" if rating_count else ""
        lines.append(f"평균: {rating:g} / 5.0{count_text}")
        if highest:
            lines.extend(["", cls._review_text("최고 평점 리뷰", highest, limit)])
        lines.append(BLOCK_END)
        return "\n".join(lines)

    @classmethod
    def _review_text(cls, label, review, limit):
        if not review:
            return ""
        rating_data = review.get("reviewRating") or {}
        rating = cls._float(rating_data.get("ratingValue"), 0.0)
        author_data = review.get("author") or {}
        author = author_data.get("name") if isinstance(author_data, dict) else author_data
        author = html.escape(cls._clean_text(author) or "익명", quote=False)
        body = cls._clean_text(review.get("reviewBody"), preserve_newlines=True)
        if len(body) > limit:
            body = body[:limit].rstrip() + "..."
        body = html.escape(body, quote=False)
        return f"[{label} {rating:g}] {author}\n{body}"

    @staticmethod
    def _remove_existing_block(summary):
        pattern = re.compile(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END), re.DOTALL)
        return pattern.sub("", str(summary or "")).strip()

    def _config(self, db_type):
        values = {item["key"]: item.get("default") for item in self.config_schema}
        stored = self.get_plugin_config(db_type, default={})
        if isinstance(stored, dict):
            values.update(stored)
        return values

    @staticmethod
    def _clean_text(value, preserve_newlines=False):
        text = html.unescape(str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
        if preserve_newlines:
            return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")).strip()
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _review_count(value):
        match = re.search(r"\(([\d,]+)\)", str(value or ""))
        return int(match.group(1).replace(",", "")) if match else 0

    @staticmethod
    def _score_100(rating):
        return max(0, min(100, int(round(float(rating or 0) * 20))))

    @staticmethod
    def _float(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _int_value(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _int(value, default, minimum, maximum):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _truthy(value):
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in ("1", "true", "yes", "on")
