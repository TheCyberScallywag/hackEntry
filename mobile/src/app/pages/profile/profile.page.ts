import { HttpErrorResponse } from '@angular/common/http';
import { Component, computed, inject, signal } from '@angular/core';
import {
  IonAccordion,
  IonAccordionGroup,
  IonBadge,
  IonButton,
  IonButtons,
  IonContent,
  IonFooter,
  IonHeader,
  IonInput,
  IonItem,
  IonLabel,
  IonList,
  IonMenuButton,
  IonNote,
  IonSelect,
  IonSelectOption,
  IonText,
  IonTitle,
  IonToolbar,
} from '@ionic/angular';

import { ApiService, CauseStance, Profile, SkillGroup, SkillState } from '../../core/api.service';

/** A `{state: label}` map from the API as the options of a select, in the server's order. */
function options<T extends string>(labels: Record<T, string> | undefined): { value: T; label: string }[] {
  return Object.entries(labels ?? {}).map(([value, label]) => ({ value: value as T, label: label as string }));
}

/**
 * The signed-in user's own profile: one skill set shared by every organisation they join.
 * Same sections, order and labels as the web Profile page: the server (src/app/profile_view.py)
 * sends the grouping and every label, so nothing is defined twice. Sections: display
 * name, Skills (one sub-accordion per skill type), Causes, Active projects, Past projects.
 * Edits go to PUT /api/v1/profile, which is checked against the profile version so an edit
 * made elsewhere is never silently overwritten.
 */
@Component({
  selector: 'app-profile',
  templateUrl: 'profile.page.html',
  styleUrls: ['profile.page.scss'],
  imports: [
    IonAccordion,
    IonAccordionGroup,
    IonBadge,
    IonButton,
    IonButtons,
    IonContent,
    IonFooter,
    IonHeader,
    IonInput,
    IonItem,
    IonLabel,
    IonList,
    IonMenuButton,
    IonNote,
    IonSelect,
    IonSelectOption,
    IonText,
    IonTitle,
    IonToolbar,
  ],
})
export class ProfilePage {
  private readonly api = inject(ApiService);
  readonly profile = signal<Profile | null>(null);
  readonly displayName = signal('');
  readonly skills = signal<Record<string, SkillState>>({});
  readonly causes = signal<Record<string, CauseStance>>({});
  readonly busy = signal(false);
  readonly message = signal<{ text: string; color: 'success' | 'danger' | 'warning' } | null>(null);

  /** Skills grouped by catalogue category, in catalogue order (grouped by the server). */
  readonly groups = computed<SkillGroup[]>(() => this.profile()?.skill_groups ?? []);
  protected readonly skillStates = computed(() => options(this.profile()?.skill_states));
  protected readonly causeStances = computed(() => options(this.profile()?.cause_stances));

  readonly offeredCount = computed(() => Object.values(this.skills()).filter((s) => s === 'offers').length);
  readonly excludedCount = computed(() => Object.values(this.skills()).filter((s) => s === 'exclude').length);

  readonly dirty = computed(() => {
    const p = this.profile();
    if (!p) return false;
    return (
      p.display_name !== this.displayName() ||
      p.skills.some((s) => s.state !== this.skills()[s.id]) ||
      p.causes.some((c) => c.stance !== this.causes()[c.id])
    );
  });

  constructor() {
    this.load();
  }

  load(): void {
    this.api.profile().subscribe({
      next: (p) => this.apply(p),
      error: () => this.message.set({ text: 'Could not load your profile.', color: 'danger' }),
    });
  }

  /** How many skills of one type are in a given state (for the sub-accordion header). */
  count(group: SkillGroup, state: SkillState): number {
    const current = this.skills();
    return group.skills.filter((s) => current[s.id] === state).length;
  }

  setSkill(id: string, state: SkillState): void {
    this.skills.update((all) => ({ ...all, [id]: state }));
  }

  setCause(id: string, stance: CauseStance): void {
    this.causes.update((all) => ({ ...all, [id]: stance }));
  }

  save(): void {
    const p = this.profile();
    if (!p || !this.dirty()) return;
    this.busy.set(true);
    this.message.set(null);
    this.api
      .saveProfile({
        display_name: this.displayName().trim(),
        version: p.version,
        skills: this.skills(),
        causes: this.causes(),
      })
      .subscribe({
        next: (saved) => {
          this.busy.set(false);
          this.apply(saved);
          this.message.set({ text: 'Profile saved. Your matches now use it.', color: 'success' });
        },
        error: (err: HttpErrorResponse) => {
          this.busy.set(false);
          if (err.status === 409) {
            this.message.set({ text: 'Your profile changed elsewhere, so it has been reloaded.', color: 'warning' });
            this.load();
          } else {
            const detail = typeof err.error?.detail === 'string' ? err.error.detail : null;
            this.message.set({ text: detail ?? 'Could not save your profile.', color: 'danger' });
          }
        },
      });
  }

  /** Onboarding step 1: send the saved profile to a moderator for screening. */
  submitForVetting(): void {
    this.busy.set(true);
    this.api.submitForVetting().subscribe({
      next: () => {
        this.busy.set(false);
        this.message.set({ text: "Thanks! Your profile is in screening. We'll notify you when it's reviewed.", color: 'success' });
        this.load();
      },
      error: (err: HttpErrorResponse) => {
        this.busy.set(false);
        const detail = typeof err.error?.detail === 'string' ? err.error.detail : null;
        this.message.set({ text: detail ?? 'Could not submit your profile.', color: 'danger' });
      },
    });
  }

  private apply(p: Profile): void {
    this.profile.set(p);
    this.displayName.set(p.display_name);
    this.skills.set(Object.fromEntries(p.skills.map((s) => [s.id, s.state])));
    this.causes.set(Object.fromEntries(p.causes.map((c) => [c.id, c.stance])));
  }
}
