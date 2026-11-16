"""The JSON API's contract: every request body and every response shape.

Responses are declared, not whatever a query happens to return: a column added to a table is
published only when it is added here. The OpenAPI document (/api/openapi.json) is generated from
these models (`python -m app.cli openapi` writes it to a file), and tests/test_api_contract.py
checks the mobile app's types against it, so the three cannot drift apart unnoticed.
"""

from typing import Literal

from pydantic import BaseModel, Field

from app import services

VolunteerStatus = Literal["draft", "screening", "info_requested", "active", "declined", "suspended"]
SubmissionState = Literal[
    "submitted", "changes_requested", "rejected", "approved", "published", "withdrawn"
]
PlacementState = Literal["eligible", "team", "declined", "lapsed", "removed", "left"]
SkillState = Literal["none", "has", "offers", "exclude"]
CauseStance = Literal["neutral", "prefer", "exclude"]
Answer = Literal["yes", "no"]


# ------------------------------------------------------------------ request bodies
class SignIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class SignUp(SignIn):
    display_name: str = Field(max_length=60)


class ActiveOrg(BaseModel):
    org_id: str = Field(max_length=64)


class ProfileIn(BaseModel):
    display_name: str = Field(max_length=60)
    version: int
    skills: dict[str, str] = Field(max_length=200)  # skill_id -> none|has|offers|exclude
    causes: dict[str, str] = Field(max_length=50)  # cause_id -> neutral|prefer|exclude


class DraftIn(BaseModel):
    title: str = Field(max_length=200)
    summary: str = Field(max_length=4000)
    cause_id: str = Field(max_length=64)
    required: list[str] = Field(max_length=50)
    optional: list[str] = Field(default=[], max_length=50)

    def draft(self) -> services.Draft:
        return services.Draft(self.title, self.summary, self.cause_id, self.required, self.optional)


class SubmissionIn(DraftIn):
    org_id: str = Field(max_length=64)


class RevisionIn(DraftIn):
    version: int


class DecisionIn(BaseModel):
    action: Literal["changes_requested", "rejected", "approved"]
    reason: str = Field(max_length=2000)
    private_note: str = Field(default="", max_length=4000)
    revision: int
    version: int


class ReasonIn(BaseModel):
    reason: str = Field(max_length=2000)


class WithdrawIn(ReasonIn):
    version: int


class LeadIn(BaseModel):
    user_id: str = Field(max_length=64)


class AnswerIn(BaseModel):
    answer: Answer
    version: int


class VetIn(BaseModel):
    action: Literal["approve", "request_info", "decline", "suspend", "reinstate"]
    note: str = Field(default="", max_length=2000)
    private_note: str = Field(default="", max_length=4000)
    version: int


class OrganisationIn(BaseModel):
    key: str = Field(max_length=64)
    name: str = Field(max_length=120)
    description: str = Field(default="", max_length=1000)


# ------------------------------------------------------------------ auth + me
class Token(BaseModel):
    token: str


class SignedUp(Token):
    user_id: str


class UserRef(BaseModel):
    id: str
    display_name: str


class Organisation(BaseModel):
    id: str
    key: str
    name: str
    description: str
    opted_in: bool
    reviewer: bool
    org_admin: bool


class Me(BaseModel):
    user: UserRef
    status: VolunteerStatus
    status_label: str
    system_roles: list[str]
    organisations: list[Organisation]
    active_org_id: str | None
    unread_notifications: int


class ActiveOrgOut(BaseModel):
    active_org_id: str


class ProfileSkill(BaseModel):
    id: str
    name: str
    category: str
    state: SkillState


class SkillGroup(BaseModel):
    category: str
    skills: list[ProfileSkill]


class ProfileCause(BaseModel):
    id: str
    name: str
    stance: CauseStance


class ProfileProject(BaseModel):
    id: str
    title: str
    organisation: str
    org_id: str
    relation: Literal["suggested", "matched"]
    state: SubmissionState
    state_label: str
    detail: str | None


class ProfileProjects(BaseModel):
    active: list[ProfileProject]
    past: list[ProfileProject]
    matches_withheld: str | None


class Profile(BaseModel):
    display_name: str
    version: int
    skills: list[ProfileSkill]
    skill_groups: list[SkillGroup]  # the same skills, grouped by category in catalogue order
    skill_states: dict[SkillState, str]  # state -> label, in display order
    cause_stances: dict[CauseStance, str]
    causes: list[ProfileCause]
    projects: ProfileProjects
    status: VolunteerStatus
    status_label: str
    can_submit_for_vetting: bool


class StatusOut(BaseModel):
    status: VolunteerStatus


class Notification(BaseModel):
    id: int
    kind: str
    message: str
    link: str
    created_at: str
    read_at: str | None


class ReadOut(BaseModel):
    read: int


# ------------------------------------------------------------------ organisations
class MembershipOut(BaseModel):
    opted_in: bool


class Match(BaseModel):
    project: str
    title: str
    organisation: str
    cause: str
    preferred_cause: bool
    matched_skills: list[str]
    optional_skills: list[str]
    remaining_skills: list[str]


