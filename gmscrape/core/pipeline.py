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
from ..emails.patterns import build_owner_permutations, build_permutations
from ..emails.people import (
    choose_owner,
    email_matches_person,
    emails_for_person_in_text,
    is_medical,
    owner_candidates_from_search,
)
from ..emails.score import keep_candidate, score_business
from ..filters.chains import (
    ChainProfile,
    brand_display_name,
    chain_profile,
    classify,
    profile_for_kind,
    should_keep,
)
from ..models import (
    BusinessResult,
    CONTACT_OWNER,
    EmailCandidate,
    Place,
    QuerySpec,
    SOURCE_MAPS,
    SOURCE_SEARCH,
    VerificationResult,
    V_CATCH_ALL,
    V_VALID,
)
from ..providers import get_maps_provider, get_verifier, get_web_search
from ..providers.base import EmailVerifier, MapsProvider, ProviderError
from ..providers.search.base import SearchResponse, WebSearchProvider
from ..providers.search.openwebninja import parse_response
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
from ..data.domains import FREE_MAIL_DOMAINS
from ..util import city_from_address, domain_has_mx, hostname, registered_domain
from ..web.crawl import scrape_site
from ..web.discover import confirm_website, discovery_query, pick_website
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
    search_provider: str = ""
    search_calls: int = 0
    search_cache_hits: int = 0
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
        owners = [r for r in self.results if r.owner]
        return {
            "queries": len(self.queries),
            "businesses": len(self.results),
            "with_website": sum(1 for r in self.results if r.place.website),
            "websites_discovered": sum(
                1 for r in self.results if r.website_source == "search" and r.place.website
            ),
            "owners_found": len(owners),
            "owners_from_site": sum(1 for r in owners if r.owner.source.startswith("site")),
            "owners_from_search": sum(1 for r in owners if r.owner.source.startswith("search")),
            "owner_emails": sum(1 for r in self.results if r.best_owner_email),
            "lead_rows": sum(max(1, len(r.lead_contacts())) for r in self.results),
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
            "web_search_calls": self.search_calls,
            "web_search_cache_hits": self.search_cache_hits,
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
        web_search: Optional[WebSearchProvider] = None,
    ) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self.store = store or Store(settings.db_path)
        self._owns_store = store is None
        self.maps = maps or get_maps_provider(settings)
        self.verifier = verifier or get_verifier(settings)
        self.web_search = web_search if web_search is not None else get_web_search(settings)
        self.progress = progress or (lambda event, data: None)
        self._search_calls = 0
        self._search_cache_hits = 0
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
            search_provider=self.web_search.name if self.web_search else "none",
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

        strict_guesses = self.settings.require_verified_guesses and self.verifier.requires_key
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
            if strict_guesses:
                # A guessed address becomes a lead only once a verifier said
                # the mailbox exists. Everything else stays in the emails
                # export, flagged, but never in the leads.
                for candidate in result.guessed_emails:
                    if candidate.status != V_VALID or (
                        candidate.verification and candidate.verification.is_catch_all
                    ):
                        candidate.lead_eligible = False
                        candidate.notes.append("not_lead_eligible:unverified_guess")
            self.store.save_business(result, report.run_id)

        report.results = results
        report.verification_calls = self._verify_calls
        report.verification_cache_hits = self._cache_hits
        report.search_calls = self._search_calls
        report.search_cache_hits = self._search_cache_hits
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
        if self.web_search is not None:
            self.web_search.close()
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
            profile = chain_profile(place, verdict)
            if profile is not None:
                result.chain_kind = profile.kind
                result.target_role = profile.label
                result.notes.append(f"chain_brand={brand_display_name(place)}")
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

    # --- stage 3: websites, owners ---------------------------------------
    async def _scrape_websites(self, results: Sequence[BusinessResult]) -> None:
        if not self.settings.crawl_websites:
            for result in results:
                if result.place.website:
                    result.website_status = "skipped:crawl_disabled"
            return
        searching = self.web_search is not None
        discover = searching and self.settings.discover_websites
        owner_search = searching and self.settings.find_owners and self.settings.owner_search
        chain_people = owner_search and self.settings.chain_people
        targets = [
            r for r in results
            if r.place.website or discover or (r.is_chain and chain_people)
        ]
        if not targets:
            return

        search_sem = asyncio.Semaphore(max(1, self.settings.web_search_concurrency))
        async with Fetcher(self.settings, cache=self.store) as fetcher:
            semaphore = asyncio.Semaphore(max(1, self.settings.http_concurrency))
            done = 0

            async def worker(result: BusinessResult) -> None:
                nonlocal done
                async with semaphore:
                    try:
                        await self._process_site(
                            result, fetcher, search_sem, discover=discover,
                            owner_search=owner_search,
                        )
                    except Exception as exc:  # a single bad site must not kill the run
                        log.warning("processing failed for %s: %s", result.place.name, exc)
                        result.website_status = result.website_status or f"error:{type(exc).__name__}"
                    done += 1
                    self._publish([result], STATUS_CRAWLED)
                    self.progress(
                        "site_done",
                        {
                            "website": result.place.website,
                            "emails": len(result.found_emails),
                            "owner": result.owner.name if result.owner else "",
                            "done": done,
                            "total": len(targets),
                        },
                    )

            await asyncio.gather(*(worker(r) for r in targets))

    async def _process_site(
        self,
        result: BusinessResult,
        fetcher: Fetcher,
        search_sem: asyncio.Semaphore,
        *,
        discover: bool,
        owner_search: bool,
    ) -> None:
        """Discover (if needed), crawl, confirm, and find the owner for one business."""
        place = result.place
        profile = self._profile(result)

        if not place.website:
            if profile is not None:
                # A chain's site is corporate; nothing to discover locally.
                # The right local person can still be found by search.
                result.website_status = "no_website"
                if owner_search and self.settings.chain_people:
                    await self._find_chain_person(result, profile, search_sem)
                return
            if not discover:
                result.website_status = "no_website"
                return
            response = await self._search(discovery_query(place), search_sem)
            guess = (
                pick_website(place, response.hits, min_score=self.settings.website_min_confidence)
                if response.ok else None
            )
            if guess is None:
                result.website_status = "no_website"
                result.notes.append(
                    f"website_search:{response.error}" if response.error else "website_search:no_clear_match"
                )
                return
            place.website = guess.url
            place.domain = guess.domain
            result.website_source = "search"
            result.website_confidence = guess.score
            result.notes.append(f"website_discovered:{','.join(guess.reasons)}")
        else:
            result.website_source = result.website_source or "maps"

        find_owner = self.settings.find_owners and (profile is None or self.settings.chain_people)
        scrape = await scrape_site(
            fetcher, place.website, self.settings, place.domain,
            business_name=place.name,
            medical=is_medical(place.category, place.query),
            find_owner=find_owner,
            owner_min_confidence=self.settings.owner_min_confidence,
            max_pages=self.settings.chain_crawl_pages if profile is not None else None,
            preferred_titles=profile.target_titles if profile is not None else (),
        )

        if result.website_source == "search":
            # A discovered site earns its keep only by mentioning the business.
            confirmed, reasons = (
                confirm_website(place, scrape.homepage_text) if scrape.status == "ok"
                else (False, [scrape.status])
            )
            if not confirmed:
                result.website_status = f"discovered_unconfirmed:{','.join(reasons)}"
                result.notes.append(f"rejected_site={hostname(place.website)}")
                place.website = ""
                place.domain = ""
                result.website_source = ""
                result.website_confidence = 0
                return
            result.notes.append(f"site_confirmed:{','.join(reasons)}")

        result.website_status = scrape.status
        result.pages_crawled = scrape.pages
        known = {c.email for c in result.emails}
        result.emails.extend(c for c in scrape.candidates if c.email not in known)
        if scrape.errors:
            result.notes.append(f"crawl_errors={len(scrape.errors)}")

        if find_owner:
            if scrape.owner is not None:
                result.owner = scrape.owner
            elif owner_search and profile is not None:
                await self._find_chain_person(result, profile, search_sem)
            elif owner_search and place.domain:
                await self._find_owner_by_search(result, search_sem)
            if result.owner is not None:
                self._tag_owner_emails(result)

    async def _find_owner_by_search(
        self, result: BusinessResult, search_sem: asyncio.Semaphore
    ) -> None:
        place = result.place
        city = place.city or city_from_address(place.address)
        where = city or place.state or ""
        query = f"who is the owner of {place.name}" + (f" in {where}" if where else "")
        response = await self._search(query, search_sem)
        result.owner_search_done = True
        if not response.ok:
            result.notes.append(f"owner_search:{response.error}")
            return
        blocks = response.all_text_blocks()
        candidates = owner_candidates_from_search(blocks, place.name, city)
        result.owner = choose_owner(candidates, min_confidence=self.settings.owner_min_confidence)
        if result.owner is None:
            result.notes.append(
                "owner_search:no_confident_match" if candidates else "owner_search:no_mention"
            )
        else:
            self._harvest_person_emails(result, blocks, CONTACT_OWNER)

    async def _find_chain_person(
        self, result: BusinessResult, profile: ChainProfile, search_sem: asyncio.Semaphore
    ) -> None:
        """The local franchisee / store manager of a chain location, by search.

        Up to two queries (a question for the AI Overview, then a role search);
        only text that names both the brand and the city counts.
        """
        place = result.place
        city = place.city or city_from_address(place.address)
        if not city:
            # Without a city there is no way to tie a manager to *this* store.
            result.notes.append("chain_person:no_city")
            return
        where = ", ".join(p for p in (city, place.state) if p)
        brand = brand_display_name(place)
        best_candidates: list = []
        for template in profile.queries:
            query = template.format(brand=brand, name=place.name, city=city,
                                    state=place.state, where=where)
            response = await self._search(query, search_sem)
            result.owner_search_done = True
            if not response.ok:
                result.notes.append(f"chain_person:{response.error}")
                break
            blocks = response.all_text_blocks()
            candidates = owner_candidates_from_search(
                blocks, brand, city, require_location=True
            )
            best_candidates.extend(candidates)
            person = choose_owner(
                best_candidates, min_confidence=self.settings.owner_min_confidence,
                preferred_titles=profile.target_titles,
            )
            if person is not None:
                result.owner = person
                self._harvest_person_emails(result, blocks, profile.contact_type)
                return
        result.notes.append(
            "chain_person:no_confident_match" if best_candidates else "chain_person:no_mention"
        )

    def _harvest_person_emails(
        self, result: BusinessResult, blocks: Sequence[tuple[str, str]], contact_type: str
    ) -> None:
        """An address in the search text that spells the person's name is theirs."""
        person = result.owner
        if person is None:
            return
        known = {c.email for c in result.emails}
        for source, text in blocks:
            for email, context in emails_for_person_in_text(text, person):
                if email in known:
                    continue
                known.add(email)
                domain = email.rsplit("@", 1)[-1]
                result.emails.append(EmailCandidate(
                    email=email,
                    source=SOURCE_SEARCH,
                    context=context,
                    contact_type=contact_type,
                    contact_name=person.name,
                    contact_title=person.title,
                    on_business_domain=bool(result.place.domain)
                    and registered_domain(domain) == result.place.domain,
                    is_personal_domain=domain in FREE_MAIL_DOMAINS,
                    notes=[f"from_{source}"],
                ))

    def _tag_owner_emails(self, result: BusinessResult) -> None:
        """An address found on the site that spells the person's name is theirs -
        on the business domain or a personal one (john.kowalski@gmail.com)."""
        owner = result.owner
        assert owner is not None
        contact_type = self._person_contact_type(result)
        for candidate in result.emails:
            if candidate.from_permutation or candidate.is_owner:
                continue
            if not (candidate.on_business_domain or candidate.is_personal_domain):
                continue
            if email_matches_person(candidate.local_part, owner):
                candidate.contact_type = contact_type
                candidate.contact_name = owner.name
                candidate.contact_title = owner.title
                candidate.notes.append("matches_person_name")

    @staticmethod
    def _profile(result: BusinessResult) -> Optional[ChainProfile]:
        if not result.is_chain or not result.chain_kind:
            return None
        return profile_for_kind(result.chain_kind)

    def _person_contact_type(self, result: BusinessResult) -> str:
        profile = self._profile(result)
        return profile.contact_type if profile is not None else CONTACT_OWNER

    # --- web search (cached, budgeted, never fatal) -----------------------
    async def _search(self, query: str, search_sem: asyncio.Semaphore) -> SearchResponse:
        assert self.web_search is not None
        cached = self.store.get_search(self.web_search.name, query, self.settings.search_cache_ttl_hours)
        if cached is not None:
            self._search_cache_hits += 1
            response = parse_response(query, cached)
            response.from_cache = True
            return response
        async with search_sem:
            return await asyncio.to_thread(self._search_sync, query)

    def _search_sync(self, query: str) -> SearchResponse:
        assert self.web_search is not None
        disabled = getattr(self, "_search_disabled", "")
        if disabled:
            return SearchResponse(query=query, error=disabled)
        try:
            response = self.web_search.search(query, limit=10)
        except ProviderError as exc:
            # A rejected key fails every call the same way - stop asking.
            self._search_disabled = str(exc)
            log.error("web search disabled for this run: %s", exc)
            return SearchResponse(query=query, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - search is best effort
            log.warning("web search failed for %r: %s", query, exc)
            return SearchResponse(query=query, error=f"{type(exc).__name__}: {exc}")
        self._search_calls += 1
        if response.ok and response.raw:
            self.store.put_search(self.web_search.name, query, response.raw)
        return response

    # --- stage 4: permutations --------------------------------------------
    def _domain_has_mx(self, result: BusinessResult, domain: str) -> bool:
        cached = self.store.get_domain_facts(domain)
        has_mx = cached[0] if cached else None
        if has_mx is None:
            has_mx = domain_has_mx(domain)
            self.store.put_domain_facts(domain, has_mx=has_mx)
        result.domain_has_mx = has_mx
        if cached and cached[1] is not None:
            result.domain_is_catch_all = cached[1]
        return has_mx

    def _plan_permutations(self, results: Sequence[BusinessResult]) -> None:
        if not self.settings.permutations:
            for result in results:
                if not result.found_emails:
                    result.permutations_skipped_reason = "permutations_disabled"
            return

        for result in results:
            domain = result.place.domain or registered_domain(result.place.website)
            known = [c.email for c in result.emails]

            # General inbox: only when nothing usable was found on the site.
            usable = [
                c for c in result.found_emails
                if not c.is_low_value and (c.on_business_domain or c.is_personal_domain)
            ]
            if not usable:
                if not domain:
                    result.permutations_skipped_reason = "no_domain"
                else:
                    self._domain_has_mx(result, domain)
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
                        exclude=known,
                    )
                    if plan.allowed:
                        result.emails.extend(plan.candidates)
                        known.extend(c.email for c in plan.candidates)
                        result.notes.append(f"guessed={len(plan.candidates)}")
                    else:
                        result.permutations_skipped_reason = plan.skipped_reason

            # Owner mailbox: whenever we know who they are and haven't found it.
            owner = result.owner
            if not (self.settings.find_owners and owner and domain):
                continue
            if owner.confidence < self.settings.owner_min_confidence:
                result.notes.append("owner_guess_skipped:low_confidence")
                continue
            if any(c.is_owner and not c.from_permutation for c in result.emails):
                continue
            self._domain_has_mx(result, domain)
            plan = build_owner_permutations(
                domain,
                owner,
                max_candidates=self.settings.owner_permutation_max,
                require_mx=self.settings.permutation_require_mx,
                is_chain=result.is_chain,
                allow_chains=self.settings.chain_person_guesses,
                exclude=known,
                contact_type=self._person_contact_type(result),
            )
            if plan.allowed:
                result.emails.extend(plan.candidates)
                result.notes.append(f"owner_guessed={len(plan.candidates)}")
            else:
                result.notes.append(f"owner_guess_skipped:{plan.skipped_reason}")

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
        """Walk each contact type's guesses in order; stop at the first hit."""
        guesses = result.guessed_emails
        chains = [
            [c for c in guesses if not c.is_owner],
            [c for c in guesses if c.is_owner],
        ]
        for chain in chains:
            if not chain:
                continue
            if result.domain_is_catch_all:
                # Already known to accept anything - no guess can be proven.
                self._drop_untested(result, chain, 0)
                continue
            self._verify_chain(result, chain)

    def _verify_chain(self, result: BusinessResult, chain: list[EmailCandidate]) -> None:
        stopped_at: Optional[int] = None
        for index, candidate in enumerate(chain):
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
        if stopped_at is not None:
            self._drop_untested(result, chain, stopped_at)

    @staticmethod
    def _drop_untested(result: BusinessResult, chain: list[EmailCandidate], keep: int) -> None:
        """Guesses we never checked are not results - drop them rather than
        shipping a dozen unverified addresses per business."""
        untested = {c.email for c in chain[keep:]}
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
