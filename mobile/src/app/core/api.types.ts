// Shapes returned by the web app's JSON API, as declared in src/app/web/schemas.py.
// tests/test_api_contract.py checks every field and state here against the API's OpenAPI document,
// so a rename on the server fails the Python tests until this file follows.

export interface Organisation {
  id: string;
  key: string;
  name: string;
  description: string;
  opted_in: boolean;
  reviewer: boolean;
  team_coordinator: boolean;
  org_admin: boolean;
}

export type VolunteerStatus = 'draft' | 'screening' | 'info_requested' | 'active' | 'declined' | 'suspended';

export interface Me {
  user: { id: string; display_name: string };
  status: VolunteerStatus;
  status_label: string;
  system_roles: string[];
  organisations: Organisation[];
  active_org_id: string | null;
  unread_notifications: number;
}

export interface Notification {
  id: number;
  kind: string;
  message: string;
  link: string;
  created_at: string;
  read_at: string | null;
}

export interface Placement {
  project_id: string;
  volunteer_id: string;
  title: string;
  org_id: string;
  org_name: string;
  state: 'eligible' | 'onboarding' | 'team' | 'suspended' | 'declined' | 'lapsed' | 'removed' | 'left';
  state_label: string;
  volunteer_said: 'yes' | 'no' | null;
  lead_said: 'yes' | 'no' | null;
  matched_skills: string[];
  version: number;
}

export interface Match {
  project: string;
  title: string;
  organisation: string;
  cause: string;
  preferred_cause: boolean;
  matched_skills: string[];
  optional_skills: string[];
  remaining_skills: string[];
}

export interface Exclusion {
  project: string;
  title: string;
  reasons: string[];
}

export interface Matches {
  withheld: string | null;
  matches: Match[];
  excluded: Exclusion[];
}

export interface Submission {
  id: string;
  title: string;
  state: 'draft' | 'submitted' | 'changes_requested' | 'rejected' | 'approved' | 'published' | 'withdrawn';
  state_label: string;
  org_name: string;
  current_revision: number;
  updated_at: string;
}

export type SkillState = 'none' | 'has' | 'offers' | 'exclude';
export type CauseStance = 'neutral' | 'prefer' | 'exclude';

export interface ProfileProject {
  id: string;
  title: string;
  organisation: string;
  org_id: string;
  relation: 'suggested' | 'matched';
  state: Submission['state'];
  state_label: string;
  detail: string | null;
}

export interface ProfileSkill {
  id: string;
  name: string;
  category: string;
  state: SkillState;
}

export interface SkillGroup {
  category: string;
  skills: ProfileSkill[];
}

export interface Profile {
  display_name: string;
  version: number;
  skills: ProfileSkill[];
  /** The same skills grouped by category, in catalogue order. */
  skill_groups: SkillGroup[];
  /** State to label, in display order. The server owns the wording. */
  skill_states: Record<SkillState, string>;
  cause_stances: Record<CauseStance, string>;
  causes: { id: string; name: string; stance: CauseStance }[];
  projects: { active: ProfileProject[]; past: ProfileProject[]; matches_withheld: string | null };
  status: VolunteerStatus;
  status_label: string;
  can_submit_for_vetting: boolean;
}

export interface ProfileUpdate {
  display_name: string;
  version: number;
  skills: Record<string, SkillState>;
  causes: Record<string, CauseStance>;
}
