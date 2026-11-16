import { HttpErrorResponse } from '@angular/common/http';
import { Component, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed, toObservable } from '@angular/core/rxjs-interop';
import {
  IonBadge,
  IonButtons,
  IonCard,
  IonCardContent,
  IonCardHeader,
  IonCardSubtitle,
  IonCardTitle,
  IonChip,
  IonContent,
  IonHeader,
  IonItem,
  IonMenuButton,
  IonText,
  IonTitle,
  IonToggle,
  IonToolbar,
} from '@ionic/angular';

import { EMPTY, catchError, switchMap, tap } from 'rxjs';

import { ApiService, Matches } from '../../core/api.service';
import { OrgStore } from '../../core/org.store';

/** Explainable matches for the active organisation, decided by the server's Relate query. */
@Component({
  selector: 'app-matches',
  templateUrl: 'matches.page.html',
  imports: [
    IonBadge,
    IonButtons,
    IonCard,
    IonCardContent,
    IonCardHeader,
    IonCardSubtitle,
    IonCardTitle,
    IonChip,
    IonContent,
    IonHeader,
    IonItem,
    IonMenuButton,
    IonText,
    IonTitle,
    IonToggle,
    IonToolbar,
  ],
})
export class MatchesPage {
  private readonly api = inject(ApiService);
  protected readonly orgs = inject(OrgStore);
  readonly preferredOnly = signal(false);
  readonly result = signal<Matches | null>(null);
  readonly error = signal<string | null>(null);

  private readonly query = computed(() => ({ org: this.orgs.activeId(), preferredOnly: this.preferredOnly() }));

  constructor() {
    // switchMap drops the request in flight when the organisation or the filter changes, so a
    // slow earlier answer can never replace a newer one.
    toObservable(this.query)
      .pipe(
        tap(() => this.error.set(null)),
        switchMap(({ org, preferredOnly }) =>
          org
            ? this.api.matches(org, preferredOnly).pipe(
                catchError((e: HttpErrorResponse) => {
                  this.error.set(e.status === 403 ? 'Join this organisation as a volunteer to see matches.' : 'Could not load matches.');
                  return EMPTY;
                }),
              )
            : EMPTY,
        ),
        takeUntilDestroyed(),
      )
      .subscribe((r) => this.result.set(r));
  }
}