class Exclusion(BaseModel):
    project: str
    title: str
    reasons: list[str]


class Matches(BaseModel):
    withheld: str | None
    matches: list[Match]
    excluded: list[Exclusion]


class Project(BaseModel):
    project: str
    title: str
    summary: str
    cause: str
    required_skills: list[str]
    optional_skills: list[str]


class CoverageCandidate(BaseModel):
    volunteer: str
    name: str
    preferred_cause: bool


class CoverageSkill(BaseModel):
    skill: str
    candidates: list[CoverageCandidate]


class CoverageProject(BaseModel):
    project: str
    title: str
    gaps: list[str]
    per_skill: list[CoverageSkill]


class Coverage(BaseModel):
    withheld: str | None
    projects: list[CoverageProject]


class OrgVolunteer(BaseModel):
    id: str
    display_name: str
    status: VolunteerStatus
    joined_at: str
    offers: list[str]
    teams: int


class RemovedOut(BaseModel):
    removed: bool


# ------------------------------------------------------------------ project onboarding
class SubmissionSummary(BaseModel):
    id: str
    title: str
    state: SubmissionState
    state_label: str
    org_name: str
    current_revision: int
    updated_at: str


class SubmissionCreated(BaseModel):
    id: str
    state: SubmissionState


class SubmissionStateOut(BaseModel):
    state: SubmissionState


class Revision(BaseModel):
    revision: int
    title: str
    summary: str
    cause_id: str
    created_at: str
    required: list[str]
    optional: list[str]


class Review(BaseModel):
    revision: int
    action: str
    reason_for_author: str
    private_note: str  # empty unless the caller reviews for the organisation
    created_at: str
    reviewer_name: str


class Submission(BaseModel):
    id: str
    org_id: str
    org_name: str
    author_id: str
    author_name: str
    lead_id: str | None
    state: SubmissionState
    current_revision: int
    version: int
    approved_revision: int | None
    approved_at: str | None
    approved_by_name: str | None
    published_at: str | None
    withdrawn_at: str | None
    created_at: str
    updated_at: str
    current: Revision
    reviews: list[Review]
    is_author: bool
    is_reviewer: bool
    can_review: bool
    can_revise: bool
    can_withdraw: bool


class QueueItem(BaseModel):
    id: str
    title: str
    state: SubmissionState
    current_revision: int
    updated_at: str
    author_id: str
    author_name: str


class LeadOut(BaseModel):
    lead_id: str


# ------------------------------------------------------------------ placements
class _Placement(BaseModel):
    project_id: str
    volunteer_id: str
    state: PlacementState
    state_label: str
    volunteer_said: Answer | None
    lead_said: Answer | None
    matched_skills: list[str]
    version: int
    created_at: str
    updated_at: str


class MyPlacement(_Placement):
    """One of my own offers or teams."""

    title: str
    org_id: str
    org_name: str


class ProjectPlacement(_Placement):
    """A candidate or team member, as the project lead sees them (no contact details)."""

    display_name: str


class PlacementStateOut(BaseModel):
    state: PlacementState


# ------------------------------------------------------------------ moderation
class VettingQueueItem(BaseModel):
    id: str
    display_name: str
    status: VolunteerStatus
    status_version: int
    created_at: str
    skills_offered: int


class VettingEvent(BaseModel):
    from_status: VolunteerStatus
    to_status: VolunteerStatus
    note_for_volunteer: str
    private_note: str
    created_at: str
    actor: str


class VettingRecord(BaseModel):
    id: str
    display_name: str
    email: str | None = None  # present for system admins only; omitted for moderators
    status: VolunteerStatus
    status_version: int
    created_at: str
    vetted_at: str | None
    skills: dict[str, SkillState]
    causes: dict[str, CauseStance]
    organisations: list[str]
    history: list[VettingEvent]


# ------------------------------------------------------------------ system admin
class OrgRole(BaseModel):
    org_id: str
    org: str
    role: Literal["org_admin", "reviewer"]


class AdminUser(BaseModel):
    id: str
    display_name: str
    email: str
    status: VolunteerStatus
    created_at: str
    system_roles: list[str]
    org_roles: list[OrgRole]


class Granted(BaseModel):
    granted: str


class Revoked(BaseModel):
    revoked: str


class Created(BaseModel):
    id: str


class OutboxItem(BaseModel):
    id: int
    kind: str
    entity_id: str
    attempts: int
    last_error: str | None
    created_at: str
    next_attempt_at: str


class ProjectionStatus(BaseModel):
    pending: int
    failed: int
    items: list[OutboxItem]


class Replayed(BaseModel):
    done: int
    failed: int


class LabelCount(BaseModel):
    label: str
    nodes: int


class RelationshipCount(BaseModel):
    type: str
    relationships: int


class Inspection(BaseModel):
    organisation: str
    project: str
    cause: str
    skill: str
    eligible_volunteers: list[str]


class GraphOverview(BaseModel):
    labels: list[LabelCount]
    relationships: list[RelationshipCount]
    inspect: list[Inspection]
