import argparse
import asyncio
import json
import re
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import Error, async_playwright

DOTABUFF_BASE = "https://www.dotabuff.com"
DEFAULT_TIMEOUT_MS = 45_000
DEFAULT_WAIT_AFTER_LOAD_MS = 3_000
DEFAULT_SETTLE_TIMEOUT_MS = 25_000
DEFAULT_PROFILE_DIR = Path(".cache/dotabuff-playwright-profile")
DEFAULT_OUTPUT_DIR = Path(".cache/dotabuff-playwright-output")
STEAM_ID_OFFSET = 76561197960265728


class DotabuffBrowserClient:
    """Browser-backed Dotabuff extractor POC with persisted Chromium profile."""

    def __init__(
        self,
        profile_dir: Path = DEFAULT_PROFILE_DIR,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        headless: bool = True,
        cookies_json: Optional[Path] = None,
        ws_endpoint: Optional[str] = None,
    ):
        self._profile_dir = Path(profile_dir)
        self._timeout_ms = int(timeout_ms)
        self._headless = headless
        self._cookies_json = Path(cookies_json) if cookies_json else None
        self._ws_endpoint = (ws_endpoint or "").strip() or None
        self._playwright = None
        self._browser = None
        self._context = None
        self._owns_context = False

    async def __aenter__(self) -> "DotabuffBrowserClient":
        try:
            self._playwright = await async_playwright().start()
            if self._ws_endpoint:
                self._browser = await self._playwright.chromium.connect(self._ws_endpoint)
                self._context = await self._browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/136.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1440, "height": 1200},
                    locale="en-US",
                    timezone_id="UTC",
                )
                self._owns_context = True
            else:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self._profile_dir),
                    headless=self._headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/136.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1440, "height": 1200},
                    locale="en-US",
                    timezone_id="UTC",
                )
                self._owns_context = True
            await self._context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'language', { get: () => 'en-US' });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                window.chrome = window.chrome || { runtime: {} };
                """
            )
            await self._maybe_load_cookies()
            return self
        except BaseException:
            await asyncio.shield(self._close_browser())
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await asyncio.shield(self._close_browser())

    async def _close_browser(self) -> None:
        browser = self._browser
        context = self._context
        playwright = self._playwright
        owns_context = self._owns_context
        self._browser = None
        self._context = None
        self._playwright = None
        self._owns_context = False

        if context is not None and owns_context:
            with suppress(Exception):
                await context.close()

        if browser is not None:
            with suppress(Exception):
                await browser.close()

        if playwright is not None:
            with suppress(Exception):
                await playwright.stop()

    async def _maybe_load_cookies(self) -> None:
        if not self._cookies_json:
            return
        payload = json.loads(self._cookies_json.read_text(encoding="utf-8"))
        cookies = payload.get("cookies") if isinstance(payload, dict) else payload
        if not isinstance(cookies, list):
            raise ValueError("cookies_json must contain a cookie list or {\"cookies\": [...]} payload")
        if cookies:
            await self._context.add_cookies(cookies)

    async def fetch_player_matches(
        self,
        player_id: str,
        *,
        limit: int = 5,
        output_dir: Path = DEFAULT_OUTPUT_DIR,
        wait_after_load_ms: int = DEFAULT_WAIT_AFTER_LOAD_MS,
        settle_timeout_ms: int = DEFAULT_SETTLE_TIMEOUT_MS,
    ) -> Dict[str, Any]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        page = await self._context.new_page()
        page.set_default_timeout(self._timeout_ms)

        captured_responses: List[Dict[str, Any]] = []

        async def on_response(response) -> None:
            url = response.url
            if "dotabuff.com" not in url:
                return
            if not any(token in url for token in ("/matches", "/players/", ".json", "graphql", "api")):
                return
            try:
                body_preview = await response.text()
            except Error:
                body_preview = ""
            captured_responses.append(
                {
                    "url": url,
                    "status": response.status,
                    "content_type": response.headers.get("content-type", ""),
                    "body_preview": body_preview[:1500],
                }
            )

        page.on("response", on_response)

        overview_url = f"{DOTABUFF_BASE}/players/{player_id}"
        matches_url = f"{DOTABUFF_BASE}/players/{player_id}/matches"
        visited = []
        best_snapshot: Optional[Dict[str, Any]] = None

        try:
            for index, url in enumerate((overview_url, matches_url)):
                response = await page.goto(url, wait_until="domcontentloaded")
                status = response.status if response else None
                visited.append({"requested_url": url, "final_url": page.url, "status": status})
                await page.wait_for_timeout(wait_after_load_ms)
                await self._wait_for_page_settle(page, settle_timeout_ms)

                interim_title = await page.title()
                interim_html = await page.content()
                interim_rows = await self._extract_match_rows(page, limit=limit)
                interim_status = self._classify_page(
                    title=interim_title,
                    page_url=page.url,
                    html=interim_html,
                    rows=interim_rows,
                )
                if interim_rows:
                    best_snapshot = {
                        "final_url": page.url,
                        "title": interim_title,
                        "status": interim_status,
                        "rows": interim_rows,
                        "html": interim_html,
                    }

                if await self._is_login_gate(page):
                    break
                if index == 1 and await self._looks_like_matches_page(page):
                    break

            title = await page.title()
            html = await page.content()
            visible_text = await self._extract_visible_text(page)
            rows = await self._extract_match_rows(page, limit=limit)
            status = self._classify_page(title=title, page_url=page.url, html=html, rows=rows)

            if best_snapshot and not rows:
                title = best_snapshot["title"]
                html = best_snapshot["html"]
                rows = best_snapshot["rows"]
                status = best_snapshot["status"]
                final_url = best_snapshot["final_url"]
            else:
                final_url = page.url

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            base_name = f"dotabuff_{player_id}_{timestamp}"
            screenshot_path = output_dir / f"{base_name}.png"
            html_path = output_dir / f"{base_name}.html"
            json_path = output_dir / f"{base_name}.json"

            await page.screenshot(path=str(screenshot_path), full_page=True)
            html_path.write_text(html, encoding="utf-8")

            result = {
                "player_id": player_id,
                "visited": visited,
                "final_url": final_url,
                "title": title,
                "status": status,
                "rows": rows,
                "visible_text_preview": visible_text[:2000],
                "captured_responses": captured_responses[:20],
                "artifacts": {
                    "screenshot": str(screenshot_path.resolve()),
                    "html": str(html_path.resolve()),
                    "json": str(json_path.resolve()),
                },
            }
            json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result
        finally:
            with suppress(Exception):
                page.remove_listener("response", on_response)
            await asyncio.shield(self._close_page(page))

    async def _close_page(self, page) -> None:
        with suppress(Exception):
            await page.close()

    async def _extract_visible_text(self, page) -> str:
        return await page.evaluate(
            r"""
            () => {
              const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
              const parts = [];
              while (walker.nextNode()) {
                const text = walker.currentNode.textContent.replace(/\s+/g, ' ').trim();
                if (text) parts.push(text);
              }
              return parts.join(' ').slice(0, 10000);
            }
            """
        )

    async def _looks_like_matches_page(self, page) -> bool:
        return await page.evaluate(
            """
            () => {
              const text = document.body.innerText || '';
              return /latest matches|recent matches/i.test(text)
                || document.querySelectorAll('a[href*="/matches/"]').length > 0;
            }
            """
        )

    async def _wait_for_page_settle(self, page, timeout_ms: int) -> None:
        deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000)
        while asyncio.get_event_loop().time() < deadline:
            if await self._looks_like_matches_page(page):
                return
            if await self._is_login_gate(page):
                return
            if not await self._is_cloudflare_challenge(page):
                return
            await page.wait_for_timeout(1500)

    async def _is_login_gate(self, page) -> bool:
        return await page.evaluate(
            """
            () => {
              const text = document.body.innerText || '';
              return /sign in with steam/i.test(text)
                || /sign in to continue/i.test(text)
                || /view this page/i.test(text);
            }
            """
        )

    async def _is_cloudflare_challenge(self, page) -> bool:
        return await page.evaluate(
            """
            () => {
              const text = document.body.innerText || '';
              return /just a moment/i.test(text)
                || /verify you are human/i.test(text)
                || /cloudflare/i.test(text);
            }
            """
        )

    def _classify_page(
        self,
        *,
        title: str,
        page_url: str,
        html: str,
        rows: List[Dict[str, Any]],
    ) -> str:
        lowered = f"{title}\n{page_url}\n{html[:5000]}".lower()
        if rows:
            return "accessible_recent_matches"
        if "just a moment" in lowered or "cloudflare" in lowered:
            return "challenge_blocked"
        if "sign in with steam" in lowered or "sign in to continue" in lowered:
            return "login_gated"
        if "player not found" in lowered or "404" in lowered:
            return "not_found"
        return "unknown"

    async def _extract_match_rows(self, page, *, limit: int) -> List[Dict[str, Any]]:
        rows = await page.evaluate(
            r"""
            (limit) => {
              const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
              const parseDuration = (text) => {
                const parts = clean(text).split(':').map((x) => Number.parseInt(x, 10));
                if (!text || parts.some((x) => Number.isNaN(x))) return null;
                if (parts.length === 2) return parts[0] * 60 + parts[1];
                if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
                return null;
              };
              const toMatchRow = (row, matchAnchor) => {
                const rowText = clean(row.innerText);
                const href = matchAnchor?.getAttribute('href') || '';
                const match = href.match(/\/matches\/(\d+)/);
                if (!match) return null;
                const heroAnchor = row.querySelector('a[href*="/heroes/"]');
                const heroSlugMatch = (heroAnchor?.getAttribute('href') || '').match(/\/heroes\/([a-z0-9\-]+)/i);
                const heroNameAnchor = Array.from(row.querySelectorAll('a[href*="/matches/"]')).find((a) => !/match/i.test(clean(a.textContent)));
                const timeEl = row.querySelector('time');
                const datetimeValue = timeEl?.getAttribute('datetime') || timeEl?.getAttribute('title') || timeEl?.textContent || null;
                const resultMatch = rowText.match(/\b(Won|Lost)\s+Match\b/i);
                const durationMatch = rowText.match(/\b(?:\d+:)?\d{1,2}:\d{2}\b/);
                const kdaMatch = rowText.match(/\b\d+\/\d+\/\d+\b/);
                const modeMatch = rowText.match(/\b(All Pick|Turbo|Ability Draft|Single Draft|Captains Mode|Random Draft)\b/i);
                const typeMatch = rowText.match(/\b(Rankedx\d+|Normalx\d+|Practice)\b/i);
                return {
                  match_id: match[1],
                  match_url: new URL(href, window.location.origin).toString(),
                  hero_name: clean(heroNameAnchor?.textContent || heroAnchor?.getAttribute('alt') || ''),
                  hero_slug: heroSlugMatch ? heroSlugMatch[1] : null,
                  duration_text: durationMatch ? durationMatch[0] : null,
                  duration_sec: durationMatch ? parseDuration(durationMatch[0]) : null,
                  result_text: resultMatch ? resultMatch[0] : null,
                  relative_or_absolute_time: clean(datetimeValue) || null,
                  kda_text: kdaMatch ? kdaMatch[0] : null,
                  mode_text: modeMatch ? modeMatch[0] : null,
                  queue_text: typeMatch ? typeMatch[0] : null,
                  row_text: rowText,
                };
              };

              const structuredRows = Array.from(document.querySelectorAll('.performances-overview .r-row'));
              if (structuredRows.length) {
                return structuredRows.slice(0, limit).map((row) => {
                  const anchors = Array.from(row.querySelectorAll('a[href*="/matches/"]'));
                  const matchAnchor = anchors.find((a) => !/match/i.test(clean(a.textContent))) || anchors[0];
                  return toMatchRow(row, matchAnchor);
                }).filter(Boolean);
              }

              const anchors = Array.from(document.querySelectorAll('a[href*="/matches/"]'));
              const seen = new Set();
              const out = [];

              for (const anchor of anchors) {
                const href = anchor.getAttribute('href') || '';
                const match = href.match(/\/matches\/(\d+)/);
                if (!match) continue;
                const matchId = match[1];
                if (seen.has(matchId)) continue;
                seen.add(matchId);

                const matchLinkCount = (node) => node
                  ? node.querySelectorAll('a[href*="/matches/"]').length
                  : 0;
                let row = anchor.closest('tr, article, li, .match, .match-row, .r-row');
                if (!row || matchLinkCount(row) > 1) {
                  row = anchor.closest('.content-inner') || anchor.parentElement;
                }
                if (matchLinkCount(row) > 1) {
                  row = anchor.parentElement;
                }
                if (!row) continue;
                const parsed = toMatchRow(row, anchor);
                if (!parsed) continue;
                out.push(parsed);
                if (out.length >= limit) break;
              }
              return out;
            }
            """,
            limit,
        )
        normalized = []
        duration_re = re.compile(r"\b(?:\d+:)?\d{1,2}:\d{2}\b")
        kda_re = re.compile(r"\b\d+/\d+/\d+\b")
        result_re = re.compile(r"\b(?:Won|Lost) Match\b", re.IGNORECASE)
        mode_re = re.compile(r"\b(All Pick|Turbo|Ability Draft|Single Draft|Captains Mode|Random Draft)\b", re.IGNORECASE)
        queue_re = re.compile(r"\b(Rankedx\d+|Normalx\d+|Practice)\b", re.IGNORECASE)
        for row in rows:
            row_text = row.get("row_text") or ""
            duration_match = duration_re.search(row_text)
            kda_match = kda_re.search(row_text)
            result_match = result_re.search(row_text)
            mode_match = mode_re.search(row_text)
            queue_match = queue_re.search(row_text)
            duration_text = row.get("duration_text") or (duration_match.group(0) if duration_match else None)
            kda_text = row.get("kda_text") or (kda_match.group(0) if kda_match else None)
            result_text = row.get("result_text") or (result_match.group(0) if result_match else None)
            mode_text = row.get("mode_text") or (mode_match.group(0) if mode_match else None)
            queue_text = row.get("queue_text") or (queue_match.group(0) if queue_match else None)
            duration_sec = row.get("duration_sec")
            if duration_sec is None and duration_text:
                duration_sec = self._parse_duration(duration_text)
            normalized.append(
                {
                    "match_id": row.get("match_id"),
                    "match_url": row.get("match_url"),
                    "hero_name": row.get("hero_name") or None,
                    "hero_slug": row.get("hero_slug"),
                    "duration_text": duration_text,
                    "duration_sec": duration_sec,
                    "result_text": result_text,
                    "relative_or_absolute_time": row.get("relative_or_absolute_time") or None,
                    "kda_text": kda_text,
                    "mode_text": mode_text,
                    "queue_text": queue_text,
                    "row_text": row_text,
                }
            )
        return normalized

    @staticmethod
    def _parse_duration(raw: Optional[str]) -> Optional[int]:
        if not raw:
            return None
        parts = [int(part) for part in raw.split(":")]
        if len(parts) == 2:
            minutes, seconds = parts
            return minutes * 60 + seconds
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return hours * 3600 + minutes * 60 + seconds
        return None


def parse_player_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"\d{17}", value):
        return str(int(value) - STEAM_ID_OFFSET)
    match = re.search(r"/players/(\d+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"\d+", value):
        return value
    raise ValueError(f"Unsupported Dotabuff/Steam identifier: {value}")


async def run_cli() -> int:
    parser = argparse.ArgumentParser(description="Dotabuff Playwright extractor POC")
    parser.add_argument("identifier", help="Dotabuff player id, Dotabuff player URL, or SteamID64")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--cookies-json", help="Optional Playwright-style cookie JSON to preload")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    parser.add_argument("--wait-ms", type=int, default=DEFAULT_WAIT_AFTER_LOAD_MS)
    parser.add_argument("--settle-timeout-ms", type=int, default=DEFAULT_SETTLE_TIMEOUT_MS)
    parser.add_argument("--headed", action="store_true", help="Run Chromium in headed mode")
    args = parser.parse_args()

    player_id = parse_player_id(args.identifier)
    async with DotabuffBrowserClient(
        profile_dir=Path(args.profile_dir),
        timeout_ms=args.timeout_ms,
        headless=not args.headed,
        cookies_json=Path(args.cookies_json) if args.cookies_json else None,
    ) as client:
        result = await client.fetch_player_matches(
            player_id,
            limit=args.limit,
            output_dir=Path(args.output_dir),
            wait_after_load_ms=args.wait_ms,
            settle_timeout_ms=args.settle_timeout_ms,
        )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "accessible_recent_matches" else 1


def main() -> None:
    raise SystemExit(asyncio.run(run_cli()))


if __name__ == "__main__":
    main()
