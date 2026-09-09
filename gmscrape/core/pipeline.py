"""End-to-end orchestration.

    queries -> Maps provider -> dedupe -> chain classification
            -> website crawl -> email extraction
            -> permutations (only when nothing was found)
            -> verification (cached, budgeted, stop-on-first-valid)
            -> scoring -> SQLite + exports
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional, Sequence

from ..config import Settings
from ..emails.patterns import build_permutations
from ..emails.score import keep_candidate, score_business
from ..filters.chains import classify, should_keep
from ..models import (
    BusinessResult,
    EmailCandidate,
    Place,
    QuerySpec,
    SOURCE_MAPS,
    VerificationResult,
    V_CATCH_ALL,
    V_VALID,
)
from ..providers import get_maps_provider, get_verifier
from ..providers.base import EmailVerifier, MapsProvider, ProviderError
from ..providers.verify.local import prefilter
from ..query import parse_queries
from ..store.db import Store
from ..store.sinks import (
    STATUS_CRAWLED,
    STATUS_DONE,
    STATUS_GUESSED,
    STATUS_QUEUED,
    STATUS_VERIFIED,
    LeadSink,
    NullSink,
)
from ..util import domain_has_mx, registered_domain
from ..web.crawl import scrape_site
from ..web.fetch import Fetcher

log = logging.getLogger(__name__)

ProgressHook = Callable[[str, dict], None]


@dataclass
class RunReport:
    """Summary of one pipeline run."""

    run_id: str
    queries: list[str] = field(default_factory=list)
    results: list[BusinessResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    maps_provider: str = ""
    verify_provider: str = ""
    verification_calls: int = 0
    verification_cache_hits: int = 0
    errors: list[str] = field(default_factory=list)
    sinks: list[Any] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def stats(self) -> dict[str, object]:
        with_email = [r for r in self.results if r.best_email]
        scraped = [r for r in self.results if r.found_emails]
        guessed_only = [r for r in with_email if not r.found_emails]
        valid = [r for r in with_email if r.best_email and r.best_email.status == V_VALID]
        return {
            "queries": len(self.queries),
            "businesses": len(self.results),
            "with_website": sum(1 for r in self.results if r.place.website),
            "with_any_email": len(with_email),
            "emails_scraped_from_site": len(scraped),
            "emails_guessed_only": len(guessed_only),
            "best_email_verified_valid": len(valid),
            "personal_domain_emails": sum(
                1 for r in self.results for e in r.emails if e.is_personal_domain
            ),
            "chains_flagged": sum(1 for r in self.results if r.is_chain),
            "total_emails": sum(len(r.emails) for r in self.results),
            "verification_api_calls": self.verification_calls,
            "verification_cache_hits": self.verification_cache_hits,
            "duration_seconds": round(self.duration, 1),
        }


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        *,
        store: Optional[Store] = None,
        maps: Optional[MapsProvider] = None,
        verifier: Optional[EmailVerifier] = None,
        progress: Optional[ProgressHook] = None,
        sinks: Optional[Sequence[LeadSink]] = None,
    ) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self.store = store or Store(settings.db_path)
        self._owns_store = store is None
        self.maps = maps or get_maps_provider(settings)
        self.verifier = verifier or get_verifier(settings)
        self.progress = progress or (lambda event, data: None)
        self.sinks: list[LeadSink] = list(sinks) if sinks else [NullSink()]
        self._verify_calls = 0
        self._cache_hits = 0

    # --- public API --------------------------------------------------------
    def run(self, queries: Sequence[str], *, run_id: Optional[str] = None) -> RunReport:
        specs = parse_queries(queries)
        if not specs:
            raise ValueError("no usable queries provided")
        report = RunReport(
            run_id=run_id or uuid.uuid4().hex[:12],
            queries=[s.search_string for s in specs],
            maps_provider=self.maps.name,
            verify_provider=self.verifier.name,
        )
        self.store.start_run(report.run_id, report.queries, asdict(self.settings))
        self._sink_call("start_run", report.run_id, {
            "queries": report.queries,
            "maps_provider": report.maps_provider,
            "verify_provider": report.verify_provider,
        })

        places = self._collect_places(specs, report)
        results = self._classify(places)
        if results:
            # Publish and flush before any slow work, so the table is fully
            # populated the moment the run starts rather than after the crawl.
            self._publish(results, STATUS_QUEUED)
            self._sink_call("flush")
            asyncio.run(self._scrape_websites(results))
            self._publish(results, STATUS_CRAWLED)
            self._plan_permutations(results)
            self._publish([r for r in results if r.guessed_emails], STATUS_GUESSED)
            self._verify_all(results)
            self._publish(results, STATUS_VERIFIED)

        for result in results:
            score_business(result)
            result.emails = [
                candidate for candidate in result.emails
                if keep_candidate(
                    candidate,
                    keep_risky=self.settings.keep_risky,
                    keep_invalid=self.settings.keep_invalid,
                )
                and candidate.confidence >= self.settings.min_confidence
            ]
            self.store.save_business(result, report.run_id)

        report.results = results
        report.verification_calls = self._verify_calls
        report.verification_cache_hits = self._cache_hits
        report.sinks = [s for s in self.sinks if not isinstance(s, NullSink)]
        self._publish(results, STATUS_DONE)
        report.finished_at = time.time()
        stats = report.stats()
        self.store.finish_run(report.run_id, stats)
        self._sink_call("finish_run", report.run_id, stats)
        self.progress("run_finished", {"stats": stats})
        return report

    def _publish(self, results: Sequence[BusinessResult], status: str) -> None:
        """Push a stage's results to every sink. A sink must never break a run."""
        for sink in self.sinks:
            try:
                sink.upsert(results, status)
            except Exception as exc:  # noqa: BLE001 - sinks are best-effort
                log.warning("sink %s failed at %s: %s", type(sink).__name__, status, exc)

    def _sink_call(self, method: str, *args: object) -> None:
        for sink in self.sinks:
            try:
                getattr(sink, method)(*args)
            except Exception as exc:  # noqa: BLE001
                log.warning("sink %s.%s failed: %s", type(sink).__name__, method, exc)

    def close(self) -> None:
        self.maps.close()
        self.verifier.close()
        self._sink_call("close")
        if self._owns_store:
            self.store.close()

    def __enter__(self) -> "Pipeline":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- stage 1: maps -----------------------------------------------------
    def _collect_places(self, specs: Sequence[QuerySpec], report: RunReport) -> list[Place]:
        seen: set[str] = set()
        places: list[Place] = []
        for spec in specs:
            self.progress("query_start", {"query": spec.search_string})
            found = 0
            try:
                for place in self.maps.search(spec, self.settings.results_per_query):
                    key = place.dedupe_key()
                    if key in seen:
                        continue
                    seen.add(key)
                    places.append(place)
                    found += 1
            except ProviderError as exc:
                message = f"{spec.search_string}: {exc}"
                log.error("maps provider failed for %s", message)
                report.errors.append(message)
            self.progress("query_done", {"query": spec.search_string, "found": found})
        return places

    # --- stage 2: chains ---------------------------------------------------
    def _classify(self, places: Sequence[Place]) -> list[BusinessResult]:
        results: list[BusinessResult] = []
        for place in places:
            verdict = classify(place, review_threshold=self.settings.chain_review_threshold)
            if not should_keep(verdict, self.settings.chain_mode):
                continue
            result = BusinessResult(
                place=place,
                is_chain=verdict.is_chain,
                chain_score=verdict.score,
                chain_reasons=verdict.reasons,
            )
            if not place.website:
                result.website_status = "no_website"
            # Some Maps providers surface an email directly; keep it.
            for key in ("email", "emails", "email_1", "contact_email"):
                raw_email = place.raw.get(key) if isinstance(place.raw, dict) else None
                for address in _as_email_list(raw_email):
                    result.emails.append(
                        EmailCandidate(
                            email=address,
                            source=SOURCE_MAPS,
                            source_url=place.google_url,
                            on_business_domain=bool(place.domain)
                            and registered_domain(address.split("@")[-1]) == place.domain,
                            notes=["from_maps_provider"],
                        )
                    )
            results.append(result)
        self.progress("classified", {"businesses": len(results)})
        return results

    # --- stage 3: websites -------------------------------------------------
    async def _scrape_websites(self, results: Sequence[BusinessResult]) -> None:
        if not self.settings.crawl_websites:
            for result in results:
                if result.place.website:
                    result.website_status = "skipped:crawl_disabled"
            return
        targets = [r for r in results if r.place.website]
        if not targets:
            return

        async with Fetcher(self.settings, cache=self.store) as fetcher:
            semaphore = asyncio.Semaphore(max(1, self.settings.http_concurrency))
            done = 0

            async def worker(result: BusinessResult) -> None:
                nonlocal done
                async with semaphore:
                    try:
                        scrape = await scrape_site(
                            fetcher, result.place.website, self.settings, result.place.domain
                        )
                    except Exception as exc:  # a single bad site must not kill the run
                        log.warning("crawl failed for %s: %s", result.place.website, exc)
                        result.website_status = f"unreachable:{type(exc).__name__}"
                        return
                    result.website_status = scrape.status
                    result.pages_crawled = scrape.pages
                    known = {c.email for c in result.emails}
                    result.emails.extend(c for c in scrape.candidates if c.email not in known)
                    if scrape.errors:
                        result.notes.append(f"crawl_errors={len(scrape.errors)}")
                    done += 1
                    # Stream this one straight out; the sink batches for us.
                    self._publish([result], STATUS_CRAWLED)
                    self.progress(
                        "site_done",
                        {
                            "website": result.place.website,
                            "emails": len(scrape.candidates),
                            "done": done,
                            "total": len(targets),
                        },
                    )

            await asyncio.gather(*(worker(r) for r in targets))

    # --- stage 4: permutations --------------------------------------------
    def _plan_permutations(self, results: Sequence[BusinessResult]) -> None:
        if not self.settings.permutations:
            for result in results:
                if not result.found_emails:
                    result.permutations_skipped_reason = "permutations_disabled"
            return

        for result in results:
            usable = [
                c for c in result.found_emails
                if not c.is_low_value and (c.on_business_domain or c.is_personal_domain)
            ]
            if usable:
                continue

            domain = result.place.domain or registered_domain(result.place.website)
            if not domain:
                result.permutations_skipped_reason = "no_domain"
                continue

            cached = self.store.get_domain_facts(domain)
            has_mx = cached[0] if cached else None
            if has_mx is None:
                has_mx = domain_has_mx(domain)
                self.store.put_domain_facts(domain, has_mx=has_mx)
            result.domain_has_mx = has_mx
            if cached and cached[1] is not None:
                result.domain_is_catch_all = cached[1]

            plan = build_permutations(
                domain,
                business_name=result.place.name,
                category=result.place.category,
                business_type=result.place.query,
                tier=self.settings.permutation_tier,
                max_candidates=self.settings.permutation_max,
                require_mx=self.settings.permutation_require_mx,
                is_chain=result.is_chain,
                allow_chains=self.settings.permutations_for_chains,
                exclude=[c.email for c in result.emails],
            )
            if not plan.allowed:
                result.permutations_skipped_reason = plan.skipped_reason
                continue
            result.emails.extend(plan.candidates)
            result.notes.append(f"guessed={len(plan.candidates)}")

    # --- stage 5: verification --------------------------------------------
    def _verify_all(self, results: Sequence[BusinessResult]) -> None:
        if not self.settings.verify_emails:
            return
        paid = self.verifier.requires_key

        # Found addresses: verify each unique address once.
        if self.settings.verify_found:
            unique: dict[str, list[EmailCandidate]] = {}
            for result in results:
                for candidate in result.found_emails:
                    unique.setdefault(candidate.email, []).append(candidate)
            self._verify_batch(list(unique.keys()), unique)

        if not self.settings.verify_permutations:
            return

        if not paid:
            # The local verifier cannot confirm a mailbox, so verifying twelve
            # guesses on one domain returns twelve identical answers. Record the
            # domain fact once and label the guesses honestly instead.
            for result in results:
                for candidate in result.guessed_emails:
                    candidate.notes.append("unverified_guess_local_provider")
            return

        # Guesses: walk each business's list in order and stop at the first
        # deliverable address, so a lead costs 1-2 credits rather than a dozen.
        pending = [r for r in results if r.guessed_emails]
        if not pending:
            return
        workers = max(1, self.settings.verify_concurrency)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(self._verify_guesses_for, pending))

    def _verify_guesses_for(self, result: BusinessResult) -> None:
        guesses = result.guessed_emails
        stopped_at: Optional[int] = None
        for index, candidate in enumerate(guesses):
            if self._budget_exhausted():
                candidate.notes.append("verification_budget_exhausted")
                stopped_at = index
                break
            verification = self._verify_one(candidate.email)
            candidate.verification = verification
            if verification.is_catch_all or verification.status == V_CATCH_ALL:
                result.domain_is_catch_all = True
                self.store.put_domain_facts(candidate.domain, is_catch_all=True)
                # Every guess "passes" on a catch-all domain, so checking more
                # of them buys nothing. Keep this one, flagged as unproven.
                candidate.notes.append("catch_all_domain")
                stopped_at = index + 1
                break
            if verification.status == V_VALID:
                self.store.put_domain_facts(candidate.domain, is_catch_all=False)
                if self.settings.stop_on_first_valid:
                    stopped_at = index + 1
                    break

        if stopped_at is None:
            return
        # Guesses we never checked are not results - drop them rather than
        # shipping a dozen unverified addresses per business.
        untested = {c.email for c in guesses[stopped_at:]}
        if untested:
            result.emails = [c for c in result.emails if c.email not in untested]
            result.notes.append(f"guesses_not_checked={len(untested)}")

    def _verify_batch(
        self, emails: Sequence[str], index: dict[str, list[EmailCandidate]]
    ) -> None:
        if not emails:
            return
        workers = max(1, self.settings.verify_concurrency)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for email, verification in zip(emails, pool.map(self._verify_one, emails)):
                for candidate in index.get(email, []):
                    candidate.verification = verification

    def _verify_one(self, email: str) -> VerificationResult:
        cached = self.store.get_verification(email)
        if cached is not None:
            self._cache_hits += 1
            return cached

        local = prefilter(email)
        if local is not None:
            self.store.put_verification(email, local)
            return local

        if self._budget_exhausted():
            return VerificationResult(
                status="unknown", provider=self.verifier.name,
                error="verification_budget_exhausted",
            )
        try:
            result = self.verifier.verify(email)
            if self.verifier.requires_key:
                self._verify_calls += 1
        except ProviderError as exc:
            log.warning("verification failed for %s: %s", email, exc)
            return VerificationResult(
                status="unknown", provider=self.verifier.name, error=str(exc)
            )
        except Exception as exc:  # keep one bad address from killing the run
            log.warning("verification error for %s: %s", email, exc)
            return VerificationResult(
                status="unknown", provider=self.verifier.name, error=str(exc)
            )
        self.store.put_verification(email, result)
        if result.mx_found is not None:
            self.store.put_domain_facts(email.split("@")[-1], has_mx=result.mx_found)
        return result

    def _budget_exhausted(self) -> bool:
        budget = self.settings.verify_budget
        return bool(budget) and self._verify_calls >= budget


def _as_email_list(value: object) -> list[str]:
    """Maps providers hand back an email as a string, a list, or a dict."""
    out: list[str] = []
    if isinstance(value, str):
        out = [part.strip() for part in value.replace(";", ",").split(",")]
    elif isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_as_email_list(item))
    elif isinstance(value, dict):
        for key in ("value", "email", "address"):
            if key in value:
                out.extend(_as_email_list(value[key]))
    return [e.lower() for e in out if e and "@" in e and " " not in e]
