import { Component, inject, signal } from '@angular/core';
import {
  IonButtons,
  IonContent,
  IonHeader,
  IonItem,
  IonLabel,
  IonList,
  IonMenuButton,
  IonNote,
  IonText,
  IonTitle,
  IonToolbar,
} from '@ionic/angular';

import { ApiService, Submission } from '../../core/api.service';

/** Track suggestions; feedback and revision happen in the web app. */
@Component({
  selector: 'app-submissions',
  templateUrl: 'submissions.page.html',
  imports: [IonButtons, IonContent, IonHeader, IonItem, IonLabel, IonList, IonMenuButton, IonNote, IonText, IonTitle, IonToolbar],
})
export class SubmissionsPage {
  readonly items = signal<Submission[]>([]);
  readonly error = signal<string | null>(null);

  constructor() {
    inject(ApiService)
      .submissions()
      .subscribe({
        next: (items) => this.items.set(items),
        error: () => this.error.set('Could not load your suggestions.'),
      });
  }
}
