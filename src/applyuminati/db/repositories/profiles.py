"""Profile and claim persistence."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.provenance import AssertionLevel, Claim
from applyuminati.db.mappers import claim_to_row, profile_to_row, row_to_profile
from applyuminati.db.models import ProfileRow


class ProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(self) -> CareerProfile | None:
        row = (
            await self._session.scalars(select(ProfileRow).where(ProfileRow.is_active).limit(1))
        ).first()
        return self._to_profile(row) if row else None

    async def get(self, profile_id: str) -> CareerProfile | None:
        row = await self._session.get(ProfileRow, profile_id)
        return self._to_profile(row) if row else None

    async def upsert(self, profile: CareerProfile) -> CareerProfile:
        """Replace the profile and its claim ledger wholesale.

        The claim ledger inside the domain object is authoritative, so rows
        that disappeared are deleted and the rest rewritten — which also keeps
        supersession pointers consistent.
        """
        row = await self._session.get(ProfileRow, profile.id)
        if row is None:
            row = ProfileRow(id=profile.id)
            self._session.add(row)
        profile_to_row(profile, row=row)

        existing = {claim_row.id: claim_row for claim_row in row.claims}
        keep: set[str] = set()
        for claim in profile.claims:
            keep.add(claim.id)
            claim_row = existing.get(claim.id)
            if claim_row is None:
                self._session.add(claim_to_row(claim, row.id))
            else:
                claim_to_row(claim, row.id, row=claim_row)
        for claim_id, claim_row in existing.items():
            if claim_id not in keep:
                await self._session.delete(claim_row)
        await self._session.flush()
        return profile

    async def claims_for(
        self,
        profile_id: str,
        *,
        level: AssertionLevel | None = None,
        tag: str | None = None,
    ) -> list[Claim]:
        profile = await self.get(profile_id)
        if profile is None:
            return []
        claims = profile.claims
        if level is not None:
            claims = [claim for claim in claims if claim.level is level]
        if tag is not None:
            lowered = tag.lower()
            claims = [claim for claim in claims if lowered in (t.lower() for t in claim.tags)]
        return claims

    async def add_claims(self, profile_id: str, claims: Sequence[Claim]) -> list[Claim]:
        row = await self._session.get(ProfileRow, profile_id)
        if row is None:
            msg = f"profile {profile_id} does not exist"
            raise ValueError(msg)
        for claim in claims:
            self._session.add(claim_to_row(claim, row.id))
        await self._session.flush()
        return list(claims)

    def _to_profile(self, row: ProfileRow) -> CareerProfile:
        return row_to_profile(row, row.claims)


__all__ = ["ProfileRepository"]
