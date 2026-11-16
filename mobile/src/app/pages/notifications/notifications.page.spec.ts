import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { provideIonicAngular } from '@ionic/angular';

import type { Notification } from '../../core/api.types';
import { ME, NOTIFICATIONS } from '../../testing/fixtures';
import { NotificationsPage } from './notifications.page';

function create() {
  TestBed.configureTestingModule({
    imports: [NotificationsPage],
    providers: [provideIonicAngular(), provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
  });
  const fixture = TestBed.createComponent(NotificationsPage);
  return { fixture, page: fixture.componentInstance, http: TestBed.inject(HttpTestingController) };
}

const linked = (link: string): Notification => ({ ...NOTIFICATIONS[0], link });

describe('NotificationsPage', () => {
  it('lists notifications, newest first, and marks them read', () => {
    const { fixture, page, http } = create();
    http.expectOne('/api/v1/notifications').flush(NOTIFICATIONS);
    fixture.detectChanges();
    const text = fixture.nativeElement.textContent;
    expect(text.indexOf('on the team')).toBeLessThan(text.indexOf('approved and active'));
    expect(page.appLink(NOTIFICATIONS[0])).toBe('/opportunities');
    page.readAll();
    http.expectOne('/api/v1/notifications/read-all').flush({ read: 1 });
    http.expectOne('/api/v1/notifications').flush(NOTIFICATIONS);
    http.expectOne('/api/v1/me').flush(ME);
  });

  it('links a suggestion notification to the suggestions screen', () => {
    const { page, http } = create();
    http.expectOne('/api/v1/notifications').flush([]);
    // the server links one suggestion by id; the app has a single suggestions screen
    expect(page.appLink(linked('/submissions/p_2cc592488dbc5377'))).toBe('/suggestions');
    expect(page.appLink(linked('/submissions'))).toBe('/suggestions');
    expect(page.appLink(linked('/profile'))).toBe('/profile');
    expect(page.appLink(linked('/matches'))).toBe('/matches');
  });

  it('leaves web-only and unknown links as plain text', () => {
    const { page, http } = create();
    http.expectOne('/api/v1/notifications').flush([]);
    expect(page.appLink(linked('/o/org_a/projects/p_1'))).toBeNull();
    expect(page.appLink(linked('/orgs'))).toBeNull();
    expect(page.appLink(linked('/admin'))).toBeNull();
    expect(page.appLink(linked(''))).toBeNull();
    expect(page.appLink(linked('/submissions-elsewhere'))).toBeNull(); // a prefix must end at a slash
    expect(page.appLink(linked('/profiles'))).toBeNull();
  });

  it('says so when the list cannot be loaded or marked read', () => {
    const { fixture, page, http } = create();
    http.expectOne('/api/v1/notifications').flush('down', { status: 503, statusText: '' });
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[role="alert"]').textContent).toContain('Could not load');
    page.readAll();
    http.expectOne('/api/v1/notifications/read-all').flush('down', { status: 503, statusText: '' });
    expect(page.error()).toContain('Could not mark');
    page.load();
    http.expectOne('/api/v1/notifications').flush(NOTIFICATIONS);
    expect(page.error()).toBeNull();
  });
});
