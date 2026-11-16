import { Component, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import {
  IonButton,
  IonButtons,
  IonContent,
  IonHeader,
  IonItem,
  IonLabel,
  IonList,
  IonMenuButton,
  IonText,
  IonTitle,
  IonToolbar,
} from '@ionic/angular';

import { ApiService, Notification } from '../../core/api.service';
import { OrgStore } from '../../core/org.store';

/** Server links that have a screen in this app: [link or link prefix, app route]. */
const APP_ROUTES: [string, string][] = [
  ['/opportunities', '/opportunities'],
  ['/profile', '/profile'],
  ['/matches', '/matches'],
  ['/submissions', '/suggestions'], // the server links one suggestion: /submissions/{id}
];

/** Every onboarding and placement step is announced here (and on the web Notifications page). */
@Component({
  selector: 'app-notifications',
  templateUrl: 'notifications.page.html',
  imports: [RouterLink, IonButton, IonButtons, IonContent, IonHeader, IonItem, IonLabel, IonList, IonMenuButton, IonText, IonTitle, IonToolbar],
})
export class NotificationsPage {
  private readonly api = inject(ApiService);
  private readonly orgs = inject(OrgStore);
  readonly items = signal<Notification[]>([]);
  readonly error = signal<string | null>(null);

  constructor() {
    this.load();
  }

  load(): void {
    this.api.notifications().subscribe({
      next: (items) => {
        this.items.set(items);
        this.error.set(null);
      },
      error: () => this.error.set('Could not load your notifications.'),
    });
  }

  /** Web-only links (e.g. a project page) are shown as text; app screens are tappable. */
  appLink(n: Notification): string | null {
    const match = APP_ROUTES.find(([link]) => n.link === link || n.link.startsWith(`${link}/`));
    return match ? match[1] : null;
  }

  readAll(): void {
    this.api.markAllRead().subscribe({
      next: () => {
        this.load();
        this.orgs.load();
      },
      error: () => this.error.set('Could not mark your notifications as read.'),
    });
  }
}
