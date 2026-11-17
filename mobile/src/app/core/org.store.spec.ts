import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { ME } from '../testing/fixtures';
import { OrgStore } from './org.store';

function setup() {
  TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
  const store = TestBed.inject(OrgStore);
  const http = TestBed.inject(HttpTestingController);
  store.load();
  http.expectOne('/api/v1/me').flush(ME);
  return { store, http };
}

describe('OrgStore', () => {
  it('loads the user, their volunteering organisations and the active one', () => {
    const { store } = setup();
    expect(store.volunteering().map((o) => o.id)).toEqual(['org_a', 'org_b']);
    expect(store.activeId()).toBe('org_a');
    store.clear();
    expect(store.me()).toBeNull();
    expect(store.activeId()).toBeNull();
  });

  it('switches the active organisation when the server accepts', () => {
    const { store, http } = setup();
    store.switchTo('org_b');
    const req = http.expectOne('/api/v1/me/active-org');
    expect(req.request.body).toEqual({ org_id: 'org_b' });
    req.flush({ active_org_id: 'org_b' });
    expect(store.activeId()).toBe('org_b');
  });

  it('reloads from the server when a switch is refused', () => {
    const { store, http } = setup();
    store.switchTo('org_b');
    http.expectOne('/api/v1/me/active-org').flush({ detail: 'no' }, { status: 403, statusText: '' });
    expect(store.activeId()).toBe('org_a');
    http.expectOne('/api/v1/me').flush({ ...ME, organisations: [ME.organisations[0]] });
    expect(store.volunteering().map((o) => o.id)).toEqual(['org_a']);
  });

  it('forgets the user when they cannot be loaded', () => {
    const { store, http } = setup();
    store.load();
    http.expectOne('/api/v1/me').flush('down', { status: 503, statusText: '' });
    expect(store.me()).toBeNull();
  });
});
